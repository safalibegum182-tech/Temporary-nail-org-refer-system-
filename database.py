# ============================================================
# database.py
# TEMP MAIL BOT DATABASE
# SQLite database (Clean & Fixed)
# ============================================================

import sqlite3
from pathlib import Path
from typing import Optional, Dict, List, Any

# ============================================================
# SETTINGS
# ============================================================

DB_FILE = Path(__file__).with_name("temp_mail.db")

# ============================================================
# CONNECTION
# ============================================================

def get_connection():
    conn = sqlite3.connect(
        str(DB_FILE),
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row

    # Better reliability when multiple bot tasks access SQLite
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    return conn


# ============================================================
# INIT DATABASE
# ============================================================

def init_db():
    conn = get_connection()

    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                language TEXT DEFAULT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS mailboxes (
                user_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                token TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_users_language
                ON users(language);
            """
        )

        # Safe migration for older databases
        _add_column_if_missing(
            conn, "users", "language", "TEXT DEFAULT NULL"
        )
        _add_column_if_missing(
            conn, "users", "created_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )
        _add_column_if_missing(
            conn, "users", "updated_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )

        conn.commit()

    finally:
        conn.close()


def _add_column_if_missing(conn, table_name: str, column_name: str, definition: str):
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing = {row["name"] for row in columns}

    if column_name not in existing:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


# ============================================================
# USER
# ============================================================

def save_user(user_id: int, username: Optional[str] = None):
    conn = get_connection()

    try:
        row = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row:
            conn.execute(
                """
                UPDATE users
                SET username = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (username, user_id)
            )
        else:
            conn.execute(
                """
                INSERT INTO users (user_id, username)
                VALUES (?, ?)
                """,
                (user_id, username)
            )

        conn.commit()

    finally:
        conn.close()


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()

    try:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        return dict(row) if row else None

    finally:
        conn.close()


def get_all_users() -> List[int]:
    conn = get_connection()

    try:
        rows = conn.execute(
            "SELECT user_id FROM users ORDER BY user_id ASC"
        ).fetchall()

        return [int(row["user_id"]) for row in rows]

    finally:
        conn.close()


# ============================================================
# LANGUAGE
# ============================================================

def get_language(user_id: int) -> Optional[str]:
    conn = get_connection()

    try:
        row = conn.execute(
            "SELECT language FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not row:
            return None

        return row["language"]

    finally:
        conn.close()


def set_language(user_id: int, language: str):
    save_user(user_id)

    conn = get_connection()

    try:
        conn.execute(
            """
            UPDATE users
            SET language = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (language, user_id)
        )
        conn.commit()

    finally:
        conn.close()


# ============================================================
# MAILBOX
# ============================================================

def save_mailbox(user_id: int, email: str, token: str):
    save_user(user_id)

    conn = get_connection()

    try:
        conn.execute(
            """
            INSERT INTO mailboxes (user_id, email, token)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                email = excluded.email,
                token = excluded.token,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, email, token)
        )
        conn.commit()

    finally:
        conn.close()


def get_mailbox(user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()

    try:
        row = conn.execute(
            """
            SELECT user_id, email, token, created_at, updated_at
            FROM mailboxes
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

        return dict(row) if row else None

    finally:
        conn.close()


def delete_mailbox(user_id: int):
    conn = get_connection()

    try:
        conn.execute(
            "DELETE FROM mailboxes WHERE user_id = ?",
            (user_id,)
        )
        conn.commit()

    finally:
        conn.close()


# ============================================================
# AUTO INIT
# ============================================================

if __name__ == "__main__":
    init_db()
    print("✅ Database initialized successfully.")
    print(f"📁 Database: {DB_FILE}")
