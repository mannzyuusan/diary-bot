import sqlite3
from datetime import date, datetime, timedelta


class DiaryDB:
    def __init__(self, path: str, legacy_owner_id: int | None = None):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                message_id INTEGER PRIMARY KEY,
                diary_date TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL,
                user_id    INTEGER
            );
            CREATE TABLE IF NOT EXISTS images (
                path       TEXT PRIMARY KEY,  -- BASE_DIR からの相対パス
                message_id INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                diary_date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summaries (
                period_key TEXT PRIMARY KEY,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # 一人用だった頃のDBに user_id 列を追加し、既存の日記を元の持ち主に割り当てる
        columns = [r[1] for r in self.conn.execute("PRAGMA table_info(entries)")]
        if "user_id" not in columns:
            self.conn.execute("ALTER TABLE entries ADD COLUMN user_id INTEGER")
        if legacy_owner_id:
            self.conn.execute("UPDATE entries SET user_id = ? WHERE user_id IS NULL", (legacy_owner_id,))
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_user_date ON entries(user_id, diary_date)")
        self.conn.commit()

    # --- 日記 ---
    def upsert(self, message_id: int, user_id: int, diary_date: date, content: str, created_at: datetime):
        self.conn.execute(
            "INSERT OR REPLACE INTO entries (message_id, diary_date, content, created_at, user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, diary_date.isoformat(), content, created_at.isoformat(), user_id),
        )
        self.conn.commit()

    def update_content(self, message_id: int, content: str):
        self.conn.execute("UPDATE entries SET content = ? WHERE message_id = ?", (content, message_id))
        self.conn.commit()

    def delete(self, message_id: int):
        self.conn.execute("DELETE FROM entries WHERE message_id = ?", (message_id,))
        self.conn.commit()

    def has_entry(self, user_id: int, d: date) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM entries WHERE user_id = ? AND diary_date = ? LIMIT 1", (user_id, d.isoformat())
        ).fetchone()
        return row is not None

    def last_message_id(self, user_id: int) -> int | None:
        row = self.conn.execute("SELECT MAX(message_id) FROM entries WHERE user_id = ?", (user_id,)).fetchone()
        return row[0]

    def entries_between(self, user_id: int, start: date, end: date) -> list[tuple[str, str]]:
        """start〜end（両端含む）の日記を [(日付, 本文), ...] で返す。同じ日の投稿はまとめる。"""
        rows = self.conn.execute(
            "SELECT diary_date, content FROM entries WHERE user_id = ? AND diary_date BETWEEN ? AND ? "
            "ORDER BY diary_date, created_at",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        merged: dict[str, list[str]] = {}
        for d, c in rows:
            merged.setdefault(d, []).append(c)
        return [(d, "\n".join(cs)) for d, cs in merged.items()]

    def count_days(self, user_id: int, start: date, end: date) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT diary_date) FROM entries WHERE user_id = ? AND diary_date BETWEEN ? AND ?",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchone()
        return row[0]

    def streak(self, user_id: int, today: date) -> int:
        """連続記入日数。今日が未記入なら昨日から数える。"""
        written = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT diary_date FROM entries WHERE user_id = ?", (user_id,)
        )}
        d = today if today.isoformat() in written else today - timedelta(days=1)
        n = 0
        while d.isoformat() in written:
            n += 1
            d -= timedelta(days=1)
        return n

    # --- 画像 ---
    def add_image(self, path: str, message_id: int, user_id: int, diary_date: date):
        self.conn.execute(
            "INSERT OR REPLACE INTO images VALUES (?, ?, ?, ?)",
            (path, message_id, user_id, diary_date.isoformat()),
        )
        self.conn.commit()

    def images_between(self, user_id: int, start: date, end: date) -> list[tuple[str, str]]:
        """[(日付, 相対パス), ...]"""
        return self.conn.execute(
            "SELECT diary_date, path FROM images WHERE user_id = ? AND diary_date BETWEEN ? AND ? "
            "ORDER BY diary_date, message_id",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()

    def delete_images(self, message_id: int) -> list[str]:
        """メッセージに付いていた画像の記録を消し、そのパスを返す。"""
        paths = [r[0] for r in self.conn.execute("SELECT path FROM images WHERE message_id = ?", (message_id,))]
        self.conn.execute("DELETE FROM images WHERE message_id = ?", (message_id,))
        self.conn.commit()
        return paths

    # --- まとめ ---
    def has_summary(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM summaries WHERE period_key = ?", (key,)).fetchone() is not None

    def save_summary(self, key: str, content: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO summaries VALUES (?, ?, ?)",
            (key, content, datetime.now().isoformat()),
        )
        self.conn.commit()
