import logging
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from db import DiaryDB
from summarizer import summarize

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
# 一人用だった頃の設定。/register 前でもこの組み合わせは日記チャンネルとして扱う
LEGACY_CHANNEL_ID = int(os.getenv("DIARY_CHANNEL_ID") or 0)
LEGACY_OWNER_ID = int(os.getenv("OWNER_ID") or 0)
# この時刻より前の投稿は前日の日記として扱う（夜更かし対策）
DAY_START_HOUR = int(os.getenv("DAY_START_HOUR", "4"))

JST = ZoneInfo("Asia/Tokyo")
SUMMARY_TIME = time(7, 0, tzinfo=JST)
BACKFILL_DAYS = 30
# チャンネルトピックに書く持ち主の目印。remind_action.py も同じ形式で読む
OWNER_RE = re.compile(r"日記の持ち主: <@!?(\d+)>")

BASE_DIR = Path(__file__).parent
SUMMARY_DIR = BASE_DIR / "summaries"

log = logging.getLogger("diary-bot")

db = DiaryDB(str(BASE_DIR / "diary.db"), legacy_owner_id=LEGACY_OWNER_ID or None)
claude = anthropic.AsyncAnthropic()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
guild = discord.Object(id=GUILD_ID)

# 日記チャンネルID -> 持ち主のユーザーID
owners: dict[int, int] = {}


def now() -> datetime:
    return datetime.now(JST)


def diary_date(dt: datetime) -> date:
    return (dt.astimezone(JST) - timedelta(hours=DAY_START_HOUR)).date()


def channel_of(user_id: int) -> int | None:
    return next((c for c, u in owners.items() if u == user_id), None)


def load_owners():
    owners.clear()
    g = client.get_guild(GUILD_ID)
    for ch in g.text_channels if g else []:
        m = OWNER_RE.search(ch.topic or "")
        if m:
            owners[ch.id] = int(m.group(1))
    if LEGACY_CHANNEL_ID and LEGACY_CHANNEL_ID not in owners and LEGACY_OWNER_ID and not channel_of(LEGACY_OWNER_ID):
        owners[LEGACY_CHANNEL_ID] = LEGACY_OWNER_ID
    log.info("diary channels: %s", owners)


def message_text(message: discord.Message) -> str:
    parts = [message.content.strip()] if message.content.strip() else []
    parts += [f"[添付: {a.filename}]" for a in message.attachments]
    return "\n".join(parts)


def is_diary_message(message: discord.Message) -> bool:
    return owners.get(message.channel.id) == message.author.id


def record(message: discord.Message) -> bool:
    """日記として保存する。その日の最初の投稿なら True。"""
    text = message_text(message)
    if not text:
        return False
    d = diary_date(message.created_at)
    first = not db.has_entry(message.author.id, d)
    db.upsert(message.id, message.author.id, d, text, message.created_at)
    return first


def split_message(text: str, limit: int = 1900) -> list[str]:
    chunks, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = ""
        buf += line
    if buf:
        chunks.append(buf)
    return chunks


async def get_channel(channel_id: int) -> discord.TextChannel:
    return client.get_channel(channel_id) or await client.fetch_channel(channel_id)


NOT_REGISTERED = "まだ日記チャンネルが登録されていません。自分の日記チャンネルで `/register` を実行してください。"


# --- 質問形式の入力フォーム ---

QUESTIONS = [
    ("今日あったこと", "例: 研究室でシミュレータのバグを直した", True),
    ("うれしかったこと・悲しかったこと", "小さなことでもOK", False),
    ("明日やること", "例: 輪講の資料を仕上げる", False),
]


class DiaryModal(discord.ui.Modal, title="今日の日記"):
    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id
        self.inputs = []
        for label, placeholder, required in QUESTIONS:
            field = discord.ui.TextInput(
                label=label, placeholder=placeholder, required=required,
                style=discord.TextStyle.paragraph, max_length=1000,
            )
            self.add_item(field)
            self.inputs.append((label, field))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        answers = [(label, f.value.strip()) for label, f in self.inputs if f.value.strip()]
        d = diary_date(now())
        first = not db.has_entry(user_id, d)

        embed = discord.Embed(title=f"📔 {d:%Y/%m/%d} の日記", color=0x5865F2)
        for label, value in answers:
            embed.add_field(name=label, value=value, inline=False)

        channel = await get_channel(self.channel_id)
        posted = await channel.send(embed=embed)
        text = "\n".join(f"【{label}】{value}" for label, value in answers)
        db.upsert(posted.id, user_id, d, text, posted.created_at)

        reply = "📝 記録しました！"
        if first:
            reply += f" 連続 **{db.streak(user_id, d)} 日目** 🔥"
        await interaction.followup.send(reply, ephemeral=True)


