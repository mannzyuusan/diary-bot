"""GitHub Actions 用の Discord REST API ヘルパー（標準ライブラリのみ）。

PCのBotが止まっていても動くよう、DBではなくDiscordから直接判定する。
- 誰の日記チャンネルか: チャンネルトピックの「日記の持ち主: @ユーザー」（/register で設定）
- 日記の中身: 各チャンネルの履歴
"""
import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.environ["DISCORD_TOKEN"].strip()
# サーバーを特定するためと、/register 前の一人用設定の引き継ぎに使う
LEGACY_CHANNEL_ID = os.environ["DIARY_CHANNEL_ID"].strip()
LEGACY_OWNER_ID = os.getenv("OWNER_ID", "").strip()
DAY_START_HOUR = int(os.getenv("DAY_START_HOUR", "4"))

JST = ZoneInfo("Asia/Tokyo")
API = "https://discord.com/api/v10"
MAX_PAGES = 10  # 100件 × 10ページまで遡る
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


def today() -> date:
    return (datetime.now(JST) - timedelta(hours=DAY_START_HOUR)).date()


def diary_date(timestamp: str) -> date:
    dt = datetime.fromisoformat(timestamp).astimezone(JST)
    return (dt - timedelta(hours=DAY_START_HOUR)).date()


def bot_id() -> str:
    return request("GET", "/users/@me")["id"]


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


def diary_text(message: dict, owner_id: str, bot_id: str) -> str | None:
    """日記のメッセージなら本文を返す。日記でなければ None。"""
    author = message["author"]["id"]
    if author == owner_id:
        parts = [message["content"].strip()] if message["content"].strip() else []
        parts += [f"[添付: {a['filename']}]" for a in message["attachments"]]
        return "\n".join(parts) or None
    # /diary フォームからの投稿（Botが埋め込みで投稿する）
    if author == bot_id:
        for e in message.get("embeds", []):
            if e.get("title", "").startswith("📔"):
                return "\n".join(f"【{f['name']}】{f['value']}" for f in e.get("fields", []))
    return None


def diary_history(channel_id: str, owner_id: str, bot_id: str, until: date | None = None):
    """新しい順に (日付, 本文) を返す。until を渡すとその日より前に達した時点で止める。"""
    before = None
    for _ in range(MAX_PAGES):
        query = f"?limit=100&before={before}" if before else "?limit=100"
        messages = request("GET", f"/channels/{channel_id}/messages{query}")
        if not messages:
            return
        for m in messages:
            d = diary_date(m["timestamp"])
            if until and d < until:
                return
            text = diary_text(m, owner_id, bot_id)
            if text:
                yield d, text
        before = messages[-1]["id"]


def post(channel_id: str, body: dict):
    request("POST", f"/channels/{channel_id}/messages", body)
