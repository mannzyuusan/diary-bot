"""GitHub Actions から実行する日記リマインド。

PCのBotが止まっていても動くよう、DBではなくDiscordから直接判定する。
- 誰の日記チャンネルか: チャンネルトピックの「日記の持ち主: @ユーザー」（/register で設定）
- 書いたかどうか: 各チャンネルの履歴
標準ライブラリのみで動く（pip install 不要）。
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.environ["DISCORD_TOKEN"].strip()
# サーバーを特定するためと、/register 前の一人用設定の引き継ぎに使う
LEGACY_CHANNEL_ID = os.environ["DIARY_CHANNEL_ID"].strip()
LEGACY_OWNER_ID = os.getenv("OWNER_ID", "").strip()
DAY_START_HOUR = int(os.getenv("DAY_START_HOUR", "4"))
SLOT = os.getenv("SLOT", "first")  # first: 23時 / last: 1時
FORCE = os.getenv("FORCE") == "1"  # テスト用: 記入済みでも送信する

JST = ZoneInfo("Asia/Tokyo")
API = "https://discord.com/api/v10"
MAX_PAGES = 10  # 100件 × 10ページまで遡って連続日数を数える
OWNER_RE = re.compile(r"日記の持ち主: <@!?(\d+)>")  # bot.py と同じ形式
TEXT_CHANNEL = 0


def request(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bot {TOKEN}",
            "Content-Type": "application/json",
            # DiscordはUser-Agentが無いと弾くことがある
            "User-Agent": "DiscordBot (diary-bot, 1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def diary_date(timestamp: str) -> date:
    dt = datetime.fromisoformat(timestamp).astimezone(JST)
    return (dt - timedelta(hours=DAY_START_HOUR)).date()


def find_owners() -> dict[str, str]:
    """日記チャンネルID -> 持ち主のユーザーID"""
    guild_id = request("GET", f"/channels/{LEGACY_CHANNEL_ID}")["guild_id"]
    owners = {}
    for ch in request("GET", f"/guilds/{guild_id}/channels"):
        m = OWNER_RE.search(ch.get("topic") or "")
        if ch["type"] == TEXT_CHANNEL and m:
            owners[ch["id"]] = m.group(1)
    if LEGACY_OWNER_ID and LEGACY_CHANNEL_ID not in owners and LEGACY_OWNER_ID not in owners.values():
        owners[LEGACY_CHANNEL_ID] = LEGACY_OWNER_ID
    return owners


def is_diary(message: dict, owner_id: str, bot_id: str) -> bool:
    author = message["author"]["id"]
    if author == owner_id:
        return bool(message["content"].strip() or message["attachments"])
    # /diary フォームからの投稿（Botが埋め込みで投稿する）
    if author == bot_id:
        return any(e.get("title", "").startswith("📔") for e in message.get("embeds", []))
    return False


def written_dates(channel_id: str, owner_id: str, bot_id: str) -> set[date]:
    dates, before = set(), None
    for _ in range(MAX_PAGES):
        query = f"?limit=100&before={before}" if before else "?limit=100"
        messages = request("GET", f"/channels/{channel_id}/messages{query}")
        if not messages:
            break
        dates |= {diary_date(m["timestamp"]) for m in messages if is_diary(m, owner_id, bot_id)}
        before = messages[-1]["id"]
    return dates


def streak(dates: set[date], today: date) -> int:
    d = today if today in dates else today - timedelta(days=1)
    n = 0
    while d in dates:
        n += 1
        d -= timedelta(days=1)
    return n


def remind(channel_id: str, owner_id: str, bot_id: str, today: date):
    dates = written_dates(channel_id, owner_id, bot_id)
    if today in dates and not FORCE:
        print(f"#{channel_id}: {today} 記入済みのためスキップ")
        return

    n = streak(dates, today)
    if SLOT == "first":
        msg = f"<@{owner_id}> 今日の日記がまだだよ～📝 一言だけでもOK！"
    elif n:
        msg = f"<@{owner_id}> 今日の日記、まだ書いてないよ！🔥 連続 **{n} 日** が途切れちゃう…「今日は疲れた」だけでも大丈夫！"
    else:
        msg = f"<@{owner_id}> 寝る前に一行だけ日記を書こう🌙 今日から連続記録スタート！"
    msg += "\n-# ボタンが反応しないときは、このチャンネルに直接書き込んでもOK"

    request("POST", f"/channels/{channel_id}/messages", {
        "content": msg,
        "allowed_mentions": {"users": [owner_id]},
        # PCのBotが起動中なら、このボタンで入力フォームが開く
        "components": [{
            "type": 1,
            "components": [{
                "type": 2, "style": 1, "label": "日記を書く",
                "emoji": {"name": "✏️"}, "custom_id": "diary:write",
            }],
        }],
    })
    print(f"#{channel_id}: {today} リマインドを送信（{SLOT}, 連続{n}日）")


def main():
    bot_id = request("GET", "/users/@me")["id"]
    today = (datetime.now(JST) - timedelta(hours=DAY_START_HOUR)).date()
    owners = find_owners()
    print(f"日記チャンネル: {len(owners)} 件")

    failed = False
    for channel_id, owner_id in owners.items():
        try:
            remind(channel_id, owner_id, bot_id, today)
        except Exception as e:
            # 一人分の失敗で他の人のリマインドが止まらないようにする
            print(f"#{channel_id}: 失敗 {e!r}", file=sys.stderr)
            failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
