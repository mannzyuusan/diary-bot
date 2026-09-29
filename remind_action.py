"""GitHub Actions から実行する日記リマインド（標準ライブラリのみ）。"""
import os
import sys
from datetime import date, datetime, timedelta

import discord_rest as dr

SLOT = os.getenv("SLOT", "first")  # first: 23時 / last: 1時
FORCE = os.getenv("FORCE") == "1"  # テスト用: 記入済みでも送信する
SCHEDULED = os.getenv("SCHEDULED") == "1"  # 定期実行かどうか（手動実行なら遅延チェックをしない）
SLOT_HOUR = {"first": 23, "last": 1}
MAX_DELAY = timedelta(hours=2)


def streak(dates: set[date], today: date) -> int:
    d = today if today in dates else today - timedelta(days=1)
    n = 0
    while d in dates:
        n += 1
        d -= timedelta(days=1)
    return n


def remind(channel_id: str, owner_id: str, bot_id: str, today: date):
    dates = {d for d, _ in dr.diary_history(channel_id, owner_id, bot_id)}
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

    dr.post(channel_id, {
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


def is_too_late() -> bool:
    """GitHub Actions の定期実行は混雑で数時間遅れることがある。
    本来の時刻から大きく遅れた場合や、日付の区切り（朝4時）をまたいだ場合は送らない。"""
    now = datetime.now(dr.JST)
    intended = now.replace(hour=SLOT_HOUR[SLOT], minute=0, second=0, microsecond=0)
    if intended > now:
        intended -= timedelta(days=1)
    delay = now - intended
    print(f"本来の実行時刻 {intended:%m/%d %H:%M} から {delay} 遅れ")
    return delay > MAX_DELAY or dr.diary_date(intended.isoformat()) != dr.today()


def main():
    if SCHEDULED and is_too_late():
        print("遅れすぎたためスキップ")
        return
    bot_id = dr.bot_id()
    today = dr.today()
    owners = dr.find_owners()
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
