"""GitHub Actions から実行する週・月のまとめ。毎朝7時に起動し、月曜は先週分、1日は先月分を投稿する。"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta

import anthropic

import discord_rest as dr
from summarizer import summarize

PERIOD = os.getenv("PERIOD", "auto")  # auto / week / month（week・month は手動実行用）


def periods(now: date) -> list[tuple[date, date, str]]:
    last_sunday = now - timedelta(days=now.weekday() + 1)
    week = (last_sunday - timedelta(days=6), last_sunday, "週のまとめ")
    month_end = now.replace(day=1) - timedelta(days=1)
    month = (month_end.replace(day=1), month_end, f"{month_end.month}月のまとめ")
    if PERIOD == "week":
        return [week]
    if PERIOD == "month":
        return [month]
    return ([week] if now.weekday() == 0 else []) + ([month] if now.day == 1 else [])


def collect(channel_id: str, owner_id: str, bot_id: str, start: date, end: date):
    """期間内の日記 [(日付, 本文), ...]（古い順）と、期間より前にも日記があるかを返す。"""
    entries: dict[date, list[str]] = {}
    started_before = False
    for d, text in dr.diary_history(channel_id, owner_id, bot_id):
        if d < start:
            started_before = True
            break
        if d <= end:
            entries.setdefault(d, []).insert(0, text)
    merged = [(d.isoformat(), "\n".join(texts)) for d, texts in sorted(entries.items())]
    return merged, started_before


async def build(client: anthropic.AsyncAnthropic, entries, start: date, end: date, label: str) -> str:
    total_days = (end - start).days + 1
    header = f"## 🗓️ {label}（{start:%m/%d}〜{end:%m/%d}）\n記入日数: **{len(entries)} / {total_days} 日**\n\n"
    if not entries:
        return header + "この期間の日記はありませんでした。今日から一言だけでも書いてみよう！"
    return header + await summarize(client, entries, label)


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


async def main():
    targets = periods(datetime.now(dr.JST).date())
    if not targets:
        print("今日はまとめの日ではありません")
        return

    client = anthropic.AsyncAnthropic()
    bot_id = dr.bot_id()
    failed = False
    for channel_id, owner_id in dr.find_owners().items():
        for start, end, label in targets:
            try:
                entries, started_before = collect(channel_id, owner_id, bot_id, start, end)
                # 日記を始める前の期間のまとめは出さない
                if not entries and not started_before:
                    print(f"#{channel_id}: {label} は日記開始前のためスキップ")
                    continue
                text = await build(client, entries, start, end, label)
                for chunk in split_message(text):
                    dr.post(channel_id, {"content": chunk, "allowed_mentions": {"parse": []}})
                print(f"#{channel_id}: {label}（{start}〜{end}）を投稿、{len(entries)}日分")
            except Exception as e:
                # 一人分の失敗で他の人のまとめが止まらないようにする
                print(f"#{channel_id}: {label} 失敗 {e!r}", file=sys.stderr)
                failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
