from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    priority        INTEGER NOT NULL DEFAULT 1,
    dependencies    TEXT NOT NULL DEFAULT '[]',
    claude_session  TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    agent_type  TEXT NOT NULL,
    task_id     INTEGER,
    status      TEXT NOT NULL DEFAULT 'running',
    pid         INTEGER,
    log_path    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    claude_session TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);
"""


class Database:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        # Migrations
        try:
            await self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN claude_session TEXT"
            )
        except Exception:
            pass
        # Drop old messages table if it lacks conversation_id
        try:
            await self._conn.execute(
                "SELECT conversation_id FROM messages LIMIT 1"
            )
        except Exception:
            await self._conn.execute("DROP TABLE IF EXISTS messages")
            await self._conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized"
        return self._conn

    # --- Tasks ---

    async def create_task(
        self,
        title: str,
        description: str = "",
        priority: int = 1,
        dependencies: list[int] | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.conn.execute(
            "INSERT INTO tasks (title, description, priority, dependencies, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, description, priority, json.dumps(dependencies or []), now),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_task(self, task_id: int) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            d = dict(row)
            d["dependencies"] = json.loads(d["dependencies"])
            return d

    async def update_task(self, task_id: int, **kwargs: object) -> None:
        allowed = {"title", "description", "status", "priority", "dependencies", "claude_session", "updated_at"}
        bad = set(kwargs) - allowed
        if bad:
            raise ValueError(f"Invalid task columns: {bad}")
        if "dependencies" in kwargs and isinstance(kwargs["dependencies"], list):
            kwargs["dependencies"] = json.dumps(kwargs["dependencies"])
        kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(task_id)
        await self.conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
        await self.conn.commit()

    async def list_tasks(self, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM tasks"
        params: list[object] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY priority, id"
        async with self.conn.execute(query, params) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
            for r in rows:
                r["dependencies"] = json.loads(r["dependencies"])
            return rows

    async def pick_next_task(self) -> dict | None:
        """Find the next task to work on. Interrupted tasks resume first."""
        # Interrupted tasks have a Claude session to resume — pick them first
        interrupted = await self.list_tasks(status="interrupted")
        if interrupted:
            return interrupted[0]

        open_tasks = await self.list_tasks(status="open")
        completed = await self.list_tasks(status="completed")
        completed_ids = {t["id"] for t in completed}
        for task in open_tasks:
            if all(d in completed_ids for d in task["dependencies"]):
                return task
        return None

    # --- Sessions ---

    async def create_session(
        self,
        agent_type: str,
        task_id: int | None = None,
        log_path: str = "",
        session_id: str | None = None,
    ) -> str:
        if session_id is None:
            session_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            "INSERT INTO sessions (id, agent_type, task_id, log_path, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, agent_type, task_id, log_path, now),
        )
        await self.conn.commit()
        return session_id

    async def get_session(self, session_id: str) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    _SESSION_COLUMNS = {"status", "pid", "ended_at"}

    async def update_session(self, session_id: str, **kwargs: object) -> None:
        bad = set(kwargs) - self._SESSION_COLUMNS
        if bad:
            raise ValueError(f"Invalid session columns: {bad}")
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(session_id)
        await self.conn.execute(f"UPDATE sessions SET {sets} WHERE id = ?", vals)
        await self.conn.commit()

    async def list_sessions(
        self, status: str | None = None, agent_type: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM sessions"
        params: list[object] = []
        clauses: list[str] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if agent_type:
            clauses.append("agent_type = ?")
            params.append(agent_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at DESC"
        async with self.conn.execute(query, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def latest_session_for_task(self, task_id: int) -> dict | None:
        """Return the most recent session for a given task."""
        async with self.conn.execute(
            "SELECT * FROM sessions WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def recover_interrupted_tasks(self) -> int:
        """Mark in_progress tasks as interrupted and backfill session IDs for resume."""
        now = datetime.now(timezone.utc).isoformat()

        # Find in_progress tasks that are missing a claude_session
        async with self.conn.execute(
            "SELECT t.id, s.log_path FROM tasks t "
            "LEFT JOIN sessions s ON s.task_id = t.id "
            "WHERE t.status = 'in_progress' AND t.claude_session IS NULL "
            "ORDER BY s.started_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            task_id, log_path = row["id"], row["log_path"]
            if log_path:
                claude_sid = self._extract_claude_session(log_path)
                if claude_sid:
                    await self.conn.execute(
                        "UPDATE tasks SET claude_session = ? WHERE id = ?",
                        (claude_sid, task_id),
                    )

        cursor = await self.conn.execute(
            "UPDATE tasks SET status = 'interrupted', updated_at = ? "
            "WHERE status = 'in_progress'",
            (now,),
        )
        await self.conn.commit()
        return cursor.rowcount

    @staticmethod
    def _extract_claude_session(log_path: str) -> str | None:
        """Read a stream-json log file and extract the Claude session ID."""
        try:
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if "session_id" in data:
                        return data["session_id"]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return None

    async def mark_stale_running(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.conn.execute(
            "UPDATE sessions SET status = 'interrupted', ended_at = ? "
            "WHERE status = 'running'",
            (now,),
        )
        await self.conn.commit()
        return cursor.rowcount

    # --- Conversations ---

    async def create_conversation(self, title: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.conn.execute(
            "INSERT INTO conversations (title, created_at) VALUES (?, ?)",
            (title, now),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_conversation(self, conv_id: int) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conv_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_conversation(self, conv_id: int, **kwargs: object) -> None:
        allowed = {"title", "claude_session"}
        bad = set(kwargs) - allowed
        if bad:
            raise ValueError(f"Invalid conversation columns: {bad}")
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(conv_id)
        await self.conn.execute(
            f"UPDATE conversations SET {sets} WHERE id = ?", vals
        )
        await self.conn.commit()

    async def list_conversations(self) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM conversations ORDER BY created_at DESC"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def add_message(
        self, conversation_id: int, role: str, content: str,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, now),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def list_messages(self, conversation_id: int) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]
