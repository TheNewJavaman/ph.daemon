from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    agent_type  TEXT NOT NULL,
    issue_id    INTEGER,
    status      TEXT NOT NULL DEFAULT 'running',
    pid         INTEGER,
    log_path    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    number      INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    state       TEXT NOT NULL,
    labels      TEXT NOT NULL DEFAULT '[]',
    assignee    TEXT,
    comments    TEXT NOT NULL DEFAULT '[]',
    synced_at   TEXT NOT NULL
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
        await self._conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized"
        return self._conn

    # --- Sessions ---

    async def create_session(
        self,
        agent_type: str,
        issue_id: int | None,
        log_path: str,
        session_id: str | None = None,
    ) -> str:
        if session_id is None:
            session_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            "INSERT INTO sessions (id, agent_type, issue_id, log_path, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, agent_type, issue_id, log_path, now),
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
        bad_keys = set(kwargs) - self._SESSION_COLUMNS
        if bad_keys:
            raise ValueError(f"Invalid session columns: {bad_keys}")
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(session_id)
        await self.conn.execute(
            f"UPDATE sessions SET {sets} WHERE id = ?", vals
        )
        await self.conn.commit()

    async def list_sessions(
        self,
        status: str | None = None,
        agent_type: str | None = None,
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

    # --- Issues (write-through cache) ---

    async def upsert_issue(
        self,
        number: int,
        title: str,
        body: str,
        state: str,
        labels: list[str],
        assignee: str | None,
        comments: list[dict],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            "INSERT INTO issues (number, title, body, state, labels, assignee, comments, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(number) DO UPDATE SET "
            "title=?, body=?, state=?, labels=?, assignee=?, comments=?, synced_at=?",
            (
                number, title, body, state, json.dumps(labels),
                assignee, json.dumps(comments), now,
                title, body, state, json.dumps(labels),
                assignee, json.dumps(comments), now,
            ),
        )
        await self.conn.commit()

    async def get_issue(self, number: int) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM issues WHERE number = ?", (number,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            d = dict(row)
            d["labels"] = json.loads(d["labels"])
            d["comments"] = json.loads(d["comments"])
            return d

    async def list_issues(self, state: str | None = None) -> list[dict]:
        query = "SELECT * FROM issues"
        params: list[object] = []
        if state:
            query += " WHERE state = ?"
            params.append(state)
        query += " ORDER BY number"
        async with self.conn.execute(query, params) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
            for r in rows:
                r["labels"] = json.loads(r["labels"])
                r["comments"] = json.loads(r["comments"])
            return rows
