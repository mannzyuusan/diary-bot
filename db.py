import sqlite3
from datetime import date, datetime, timedelta


class DiaryDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                message_id INTEGER PRIMARY KEY,
                diary_date TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(diary_date);
            CREATE TABLE IF NOT EXISTS summaries (
                period_key TEXT PRIMARY KEY,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # --- 日記 ---
    def upsert(self, message_id: int, diary_date: date, content: str, created_at: datetime):
        self.conn.execute(
            "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?)",
            (message_id, diary_date.isoformat(), content, created_at.isoformat()),
        )
        self.conn.commit()

    def update_content(self, message_id: int, content: str):
        self.conn.execute("UPDATE entries SET content = ? WHERE message_id = ?", (content, message_id))
        self.conn.commit()

    def delete(self, message_id: int):
        self.conn.execute("DELETE FROM entries WHERE message_id = ?", (message_id,))
        self.conn.commit()

    def has_entry(self, d: date) -> bool:
        row = self.conn.execute("SELECT 1 FROM entries WHERE diary_date = ? LIMIT 1", (d.isoformat(),)).fetchone()
        return row is not None

    def last_message_id(self) -> int | None:
        row = self.conn.execute("SELECT MAX(message_id) FROM entries").fetchone()
        return row[0]

    def entries_between(self, start: date, end: date) -> list[tuple[str, str]]:
        """start〜end（両端含む）の日記を [(日付, 本文), ...] で返す。同じ日の投稿はまとめる。"""
        rows = self.conn.execute(
            "SELECT diary_date, content FROM entries WHERE diary_date BETWEEN ? AND ? "
            "ORDER BY diary_date, created_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        merged: dict[str, list[str]] = {}
        for d, c in rows:
            merged.setdefault(d, []).append(c)
        return [(d, "\n".join(cs)) for d, cs in merged.items()]

    def count_days(self, start: date, end: date) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT diary_date) FROM entries WHERE diary_date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        return row[0]

    def streak(self, today: date) -> int:
        """連続記入日数。今日が未記入なら昨日から数える。"""
        written = {r[0] for r in self.conn.execute("SELECT DISTINCT diary_date FROM entries")}
        d = today if today.isoformat() in written else today - timedelta(days=1)
        n = 0
        while d.isoformat() in written:
            n += 1
            d -= timedelta(days=1)
        return n

    # --- まとめ ---
    def has_summary(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM summaries WHERE period_key = ?", (key,)).fetchone() is not None

    def save_summary(self, key: str, content: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO summaries VALUES (?, ?, ?)",
            (key, content, datetime.now().isoformat()),
        )
        self.conn.commit()
