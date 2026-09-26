import logging
import os
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
CHANNEL_ID = int(os.environ["DIARY_CHANNEL_ID"])
OWNER_ID = int(os.environ["OWNER_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])
# この時刻より前の投稿は前日の日記として扱う（夜更かし対策）
DAY_START_HOUR = int(os.getenv("DAY_START_HOUR", "4"))

JST = ZoneInfo("Asia/Tokyo")
SUMMARY_TIME = time(7, 0, tzinfo=JST)
BACKFILL_DAYS = 30

BASE_DIR = Path(__file__).parent
SUMMARY_DIR = BASE_DIR / "summaries"

log = logging.getLogger("diary-bot")

db = DiaryDB(str(BASE_DIR / "diary.db"))
claude = anthropic.AsyncAnthropic()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
guild = discord.Object(id=GUILD_ID)


def now() -> datetime:
    return datetime.now(JST)


def diary_date(dt: datetime) -> date:
    return (dt.astimezone(JST) - timedelta(hours=DAY_START_HOUR)).date()


def message_text(message: discord.Message) -> str:
    parts = [message.content.strip()] if message.content.strip() else []
    parts += [f"[添付: {a.filename}]" for a in message.attachments]
    return "\n".join(parts)


def is_diary_message(message: discord.Message) -> bool:
    return message.channel.id == CHANNEL_ID and message.author.id == OWNER_ID


def record(message: discord.Message) -> bool:
    """日記として保存する。その日の最初の投稿なら True。"""
    text = message_text(message)
    if not text:
        return False
    d = diary_date(message.created_at)
    first = not db.has_entry(d)
    db.upsert(message.id, d, text, message.created_at)
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


async def diary_channel() -> discord.TextChannel:
    return client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)


# --- 質問形式の入力フォーム ---

QUESTIONS = [
    ("今日あったこと", "例: 研究室でシミュレータのバグを直した", True),
    ("うれしかったこと・悲しかったこと", "小さなことでもOK", False),
    ("明日やること", "例: 輪講の資料を仕上げる", False),
]


class DiaryModal(discord.ui.Modal, title="今日の日記"):
    def __init__(self):
        super().__init__()
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
        answers = [(label, f.value.strip()) for label, f in self.inputs if f.value.strip()]
        d = diary_date(now())
        first = not db.has_entry(d)

        embed = discord.Embed(title=f"📔 {d:%Y/%m/%d} の日記", color=0x5865F2)
        for label, value in answers:
            embed.add_field(name=label, value=value, inline=False)

        channel = await diary_channel()
        posted = await channel.send(embed=embed)
        text = "\n".join(f"【{label}】{value}" for label, value in answers)
        db.upsert(posted.id, d, text, posted.created_at)

        reply = "📝 記録しました！"
        if first:
            reply += f" 連続 **{db.streak(d)} 日目** 🔥"
        await interaction.followup.send(reply, ephemeral=True)


class WriteButton(discord.ui.View):
    """リマインドに付けるボタン。Bot再起動後も押せるよう timeout=None + custom_id。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="日記を書く", emoji="✏️", style=discord.ButtonStyle.primary, custom_id="diary:write")
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_modal(interaction)


async def open_modal(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("この日記は本人だけが書けます。", ephemeral=True)
        return
    await interaction.response.send_modal(DiaryModal())


async def build_summary(start: date, end: date, label: str) -> str:
    entries = db.entries_between(start, end)
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


async def post_summary(start: date, end: date, label: str, key: str):
    if db.has_summary(key):
        return
    text = await build_summary(start, end, label)
    channel = await diary_channel()
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
    if today.weekday() == 0:  # 月曜: 先週分
        start = yesterday - timedelta(days=6)
        await post_summary(start, yesterday, "週のまとめ", f"week-{start.isoformat()}")
    if today.day == 1:  # 1日: 先月分
        start = yesterday.replace(day=1)
        await post_summary(start, yesterday, f"{start.month}月のまとめ", f"month-{start:%Y-%m}")


@auto_summary.before_loop
async def wait_ready():
    await client.wait_until_ready()


# --- イベント ---

async def backfill():
    """Botが停止していた間の投稿を取り込む。"""
    channel = await diary_channel()
    last_id = db.last_message_id()
    after = discord.Object(id=last_id) if last_id else now() - timedelta(days=BACKFILL_DAYS)
    count = 0
    async for m in channel.history(after=after, limit=None, oldest_first=True):
        if is_diary_message(m) and message_text(m):
            record(m)
            count += 1
    log.info("backfill: %d messages", count)


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
        await backfill()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not is_diary_message(message):
        return
    first = record(message)
    await message.add_reaction("✅")
    if first:
        streak = db.streak(diary_date(message.created_at))
        await message.reply(f"📝 記録しました！ 連続 **{streak} 日目** 🔥", mention_author=False)


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.channel_id != CHANNEL_ID or "content" not in payload.data:
        return
    if int(payload.data.get("author", {}).get("id", 0)) != OWNER_ID:
        return  # フォームから投稿したBotのメッセージは上書きしない
    db.update_content(payload.message_id, payload.data["content"])


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.channel_id == CHANNEL_ID:
        db.delete(payload.message_id)


# --- スラッシュコマンド ---

@tree.command(description="質問に答えて今日の日記を書く")
async def diary(interaction: discord.Interaction):
    await open_modal(interaction)


@tree.command(description="連続記入日数と今月の記入状況を表示")
async def streak(interaction: discord.Interaction):
    today = diary_date(now())
    month_start = today.replace(day=1)
    written = db.count_days(month_start, today)
    status = "✅ 記入済み" if db.has_entry(today) else "⬜ まだ"
    await interaction.response.send_message(
        f"🔥 連続 **{db.streak(today)} 日**\n"
        f"📅 今月: **{written} / {today.day} 日**\n"
        f"今日: {status}"
    )


@tree.command(description="直近N日分の日記をAIでまとめる")
@app_commands.describe(days="まとめる日数（既定: 7）")
async def summary(interaction: discord.Interaction, days: app_commands.Range[int, 1, 92] = 7):
    await interaction.response.defer(thinking=True)
    end = diary_date(now())
    start = end - timedelta(days=days - 1)
    chunks = split_message(await build_summary(start, end, f"直近{days}日のまとめ"))
    await interaction.followup.send(chunks[0])
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)


if __name__ == "__main__":
    client.run(TOKEN, root_logger=True)
