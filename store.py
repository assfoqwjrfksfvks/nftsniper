"""SQLite cursors, notification deduplication and durable delivery queue."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cursors (
                slug TEXT PRIMARY KEY, number INTEGER NOT NULL CHECK(number >= 0),
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY, slug TEXT NOT NULL, number INTEGER NOT NULL,
                gift TEXT NOT NULL, reason TEXT NOT NULL, text TEXT NOT NULL,
                reply_markup TEXT NOT NULL,
                found_at REAL NOT NULL, sent_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                UNIQUE(slug, number)
            );
            CREATE TABLE IF NOT EXISTS gaps (
                slug TEXT NOT NULL, number INTEGER NOT NULL,
                first_seen_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
                PRIMARY KEY(slug, number)
            );
            CREATE INDEX IF NOT EXISTS gap_retry ON gaps(slug, next_attempt_at);
            CREATE INDEX IF NOT EXISTS notification_delivery
                ON notifications(sent_at, next_attempt_at, id);
        """)

    def get_cursor(self, slug: str) -> int | None:
        row = self.db.execute("SELECT number FROM cursors WHERE slug=?", (slug,)).fetchone()
        return row["number"] if row else None

    def set_cursor(self, slug: str, number: int) -> None:
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ValueError("Номер курсора должен быть неотрицательным целым числом.")
        with self.db:
            self.db.execute("""INSERT INTO cursors(slug, number, updated_at) VALUES(?,?,?)
                ON CONFLICT(slug) DO UPDATE SET number=excluded.number, updated_at=excluded.updated_at""",
                (slug, number, time.time()))

    def enqueue(self, gift_dict: dict, reason: str, text: str, reply_markup: dict) -> bool:
        slug, number = gift_dict["slug"], gift_dict["number"]
        if not isinstance(slug, str) or not slug or isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("У подарка должны быть slug и положительный целый number.")
        gift = json.dumps(gift_dict, ensure_ascii=False, allow_nan=False)
        markup = json.dumps(reply_markup, ensure_ascii=False, allow_nan=False)
        with self.db:
            result = self.db.execute("""INSERT INTO notifications
                (slug,number,gift,reason,text,reply_markup,found_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(slug,number) DO NOTHING""",
                (slug, number, gift, reason, text, markup, time.time()))
        return result.rowcount == 1

    def has_seen(self, slug: str, number: int) -> bool:
        return self.db.execute("SELECT 1 FROM notifications WHERE slug=? AND number=?",
                               (slug, number)).fetchone() is not None

    def add_gap(self, slug: str, number: int) -> None:
        now = time.time()
        with self.db:
            self.db.execute("""INSERT INTO gaps(slug,number,first_seen_at,next_attempt_at)
                VALUES(?,?,?,?) ON CONFLICT(slug,number) DO NOTHING""",
                (slug, number, now, now + 30))

    def due_gaps(self, slug: str, limit: int = 5) -> list[int]:
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM gaps WHERE first_seen_at < ?", (now - 86400,))
        return [row["number"] for row in self.db.execute("""SELECT number FROM gaps
            WHERE slug=? AND next_attempt_at <= ? ORDER BY next_attempt_at, number LIMIT ?""",
            (slug, now, max(1, min(int(limit), 100)))).fetchall()]

    def defer_gap(self, slug: str, number: int) -> None:
        with self.db:
            self.db.execute("UPDATE gaps SET next_attempt_at=? WHERE slug=? AND number=?",
                            (time.time() + 120, slug, number))

    def remove_gap(self, slug: str, number: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM gaps WHERE slug=? AND number=?", (slug, number))

    def pending(self, limit: int = 10) -> list[dict]:
        rows = self.db.execute("""SELECT * FROM notifications
            WHERE sent_at IS NULL AND next_attempt_at <= ? ORDER BY id LIMIT ?""",
            (time.time(), max(1, min(int(limit), 100)))).fetchall()
        return [{"id": row["id"], "text": row["text"],
                 "reply_markup": json.loads(row["reply_markup"]),
                 "gift": json.loads(row["gift"]), "reason": row["reason"],
                 "attempts": row["attempts"]} for row in rows]

    def mark_sent(self, notification_id: int) -> None:
        with self.db:
            self.db.execute("""UPDATE notifications SET sent_at=?, last_error=''
                WHERE id=? AND sent_at IS NULL""", (time.time(), notification_id))

    def mark_failed(self, notification_id: int, error: str, retry_after: float = 30) -> None:
        row = self.db.execute("SELECT attempts FROM notifications WHERE id=? AND sent_at IS NULL",
                              (notification_id,)).fetchone()
        if row is None:
            return
        try:
            base_delay = float(retry_after)
            if not math.isfinite(base_delay):
                raise ValueError
        except (TypeError, ValueError):
            base_delay = 30
        delay = max(max(1, base_delay), min(300, max(1, base_delay) * 2 ** min(row["attempts"], 20)))
        with self.db:
            self.db.execute("""UPDATE notifications SET attempts=attempts+1,
                next_attempt_at=?, last_error=? WHERE id=? AND sent_at IS NULL""",
                (time.time() + delay, str(error)[:1000], notification_id))

    def stats(self) -> dict:
        row = self.db.execute("""SELECT COUNT(*) AS total,
            COALESCE(SUM(sent_at IS NULL),0) AS pending,
            COALESCE(SUM(sent_at IS NOT NULL),0) AS sent,
            COALESCE(SUM(sent_at IS NULL AND attempts>0),0) AS failed FROM notifications""").fetchone()
        result = dict(row)
        result["seen"] = result["total"]
        return result

    def recent(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?",
                              (max(1, min(int(limit), 1000)),)).fetchall()
        result = []
        for row in rows:
            gift = json.loads(row["gift"])
            status = "sent" if row["sent_at"] is not None else "pending"
            gift.update(reason=row["reason"], delivery_status=status, status=status,
                        attempts=row["attempts"], found_at=row["found_at"],
                        sent_at=row["sent_at"], last_error=row["last_error"])
            result.append(gift)
        return result

    def close(self) -> None:
        self.db.close()
