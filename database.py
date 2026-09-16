import os
import aiosqlite
from datetime import datetime
from config import DATABASE_PATH

class Database:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path

    async def init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sudo_users (
                    user_id INTEGER PRIMARY KEY,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS active_jobs (
                    chat_id INTEGER PRIMARY KEY,
                    started_by INTEGER,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    chat_id INTEGER,
                    action TEXT,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    async def add_sudo(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("INSERT INTO sudo_users (user_id) VALUES (?)", (user_id,))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_sudo(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("DELETE FROM sudo_users WHERE user_id = ?", (user_id,)) as cursor:
                await db.commit()
                return cursor.rowcount > 0

    async def get_sudo_list(self) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT user_id FROM sudo_users") as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

    async def is_sudo(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM sudo_users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row is not None

    async def set_job_active(self, chat_id: int, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("INSERT INTO active_jobs (chat_id, started_by) VALUES (?, ?)", (chat_id, user_id))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def set_job_inactive(self, chat_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM active_jobs WHERE chat_id = ?", (chat_id,))
            await db.commit()

    async def is_job_active(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM active_jobs WHERE chat_id = ?", (chat_id,)) as cursor:
                row = await cursor.fetchone()
                return row is not None

    async def log_action(self, user_id: int, chat_id: int, action: str, details: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO audit_logs (user_id, chat_id, action, details) VALUES (?, ?, ?, ?)",
                (user_id, chat_id, action, details)
            )
            await db.commit()

db = Database()