class WriteButton(discord.ui.View):
    """リマインドに付けるボタン。Bot再起動後も押せるよう timeout=None + custom_id。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="日記を書く", emoji="✏️", style=discord.ButtonStyle.primary, custom_id="diary:write")
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        if owners.get(interaction.channel_id) != interaction.user.id:
            await interaction.response.send_message("これは他の人の日記チャンネルです。", ephemeral=True)
            return
        await interaction.response.send_modal(DiaryModal(interaction.channel_id))


async def build_summary(user_id: int, start: date, end: date, label: str) -> str:
    entries = db.entries_between(user_id, start, end)
    total_days = (end - start).days + 1
    header = f"## 🗓️ {label}（{start:%m/%d}〜{end:%m/%d}）\n記入日数: **{len(entries)} / {total_days} 日**\n\n"
    if not entries:
        return header + "この期間の日記はありませんでした。今日から一言だけでも書いてみよう！"
    if not os.getenv("ANTHROPIC_API_KEY"):
        return header + "（APIキー未設定のため、AI要約はスキップしました）"
    try:
        body = await summarize(claude, entries, label)
    except anthropic.APIConnectionError:
        log.exception("Claude API connection error")
        return header + "⚠️ Claude APIに接続できず、要約を作れませんでした。"
    except anthropic.APIStatusError as e:
        log.exception("Claude API error")
        return header + f"⚠️ 要約の生成に失敗しました（HTTP {e.status_code}）。"
    except Exception:
        # 想定外のエラーで定期タスクが止まらないようにする
        log.exception("summary failed")
        return header + "⚠️ 要約の生成に失敗しました。"
    return header + body


async def post_summary(user_id: int, channel_id: int, start: date, end: date, label: str, period: str):
    key = f"{user_id}-{period}"
    if db.has_summary(key):
        return
    text = await build_summary(user_id, start, end, label)
    channel = await get_channel(channel_id)
    for chunk in split_message(text):
        await channel.send(chunk)
    db.save_summary(key, text)
    SUMMARY_DIR.mkdir(exist_ok=True)
    (SUMMARY_DIR / f"{key}.md").write_text(text, encoding="utf-8")


# --- 定期タスク ---
# リマインドは GitHub Actions（remind_action.py）が担当。PCが止まっていても届くようにするため。

@tasks.loop(time=SUMMARY_TIME)
async def auto_summary():
    today = now().date()
    yesterday = today - timedelta(days=1)
    for channel_id, user_id in list(owners.items()):
        try:
            if today.weekday() == 0:  # 月曜: 先週分
                start = yesterday - timedelta(days=6)
                await post_summary(user_id, channel_id, start, yesterday, "週のまとめ", f"week-{start.isoformat()}")
            if today.day == 1:  # 1日: 先月分
                start = yesterday.replace(day=1)
                await post_summary(user_id, channel_id, start, yesterday, f"{start.month}月のまとめ", f"month-{start:%Y-%m}")
        except Exception:
            log.exception("auto summary failed: channel=%s", channel_id)


@auto_summary.before_loop
async def wait_ready():
    await client.wait_until_ready()


# --- イベント ---

async def backfill():
    """Botが停止していた間の投稿を取り込む。"""
    for channel_id, user_id in owners.items():
        channel = await get_channel(channel_id)
        last_id = db.last_message_id(user_id)
        after = discord.Object(id=last_id) if last_id else now() - timedelta(days=BACKFILL_DAYS)
        count = 0
        async for m in channel.history(after=after, limit=None, oldest_first=True):
            if is_diary_message(m) and message_text(m):
                record(m)
                count += 1
        log.info("backfill #%s: %d messages", channel.name, count)


@client.event
async def setup_hook():
    client.add_view(WriteButton())
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    auto_summary.start()


_ready_once = False


@client.event
async def on_ready():
    global _ready_once
    log.info("logged in as %s", client.user)
    if not _ready_once:
        _ready_once = True
        load_owners()
        await backfill()


@client.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
    # トピックを手で書き換えた場合も反映する
    if isinstance(after, discord.TextChannel) and before.topic != after.topic:
        load_owners()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not is_diary_message(message):
        return
    first = record(message)
    await message.add_reaction("✅")
    if first:
        streak = db.streak(message.author.id, diary_date(message.created_at))
        await message.reply(f"📝 記録しました！ 連続 **{streak} 日目** 🔥", mention_author=False)


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if "content" not in payload.data:
        return
    author_id = int(payload.data.get("author", {}).get("id", 0))
    if owners.get(payload.channel_id) != author_id:
        return  # フォームから投稿したBotのメッセージは上書きしない
    db.update_content(payload.message_id, payload.data["content"])


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.channel_id in owners:
        db.delete(payload.message_id)


# --- スラッシュコマンド ---

@tree.command(description="このチャンネルを自分（または指定した人）の日記チャンネルにする")
@app_commands.describe(user="日記の持ち主（省略すると自分）")
async def register(interaction: discord.Interaction, user: discord.Member | None = None):
    me = interaction.user
    user = user or me
    channel = interaction.channel
    is_admin = me.guild_permissions.manage_guild
    current = owners.get(channel.id)

    error = None
    if not isinstance(channel, discord.TextChannel):
        error = "テキストチャンネルで実行してください。"
    elif user.bot:
        error = "Botは登録できません。"
    elif user.id != me.id and not is_admin:
        error = "他の人を登録できるのはサーバー管理者だけです。"
    elif current and current != user.id and not is_admin:
        error = f"このチャンネルはすでに <@{current}> の日記チャンネルです。"
    elif channel_of(user.id) not in (None, channel.id):
        error = f"{user.mention} はすでに <#{channel_of(user.id)}> を日記チャンネルにしています。"
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    try:
        await channel.edit(topic=f"📔 {user.display_name} の日記 | 日記の持ち主: {user.mention}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "Botに「チャンネルの管理」権限がないため登録できません。サーバー設定でBotのロールに権限を付けてください。",
            ephemeral=True,
        )
        return
    owners[channel.id] = user.id
    await interaction.response.send_message(
        f"✅ このチャンネルを {user.mention} の日記チャンネルにしました！\n"
        "`/diary` で質問に答えるか、ここに直接書き込めば日記になります。毎晩23時と1時に書き忘れをお知らせします。"
    )


@tree.command(description="質問に答えて今日の日記を書く")
async def diary(interaction: discord.Interaction):
    channel_id = channel_of(interaction.user.id)
    if not channel_id:
        await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
        return
    await interaction.response.send_modal(DiaryModal(channel_id))


@tree.command(description="連続記入日数と今月の記入状況を表示")
async def streak(interaction: discord.Interaction):
    user_id = interaction.user.id
    if not channel_of(user_id):
        await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
        return
    today = diary_date(now())
    written = db.count_days(user_id, today.replace(day=1), today)
    status = "✅ 記入済み" if db.has_entry(user_id, today) else "⬜ まだ"
    await interaction.response.send_message(
        f"🔥 連続 **{db.streak(user_id, today)} 日**\n"
        f"📅 今月: **{written} / {today.day} 日**\n"
        f"今日: {status}"
    )


@tree.command(description="直近N日分の日記をAIでまとめる")
@app_commands.describe(days="まとめる日数（既定: 7）")
async def summary(interaction: discord.Interaction, days: app_commands.Range[int, 1, 92] = 7):
    user_id = interaction.user.id
    if not channel_of(user_id):
        await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    end = diary_date(now())
    start = end - timedelta(days=days - 1)
    chunks = split_message(await build_summary(user_id, start, end, f"直近{days}日のまとめ"))
    await interaction.followup.send(chunks[0])
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)


if __name__ == "__main__":
    client.run(TOKEN, root_logger=True)
