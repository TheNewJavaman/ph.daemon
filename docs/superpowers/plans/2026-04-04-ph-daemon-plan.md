# ph.daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone Python tool that orchestrates automated research via the `claude` CLI, with GitHub issue tracking, real-time paper writing, and a web control plane.

**Architecture:** FastAPI web server managing `claude` CLI subprocesses. SQLite for session tracking + write-through issue cache. GitHub issues as source of truth for tasks. Four agent types (planner, implementor, paper writer, ephemeral) with scoped permissions. CLI (`phd`) for human interaction.

**Tech Stack:** Python 3.12+, uv, FastAPI, Jinja2, htmx, SQLite (aiosqlite), SSE (sse-starlette), Click, `claude` CLI, `gh` CLI.

---

## File Structure

```
ph.daemon/
├── pyproject.toml
├── CLAUDE.md
├── src/
│   └── daemon/
│       ├── __init__.py             # Version string
│       ├── app.py                  # FastAPI application factory
│       ├── config.py               # Project config (paths, repo name)
│       ├── db.py                   # SQLite schema, session + issue queries
│       ├── agents/
│       │   ├── __init__.py         # AgentType enum, re-exports
│       │   ├── base.py             # BaseAgent: subprocess lifecycle, log capture
│       │   ├── planner.py          # PlannerAgent: decompose tasks into issues
│       │   ├── implementor.py      # ImplementorAgent: main loop, issue pickup
│       │   ├── paper.py            # PaperAgent: update paper on new commits
│       │   └── ephemeral.py        # EphemeralAgent: read-only Q&A
│       ├── github/
│       │   ├── __init__.py         # Re-exports
│       │   ├── issues.py           # Issue CRUD via gh CLI, dependency parsing
│       │   └── hooks.py            # Post-commit hook: parse commit, write discussion
│       ├── web/
│       │   ├── __init__.py         # Re-exports
│       │   ├── routes.py           # All FastAPI route handlers
│       │   ├── sse.py              # SSE endpoint for log streaming
│       │   └── templates/
│       │       ├── base.html       # Base layout with nav, htmx
│       │       ├── dashboard.html  # Dashboard: agents, issues, commits
│       │       ├── agents.html     # Agent session list
│       │       ├── session.html    # Single session: live log stream
│       │       ├── issues.html     # Issue list + dependency DAG
│       │       ├── paper.html      # PDF viewer + diff
│       │       └── constraints.html # Constraint list + add form
│       └── cli.py                  # Click CLI: phd init/start/task/constrain/ask/...
├── prompts/
│   ├── implementor.md              # System prompt for implementor agent
│   ├── planner.md                  # System prompt for planner agent
│   ├── paper.md                    # System prompt for paper writer agent
│   ├── ephemeral.md                # System prompt for ephemeral agent
│   └── post_commit.md              # System prompt for post-commit discussion
└── tests/
    ├── conftest.py                 # Shared fixtures (tmp project dir, mock gh)
    ├── test_db.py                  # Database layer tests
    ├── test_github.py              # GitHub integration tests
    ├── test_agents.py              # Agent lifecycle tests
    ├── test_cli.py                 # CLI command tests
    └── test_web.py                 # Web route tests
```

---

### Task 1: Project Scaffolding + Database Layer

**Files:**
- Create: `pyproject.toml`
- Create: `src/daemon/__init__.py`
- Create: `src/daemon/config.py`
- Create: `src/daemon/db.py`
- Create: `tests/conftest.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "ph-daemon"
version = "0.1.0"
description = "Automated research harness orchestrating claude CLI agents"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "jinja2>=3.1",
    "sse-starlette>=2.0",
    "click>=8.1",
    "aiosqlite>=0.20",
    "httpx>=0.28",
]

[project.scripts]
phd = "daemon.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/daemon"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-httpx>=0.35",
    "ruff>=0.9",
]
```

- [ ] **Step 2: Create package init**

`src/daemon/__init__.py`:
```python
__version__ = "0.1.0"
```

- [ ] **Step 3: Create config module**

`src/daemon/config.py`:
```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectConfig:
    """Configuration for a target research project."""

    project_dir: Path
    repo: str  # "owner/repo" format

    @property
    def daemon_dir(self) -> Path:
        return self.project_dir / ".ph.daemon"

    @property
    def db_path(self) -> Path:
        return self.daemon_dir / "daemon.db"

    @property
    def logs_dir(self) -> Path:
        return self.daemon_dir / "logs"

    @property
    def constraints_path(self) -> Path:
        return self.project_dir / "docs" / "constraints.md"

    @property
    def paper_dir(self) -> Path:
        return self.project_dir / "paper"

    @classmethod
    def load(cls, project_dir: Path) -> ProjectConfig:
        """Load config from .ph.daemon/config.json."""
        config_path = project_dir / ".ph.daemon" / "config.json"
        data = json.loads(config_path.read_text())
        return cls(project_dir=project_dir, repo=data["repo"])

    def save(self) -> None:
        """Save config to .ph.daemon/config.json."""
        config_path = self.daemon_dir / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"repo": self.repo}, indent=2))
```

- [ ] **Step 4: Write failing tests for the database layer**

`tests/conftest.py`:
```python
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from daemon.config import ProjectConfig
from daemon.db import Database


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a temporary project directory."""
    d = tmp_path / "test-project"
    d.mkdir()
    (d / ".ph.daemon").mkdir()
    (d / ".ph.daemon" / "logs").mkdir()
    return d


@pytest.fixture
def config(project_dir: Path) -> ProjectConfig:
    return ProjectConfig(project_dir=project_dir, repo="test-owner/test-repo")


@pytest.fixture
async def db(config: ProjectConfig) -> Database:
    database = Database(config.db_path)
    await database.init()
    yield database
    await database.close()
```

`tests/test_db.py`:
```python
from __future__ import annotations

import pytest

from daemon.db import Database


@pytest.mark.asyncio
async def test_create_session(db: Database) -> None:
    session_id = await db.create_session(
        agent_type="implementor",
        issue_id=42,
        log_path="/tmp/test.jsonl",
    )
    assert session_id is not None

    session = await db.get_session(session_id)
    assert session["agent_type"] == "implementor"
    assert session["issue_id"] == 42
    assert session["status"] == "running"
    assert session["pid"] is None


@pytest.mark.asyncio
async def test_update_session_status(db: Database) -> None:
    session_id = await db.create_session(
        agent_type="ephemeral",
        issue_id=None,
        log_path="/tmp/test.jsonl",
    )
    await db.update_session(session_id, status="completed", pid=1234)

    session = await db.get_session(session_id)
    assert session["status"] == "completed"
    assert session["pid"] == 1234


@pytest.mark.asyncio
async def test_list_sessions_by_status(db: Database) -> None:
    await db.create_session("implementor", 1, "/tmp/a.jsonl")
    s2 = await db.create_session("planner", 2, "/tmp/b.jsonl")
    await db.update_session(s2, status="completed")

    running = await db.list_sessions(status="running")
    assert len(running) == 1
    assert running[0]["agent_type"] == "implementor"

    completed = await db.list_sessions(status="completed")
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_upsert_issue(db: Database) -> None:
    await db.upsert_issue(
        number=10,
        title="Add caching",
        body="## Task\nImplement LRU cache",
        state="open",
        labels=["ph:ready", "ph:implementor"],
        assignee=None,
        comments=[],
    )
    issue = await db.get_issue(10)
    assert issue["title"] == "Add caching"
    assert issue["state"] == "open"


@pytest.mark.asyncio
async def test_upsert_issue_updates_existing(db: Database) -> None:
    await db.upsert_issue(10, "v1", "body", "open", [], None, [])
    await db.upsert_issue(10, "v2", "body", "closed", [], None, [])

    issue = await db.get_issue(10)
    assert issue["title"] == "v2"
    assert issue["state"] == "closed"


@pytest.mark.asyncio
async def test_list_open_issues(db: Database) -> None:
    await db.upsert_issue(1, "open one", "", "open", ["ph:ready"], None, [])
    await db.upsert_issue(2, "closed one", "", "closed", ["ph:done"], None, [])
    await db.upsert_issue(3, "open two", "", "open", ["ph:blocked"], None, [])

    open_issues = await db.list_issues(state="open")
    assert len(open_issues) == 2
    assert {i["number"] for i in open_issues} == {1, 3}
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `cd /home/gpizarro/dev/ph.daemon && uv run pytest tests/test_db.py -v`
Expected: ImportError — `daemon.db` does not exist yet.

- [ ] **Step 6: Implement the database layer**

`src/daemon/db.py`:
```python
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
    ) -> str:
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

    async def update_session(self, session_id: str, **kwargs: object) -> None:
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
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd /home/gpizarro/dev/ph.daemon && uv run pytest tests/test_db.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/ tests/
git commit -m "feat: project scaffolding + SQLite database layer

Sessions table for local agent state, issues table as write-through
cache of GitHub issues with denormalized comments. #1"
```

---

### Task 2: GitHub Integration

**Files:**
- Create: `src/daemon/github/__init__.py`
- Create: `src/daemon/github/issues.py`
- Create: `tests/test_github.py`

- [ ] **Step 1: Write failing tests for GitHub issue operations**

`tests/test_github.py`:
```python
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from daemon.github.issues import (
    GitHubIssues,
    parse_dependencies,
    resolve_dependency_dag,
)


def test_parse_dependencies_from_task_list() -> None:
    body = """## Dependencies
- [ ] #12
- [ ] #13
- [x] #14
"""
    blocked_by = parse_dependencies(body)
    assert blocked_by == [12, 13, 14]


def test_parse_dependencies_empty() -> None:
    body = "## Task\nDo something"
    assert parse_dependencies(body) == []


def test_resolve_dependency_dag_picks_unblocked() -> None:
    issues = [
        {"number": 1, "body": "", "state": "open", "assignee": None,
         "labels": ["ph:ready"]},
        {"number": 2, "body": "- [ ] #1", "state": "open", "assignee": None,
         "labels": ["ph:blocked"]},
        {"number": 3, "body": "", "state": "open", "assignee": None,
         "labels": ["ph:ready"]},
    ]
    closed = set()
    ready = resolve_dependency_dag(issues, closed)
    assert [i["number"] for i in ready] == [1, 3]


def test_resolve_dependency_dag_unblocks_when_dep_closed() -> None:
    issues = [
        {"number": 2, "body": "- [ ] #1", "state": "open", "assignee": None,
         "labels": ["ph:blocked"]},
    ]
    closed = {1}
    ready = resolve_dependency_dag(issues, closed)
    assert [i["number"] for i in ready] == [2]


def test_resolve_dependency_dag_skips_assigned() -> None:
    issues = [
        {"number": 1, "body": "", "state": "open", "assignee": "bot",
         "labels": ["ph:in-progress"]},
        {"number": 2, "body": "", "state": "open", "assignee": None,
         "labels": ["ph:ready"]},
    ]
    ready = resolve_dependency_dag(issues, set())
    assert [i["number"] for i in ready] == [2]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_github.py -v`
Expected: ImportError — `daemon.github.issues` does not exist yet.

- [ ] **Step 3: Implement GitHub issues module**

`src/daemon/github/__init__.py`:
```python
from daemon.github.issues import GitHubIssues, parse_dependencies, resolve_dependency_dag

__all__ = ["GitHubIssues", "parse_dependencies", "resolve_dependency_dag"]
```

`src/daemon/github/issues.py`:
```python
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from daemon.config import ProjectConfig
from daemon.db import Database

# Matches GitHub task list items: "- [ ] #123" or "- [x] #123"
_DEP_PATTERN = re.compile(r"- \[[ x]\] #(\d+)")


def parse_dependencies(body: str) -> list[int]:
    """Extract issue numbers from GitHub task list syntax in issue body."""
    return [int(m.group(1)) for m in _DEP_PATTERN.finditer(body)]


def resolve_dependency_dag(
    open_issues: list[dict],
    closed_numbers: set[int],
) -> list[dict]:
    """Return open, unassigned issues whose dependencies are all closed.

    Results are ordered by issue number (oldest first).
    """
    ready = []
    for issue in open_issues:
        if issue["assignee"] is not None:
            continue
        deps = parse_dependencies(issue.get("body", ""))
        if all(d in closed_numbers for d in deps):
            ready.append(issue)
    return sorted(ready, key=lambda i: i["number"])


@dataclass
class GitHubIssues:
    """Issue operations via the gh CLI, with write-through SQLite cache."""

    config: ProjectConfig
    db: Database

    async def _gh(self, *args: str) -> str:
        """Run a gh CLI command and return stdout."""
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            "--repo", self.config.repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.project_dir,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"gh {' '.join(args)} failed: {stderr.decode()}"
            )
        return stdout.decode()

    async def create(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int:
        """Create a GitHub issue and cache it locally. Returns issue number."""
        cmd = ["issue", "create", "--title", title, "--body", body]
        for label in labels or []:
            cmd.extend(["--label", label])
        # gh issue create outputs the URL; extract the number
        url = (await self._gh(*cmd)).strip()
        number = int(url.rstrip("/").split("/")[-1])
        await self.sync_issue(number)
        return number

    async def edit(
        self,
        number: int,
        body: str | None = None,
        title: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        assignee: str | None = None,
    ) -> None:
        """Edit an existing issue."""
        cmd = ["issue", "edit", str(number)]
        if body is not None:
            cmd.extend(["--body", body])
        if title is not None:
            cmd.extend(["--title", title])
        for label in add_labels or []:
            cmd.extend(["--add-label", label])
        for label in remove_labels or []:
            cmd.extend(["--remove-label", label])
        if assignee is not None:
            cmd.extend(["--add-assignee", assignee])
        await self._gh(*cmd)
        await self.sync_issue(number)

    async def comment(self, number: int, body: str) -> None:
        """Add a comment to an issue."""
        await self._gh("issue", "comment", str(number), "--body", body)
        await self.sync_issue(number)

    async def close(self, number: int) -> None:
        """Close an issue."""
        await self._gh("issue", "close", str(number))
        await self.sync_issue(number)

    async def sync_issue(self, number: int) -> dict:
        """Fetch a single issue from GitHub and update local cache."""
        raw = await self._gh(
            "issue", "view", str(number),
            "--json", "number,title,body,state,labels,assignees,comments",
        )
        data = json.loads(raw)
        labels = [l["name"] for l in data.get("labels", [])]
        assignees = data.get("assignees", [])
        assignee = assignees[0]["login"] if assignees else None
        comments = [
            {
                "author": c.get("author", {}).get("login", "unknown"),
                "body": c.get("body", ""),
                "created_at": c.get("createdAt", ""),
            }
            for c in data.get("comments", [])
        ]
        await self.db.upsert_issue(
            number=data["number"],
            title=data["title"],
            body=data.get("body", ""),
            state=data["state"].lower(),
            labels=labels,
            assignee=assignee,
            comments=comments,
        )
        return await self.db.get_issue(data["number"])

    async def sync_all(self) -> None:
        """Fetch all ph:* issues from GitHub and update local cache."""
        raw = await self._gh(
            "issue", "list",
            "--label", "ph:",
            "--state", "all",
            "--limit", "500",
            "--json", "number,title,body,state,labels,assignees,comments",
        )
        for data in json.loads(raw):
            labels = [l["name"] for l in data.get("labels", [])]
            assignees = data.get("assignees", [])
            assignee = assignees[0]["login"] if assignees else None
            comments = [
                {
                    "author": c.get("author", {}).get("login", "unknown"),
                    "body": c.get("body", ""),
                    "created_at": c.get("createdAt", ""),
                }
                for c in data.get("comments", [])
            ]
            await self.db.upsert_issue(
                number=data["number"],
                title=data["title"],
                body=data.get("body", ""),
                state=data["state"].lower(),
                labels=labels,
                assignee=assignee,
                comments=comments,
            )

    async def pick_next_issue(self) -> dict | None:
        """Find the next unblocked, unassigned issue to work on."""
        open_issues = await self.db.list_issues(state="open")
        closed_issues = await self.db.list_issues(state="closed")
        closed_numbers = {i["number"] for i in closed_issues}
        ready = resolve_dependency_dag(open_issues, closed_numbers)
        return ready[0] if ready else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_github.py -v`
Expected: All 5 tests PASS (pure function tests; the `GitHubIssues` class methods that shell out to `gh` are tested via integration, not unit).

- [ ] **Step 5: Commit**

```bash
git add src/daemon/github/ tests/test_github.py
git commit -m "feat: GitHub issue integration via gh CLI

Issue CRUD, dependency parsing from task lists, DAG resolution for
picking next unblocked issue. Write-through cache to SQLite. #2"
```

---

### Task 3: Base Agent (Subprocess Lifecycle)

**Files:**
- Create: `src/daemon/agents/__init__.py`
- Create: `src/daemon/agents/base.py`
- Create: `tests/test_agents.py`

- [ ] **Step 1: Write failing tests for base agent**

`tests/test_agents.py`:
```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database


@pytest.fixture
async def agent(db: Database, config: ProjectConfig) -> BaseAgent:
    return BaseAgent(
        agent_type=AgentType.EPHEMERAL,
        config=config,
        db=db,
        issue_id=None,
    )


@pytest.mark.asyncio
async def test_agent_builds_command(agent: BaseAgent) -> None:
    cmd = agent.build_command(
        prompt="You are a test agent.",
        interactive=False,
    )
    assert "claude" in cmd[0]
    assert "--print" in cmd
    assert "--model" in cmd
    assert "claude-opus-4-6" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--append-system-prompt" in cmd


@pytest.mark.asyncio
async def test_agent_builds_interactive_command(agent: BaseAgent) -> None:
    cmd = agent.build_command(
        prompt="You are a test agent.",
        interactive=True,
    )
    assert "--print" not in cmd
    assert "--output-format" not in cmd


@pytest.mark.asyncio
async def test_agent_creates_session_on_spawn(
    db: Database, config: ProjectConfig,
) -> None:
    agent = BaseAgent(
        agent_type=AgentType.IMPLEMENTOR,
        config=config,
        db=db,
        issue_id=42,
    )
    # We'll mock the actual subprocess spawn
    with patch("daemon.agents.base.asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = mock_exec.return_value
        mock_proc.pid = 9999
        mock_proc.stdout = None
        mock_proc.stderr = None
        mock_proc.wait = lambda: asyncio.sleep(0)

        import asyncio
        session_id = await agent.spawn("Test prompt")

    session = await db.get_session(session_id)
    assert session is not None
    assert session["agent_type"] == "implementor"
    assert session["issue_id"] == 42
    assert session["status"] == "running"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agents.py -v`
Expected: ImportError — `daemon.agents.base` does not exist yet.

- [ ] **Step 3: Implement base agent**

`src/daemon/agents/__init__.py`:
```python
from daemon.agents.base import AgentType, BaseAgent

__all__ = ["AgentType", "BaseAgent"]
```

`src/daemon/agents/base.py`:
```python
from __future__ import annotations

import asyncio
import enum
import signal
from datetime import datetime, timezone
from pathlib import Path

from daemon.config import ProjectConfig
from daemon.db import Database


class AgentType(enum.StrEnum):
    PLANNER = "planner"
    IMPLEMENTOR = "implementor"
    PAPER = "paper"
    EPHEMERAL = "ephemeral"


# Prompt file basenames per agent type
_PROMPT_FILES = {
    AgentType.PLANNER: "planner.md",
    AgentType.IMPLEMENTOR: "implementor.md",
    AgentType.PAPER: "paper.md",
    AgentType.EPHEMERAL: "ephemeral.md",
}


def _prompts_dir() -> Path:
    """Locate the prompts/ directory relative to the package."""
    return Path(__file__).resolve().parent.parent.parent / "prompts"


class BaseAgent:
    """Manages a single claude CLI subprocess."""

    def __init__(
        self,
        agent_type: AgentType,
        config: ProjectConfig,
        db: Database,
        issue_id: int | None = None,
    ) -> None:
        self.agent_type = agent_type
        self.config = config
        self.db = db
        self.issue_id = issue_id
        self.session_id: str | None = None
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def log_path(self) -> Path:
        assert self.session_id is not None
        return self.config.logs_dir / f"{self.session_id}.jsonl"

    def _load_prompt(self) -> str:
        """Load the base prompt for this agent type."""
        prompt_file = _prompts_dir() / _PROMPT_FILES[self.agent_type]
        if prompt_file.exists():
            return prompt_file.read_text()
        return f"You are a {self.agent_type.value} agent."

    def build_command(
        self,
        prompt: str,
        interactive: bool = False,
    ) -> list[str]:
        """Build the claude CLI command."""
        cmd = [
            "claude",
            "--model", "claude-opus-4-6",
            "--max-turns", "100",
            "--dangerously-skip-permissions",
            "--append-system-prompt", prompt,
        ]
        if not interactive:
            cmd.extend(["--print", "--output-format", "json"])
        return cmd

    async def spawn(
        self,
        prompt: str,
        interactive: bool = False,
    ) -> str:
        """Spawn a claude subprocess and track it in the database."""
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)

        # Create session record
        self.session_id = await self.db.create_session(
            agent_type=self.agent_type.value,
            issue_id=self.issue_id,
            log_path=str(self.log_path),
        )

        full_prompt = self._load_prompt() + "\n\n" + prompt
        cmd = self.build_command(full_prompt, interactive=interactive)

        if interactive:
            # Interactive: connect to terminal directly
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.config.project_dir,
            )
        else:
            # Non-interactive: capture output to log file
            log_file = open(self.log_path, "w")
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.config.project_dir,
            )

        await self.db.update_session(self.session_id, pid=self._proc.pid)
        return self.session_id

    async def wait(self) -> int:
        """Wait for the subprocess to finish. Returns exit code."""
        assert self._proc is not None
        code = await self._proc.wait()
        now = datetime.now(timezone.utc).isoformat()
        status = "completed" if code == 0 else "failed"
        await self.db.update_session(
            self.session_id, status=status, ended_at=now
        )
        return code

    async def kill(self) -> None:
        """Send SIGTERM, wait 5s, then SIGKILL."""
        if self._proc is None or self._proc.returncode is not None:
            return
        self._proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        now = datetime.now(timezone.utc).isoformat()
        await self.db.update_session(
            self.session_id, status="killed", ended_at=now
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agents.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/daemon/agents/ tests/test_agents.py
git commit -m "feat: base agent with subprocess lifecycle and log capture

Spawns claude CLI as subprocess, tracks PID/status in SQLite,
captures output to JSONL log files. Supports interactive and
non-interactive modes. SIGTERM/SIGKILL for clean shutdown. #3"
```

---

### Task 4: Agent Prompts

**Files:**
- Create: `prompts/implementor.md`
- Create: `prompts/planner.md`
- Create: `prompts/paper.md`
- Create: `prompts/ephemeral.md`
- Create: `prompts/post_commit.md`

- [ ] **Step 1: Write the implementor prompt**

`prompts/implementor.md`:
```markdown
# Implementor Agent

You are the implementor agent for a ph.daemon-managed research project.

## Your Role

You implement one task at a time, as specified in a GitHub issue. You have full
read/write access to the codebase.

## Workflow

1. Read the issue description carefully
2. Check existing issues (open AND closed) for prior decisions and relevant context
3. Check `docs/constraints.md` for rules you must follow
4. Implement the task
5. Every code change MUST reference the issue number in the commit message (e.g., "Fixes #42")
6. After implementing, evaluate whether the change works:
   - Run tests, benchmarks, or whatever validation is appropriate
   - If it works: make an acceptance commit and close the issue
   - If it doesn't: revert the change, commit the revert, and explain why

## Commit Protocol

Every idea produces at least TWO commits:
- Implementation commit: the actual code change
- Acceptance OR revert commit: confirming it works or rolling it back

Never close an issue without an acceptance commit. Never leave a failed
approach uncommitted — always revert explicitly so the post-commit hook
can document what happened.

## Subtasks

If you discover the task is larger than expected, create child GitHub issues
for subtasks rather than doing everything in one pass. Use `gh issue create`
with dependency links.

## Status Reporting

When done, report one of:
- `DONE` — task completed successfully
- `DONE_WITH_CONCERNS` — completed but have doubts
- `BLOCKED` — cannot complete, need help
- `NEEDS_CONTEXT` — missing information
```

- [ ] **Step 2: Write the planner prompt**

`prompts/planner.md`:
```markdown
# Planner Agent

You are the planner agent for a ph.daemon-managed research project.

## Your Role

You take a high-level feature request and decompose it into ordered, actionable
GitHub issues with dependency relations. You are READ-ONLY to the codebase —
you write only to GitHub issues.

## Workflow

1. Read the feature request carefully
2. Explore the codebase to understand the current state
3. Check existing issues (open AND closed) for:
   - Similar prior attempts (reference them!)
   - Relevant decisions or constraints
   - Work that can be reused
4. Check `docs/constraints.md` for rules that affect decomposition
5. Create GitHub issues using `gh issue create`:
   - Each issue is one implementable task (fits in a single context window)
   - Issues have dependency relations via task list syntax: `- [ ] #N`
   - Each issue uses the ph.daemon issue schema (Context, Task, Dependencies, Constraints)
   - Label each issue with `ph:ready` or `ph:blocked` as appropriate
6. Cross-reference related existing issues by editing their bodies

## Issue Sizing

Each issue should be completable by the implementor in a single session. If a
task requires understanding more code than fits in 1M tokens of context, split
it further.

## Status Reporting

- `DONE` — issues created successfully
- `BLOCKED` — cannot decompose (unclear requirements, missing context)
- `NEEDS_CONTEXT` — need more information from the human
```

- [ ] **Step 3: Write the paper writer prompt**

`prompts/paper.md`:
```markdown
# Paper Writer Agent

You are the paper writer agent for a ph.daemon-managed research project.

## Your Role

You maintain the LaTeX research paper in `paper/` by incorporating recent
code changes. You are READ-ONLY to the research code — you write only to
`paper/` and can comment on GitHub issues.

## Workflow

1. Review the git diff since your last update
2. For each significant change, determine which paper section is affected:
   - New feature/design → Methodology/Design section
   - New results/benchmarks → Evaluation section
   - Bug fix or revert → may not need paper update (use judgment)
   - New constraint → may affect Threat Model or Limitations
3. Update the affected sections in `paper/`
4. Reference the source commits and issues in your LaTeX comments
5. Commit paper changes

## Writing Guidelines

- Write in academic style appropriate for the venue
- Every claim must be supported by evidence from the codebase
- Reference specific commits and issues when describing results
- Keep the paper coherent — don't just append; revise for flow
- Run `make compile` after changes to verify the paper builds

## Do NOT

- Modify any code outside `paper/`
- Fabricate results or claims
- Remove content without justification
```

- [ ] **Step 4: Write the ephemeral agent prompt**

`prompts/ephemeral.md`:
```markdown
# Ephemeral Agent

You are an ephemeral Q&A agent for a ph.daemon-managed research project.

## Your Role

You answer questions about the codebase and research project. You are
READ-ONLY to the code. You can write to `docs/` and GitHub issues.

## Capabilities

- Explain code, architecture, and design decisions
- Summarize the state of the project
- Find relevant GitHub issues and their discussion trails
- Add or modify constraints in `docs/constraints.md` (when asked)
- Create, edit, and close GitHub issues (when asked)

## Do NOT

- Modify any source code files
- Modify anything in `paper/`
- Make commits to the codebase (docs changes are OK)
```

- [ ] **Step 5: Write the post-commit hook prompt**

`prompts/post_commit.md`:
```markdown
# Post-Commit Discussion Agent

You are writing a discussion comment on a GitHub issue about a commit that
just landed.

## Your Job

Read the commit diff and write a substantive comment. This is NOT a changelog
entry — it's a discussion of WHY, not just WHAT.

## Comment Format

For implementation commits:
```
## Attempt: [description] (`COMMIT_SHA`)

**Approach:** [What was done and how]
**Justification:** [Why this approach was chosen over alternatives]
**Risks:** [Known concerns or failure modes]
**Status:** Pending evaluation.
```

For revert commits:
```
## Reverted: [description] (`COMMIT_SHA`, reverts `ORIGINAL_SHA`)

**What went wrong:** [What failed and why]
**What we learned:** [Lessons for future attempts]
**Next step:** [Follow-up plan, linked issue if created]
```

For acceptance commits:
```
## Accepted: [description] (`COMMIT_SHA`)

**Evaluation results:** [Evidence it works]
**Why it works:** [Explanation of why the approach succeeded]
**Resolved:** Closing #N.
```

## Guidelines

- Be specific. Reference line numbers, function names, test results.
- Explain reasoning, not just mechanics.
- If the commit is a revert, always explain what went wrong and what was learned.
- Keep each comment focused on one commit's contribution.
```

- [ ] **Step 6: Commit**

```bash
git add prompts/
git commit -m "feat: agent system prompts for all agent types

Implementor, planner, paper writer, ephemeral, and post-commit
discussion prompts. Each defines role, workflow, and constraints. #4"
```

---

### Task 5: Post-Commit Hook

**Files:**
- Create: `src/daemon/github/hooks.py`

- [ ] **Step 1: Implement the post-commit hook**

`src/daemon/github/hooks.py`:
```python
from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database

# Matches "#123" in commit messages
_ISSUE_REF = re.compile(r"#(\d+)")


def _get_latest_commit(project_dir: Path) -> dict:
    """Get the latest commit's SHA, message, and diff."""
    sha = subprocess.check_output(
        ["git", "log", "-1", "--format=%H"],
        cwd=project_dir,
    ).decode().strip()

    message = subprocess.check_output(
        ["git", "log", "-1", "--format=%B"],
        cwd=project_dir,
    ).decode().strip()

    diff = subprocess.check_output(
        ["git", "diff", "HEAD~1..HEAD", "--stat"],
        cwd=project_dir,
    ).decode().strip()

    full_diff = subprocess.check_output(
        ["git", "diff", "HEAD~1..HEAD"],
        cwd=project_dir,
    ).decode().strip()

    return {
        "sha": sha,
        "message": message,
        "diff_stat": diff,
        "diff": full_diff,
    }


def _extract_issue_numbers(message: str) -> list[int]:
    """Extract issue numbers from a commit message."""
    return [int(m.group(1)) for m in _ISSUE_REF.finditer(message)]


async def run_post_commit_hook(config: ProjectConfig, db: Database) -> None:
    """Parse the latest commit and post a discussion comment on linked issues."""
    commit = _get_latest_commit(config.project_dir)
    issue_numbers = _extract_issue_numbers(commit["message"])

    if not issue_numbers:
        return  # No issue reference in commit message — nothing to do

    prompt_file = Path(__file__).resolve().parent.parent.parent / "prompts" / "post_commit.md"
    base_prompt = prompt_file.read_text() if prompt_file.exists() else ""

    for issue_number in issue_numbers:
        # Build context for the discussion agent
        context = f"""## Commit Details

**SHA:** `{commit['sha'][:8]}`
**Message:** {commit['message']}

**Files changed:**
```
{commit['diff_stat']}
```

**Full diff:**
```diff
{commit['diff'][:50000]}
```

**Issue:** #{issue_number}

Write a discussion comment for this commit on issue #{issue_number}.
Use `gh issue comment {issue_number} --repo {config.repo} --body "YOUR COMMENT"`
to post it.
"""
        agent = BaseAgent(
            agent_type=AgentType.EPHEMERAL,
            config=config,
            db=db,
            issue_id=issue_number,
        )
        await agent.spawn(base_prompt + "\n\n" + context, interactive=False)
        await agent.wait()
```

- [ ] **Step 2: Commit**

```bash
git add src/daemon/github/hooks.py
git commit -m "feat: post-commit hook spawns discussion agent

Parses latest commit for issue references, then spawns a short
non-interactive claude session per issue to write a substantive
discussion comment. #5"
```

---

### Task 6: Implementor Agent (Main Loop)

**Files:**
- Create: `src/daemon/agents/implementor.py`

- [ ] **Step 1: Implement the implementor loop**

`src/daemon/agents/implementor.py`:
```python
from __future__ import annotations

import asyncio
import json
import logging

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database
from daemon.github.issues import GitHubIssues

logger = logging.getLogger(__name__)


class ImplementorLoop:
    """Main loop that picks up issues and dispatches implementor agents."""

    def __init__(
        self,
        config: ProjectConfig,
        db: Database,
        gh: GitHubIssues,
    ) -> None:
        self.config = config
        self.db = db
        self.gh = gh
        self._paused = False
        self._running = False
        self._current_session: str | None = None

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_session(self) -> str | None:
        return self._current_session

    def pause(self) -> None:
        self._paused = True
        logger.info("Implementor loop paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Implementor loop resumed")

    async def run(self) -> None:
        """Main loop: pick issue → implement → repeat."""
        self._running = True
        logger.info("Implementor loop started")
        try:
            while self._running:
                if self._paused:
                    await asyncio.sleep(5)
                    continue

                issue = await self.gh.pick_next_issue()
                if issue is None:
                    await asyncio.sleep(30)
                    continue

                await self._handle_issue(issue)
        finally:
            self._running = False
            logger.info("Implementor loop stopped")

    def stop(self) -> None:
        self._running = False

    async def _handle_issue(self, issue: dict) -> None:
        """Claim an issue and spawn an implementor agent."""
        number = issue["number"]
        logger.info(f"Picking up issue #{number}: {issue['title']}")

        # Claim the issue
        await self.gh.edit(
            number,
            add_labels=["ph:in-progress"],
            remove_labels=["ph:ready"],
            assignee="@me",
        )

        # Assemble context from issue + related issues + constraints
        context = await self._build_context(issue)

        # Spawn implementor
        agent = BaseAgent(
            agent_type=AgentType.IMPLEMENTOR,
            config=self.config,
            db=self.db,
            issue_id=number,
        )
        self._current_session = await agent.spawn(context, interactive=False)

        exit_code = await agent.wait()
        self._current_session = None

        if exit_code != 0:
            await self.gh.comment(
                number,
                f"**Agent failed** (exit code {exit_code}). "
                f"See session `{agent.session_id}` for details.",
            )

    async def _build_context(self, issue: dict) -> str:
        """Assemble the full context prompt for the implementor."""
        # Load constraints
        constraints = ""
        if self.config.constraints_path.exists():
            constraints = self.config.constraints_path.read_text()

        # Find related issues (referenced in body or comments)
        related = await self._find_related_issues(issue)

        # Build the prompt
        parts = [
            f"## Current Task: Issue #{issue['number']}",
            f"**Title:** {issue['title']}",
            "",
            issue.get("body", ""),
            "",
        ]

        if issue.get("comments"):
            parts.append("## Prior Discussion")
            for comment in issue["comments"]:
                parts.append(f"**{comment['author']}** ({comment['created_at']}):")
                parts.append(comment["body"])
                parts.append("")

        if related:
            parts.append("## Related Issues (for context / memoization)")
            for rel in related:
                parts.append(f"### #{rel['number']}: {rel['title']} ({rel['state']})")
                parts.append(rel.get("body", "")[:2000])  # Truncate long bodies
                if rel.get("comments"):
                    parts.append("Key discussion:")
                    for c in rel["comments"][-5:]:  # Last 5 comments
                        parts.append(f"  - {c['author']}: {c['body'][:500]}")
                parts.append("")

        if constraints:
            parts.append("## Active Constraints")
            parts.append(constraints)

        return "\n".join(parts)

    async def _find_related_issues(self, issue: dict) -> list[dict]:
        """Find issues referenced in the body or comments."""
        import re
        text = issue.get("body", "")
        for c in issue.get("comments", []):
            text += " " + c.get("body", "")

        refs = set(int(m) for m in re.findall(r"#(\d+)", text))
        refs.discard(issue["number"])

        related = []
        for ref in sorted(refs)[:10]:  # Cap at 10 to avoid context bloat
            rel = await self.db.get_issue(ref)
            if rel:
                related.append(rel)
        return related
```

- [ ] **Step 2: Commit**

```bash
git add src/daemon/agents/implementor.py
git commit -m "feat: implementor loop with issue pickup and context assembly

Main async loop that picks unblocked issues, claims them, builds
context (issue body + comments + related issues + constraints),
and spawns implementor agents. Pausable from web UI. #6"
```

---

### Task 7: Planner + Ephemeral + Paper Agents

**Files:**
- Create: `src/daemon/agents/planner.py`
- Create: `src/daemon/agents/ephemeral.py`
- Create: `src/daemon/agents/paper.py`

- [ ] **Step 1: Implement the planner agent**

`src/daemon/agents/planner.py`:
```python
from __future__ import annotations

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database
from daemon.github.issues import GitHubIssues


async def run_planner(
    config: ProjectConfig,
    db: Database,
    gh: GitHubIssues,
    feature_description: str,
    parent_issue: int | None = None,
) -> str:
    """Spawn a planner agent to decompose a feature into issues.

    Returns the session ID.
    """
    # Gather existing issues for memoization
    all_issues = await db.list_issues()
    issue_summaries = "\n".join(
        f"- #{i['number']} ({i['state']}): {i['title']}"
        for i in all_issues[-50:]  # Last 50 issues for context
    )

    constraints = ""
    if config.constraints_path.exists():
        constraints = config.constraints_path.read_text()

    context = f"""## Feature Request

{feature_description}

{"Parent issue: #" + str(parent_issue) if parent_issue else ""}

## Existing Issues (for reference / deduplication)

{issue_summaries or "No existing issues."}

## Active Constraints

{constraints or "No constraints yet."}

## Instructions

Decompose the feature request into GitHub issues using `gh issue create`.

For each issue:
1. Use the ph.daemon issue schema (Context, Task, Dependencies, Constraints sections)
2. Add dependency links using task list syntax: `- [ ] #N`
3. Label with `ph:ready` (no dependencies) or `ph:blocked` (has dependencies)
4. Label with `ph:implementor`
5. Use `--repo {config.repo}` on all gh commands

If this feature is related to existing issues, edit those issues to add
cross-references.
"""

    agent = BaseAgent(
        agent_type=AgentType.PLANNER,
        config=config,
        db=db,
        issue_id=parent_issue,
    )
    session_id = await agent.spawn(context, interactive=False)
    await agent.wait()

    # Sync issues to pick up newly created ones
    await gh.sync_all()

    return session_id
```

- [ ] **Step 2: Implement the ephemeral agent**

`src/daemon/agents/ephemeral.py`:
```python
from __future__ import annotations

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database


async def run_ephemeral_interactive(
    config: ProjectConfig,
    db: Database,
    initial_message: str,
) -> str:
    """Spawn an interactive ephemeral agent for Q&A.

    Returns the session ID.
    """
    constraints = ""
    if config.constraints_path.exists():
        constraints = config.constraints_path.read_text()

    context = f"""## Question

{initial_message}

## Active Constraints

{constraints or "No constraints yet."}

## Available Commands

You can use these gh commands with `--repo {config.repo}`:
- `gh issue create` — create new issues
- `gh issue edit` — edit existing issues
- `gh issue comment` — add comments
- `gh issue close` — close issues
- `gh issue list` — list issues
"""

    agent = BaseAgent(
        agent_type=AgentType.EPHEMERAL,
        config=config,
        db=db,
        issue_id=None,
    )
    session_id = await agent.spawn(context, interactive=True)
    await agent.wait()
    return session_id
```

- [ ] **Step 3: Implement the paper writer agent**

`src/daemon/agents/paper.py`:
```python
from __future__ import annotations

import subprocess

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database


def _get_commits_since(project_dir, since_sha: str | None) -> str:
    """Get commit log since a given SHA (or all commits if None)."""
    cmd = ["git", "log", "--oneline", "--no-merges"]
    if since_sha:
        cmd.append(f"{since_sha}..HEAD")
    cmd.extend(["--", ".", ":!paper/"])  # Exclude paper/ changes
    try:
        return subprocess.check_output(
            cmd, cwd=project_dir
        ).decode().strip()
    except subprocess.CalledProcessError:
        return ""


def _get_diff_since(project_dir, since_sha: str | None) -> str:
    """Get combined diff since a given SHA."""
    cmd = ["git", "diff"]
    if since_sha:
        cmd.append(f"{since_sha}..HEAD")
    else:
        cmd.append("--root")
    cmd.extend(["--", ".", ":!paper/"])
    try:
        output = subprocess.check_output(
            cmd, cwd=project_dir
        ).decode().strip()
        # Truncate to avoid exceeding context
        return output[:100000]
    except subprocess.CalledProcessError:
        return ""


async def run_paper_update(
    config: ProjectConfig,
    db: Database,
    since_sha: str | None = None,
) -> str:
    """Spawn a paper writer agent to update the paper.

    Args:
        since_sha: Only consider commits after this SHA. If None, considers all.

    Returns the session ID.
    """
    commits = _get_commits_since(config.project_dir, since_sha)
    if not commits:
        return ""  # Nothing to update

    diff = _get_diff_since(config.project_dir, since_sha)

    context = f"""## Recent Commits (since last paper update)

{commits}

## Combined Diff

```diff
{diff}
```

## Instructions

Update the paper in `paper/` to reflect these changes. The paper should
accurately describe the current state of the research.

After updating, run `make compile` in `paper/` to verify the paper builds.
Commit your changes with a message referencing the source commits.
"""

    agent = BaseAgent(
        agent_type=AgentType.PAPER,
        config=config,
        db=db,
        issue_id=None,
    )
    session_id = await agent.spawn(context, interactive=False)
    await agent.wait()
    return session_id
```

- [ ] **Step 4: Commit**

```bash
git add src/daemon/agents/planner.py src/daemon/agents/ephemeral.py src/daemon/agents/paper.py
git commit -m "feat: planner, ephemeral, and paper writer agents

Planner decomposes features into GitHub issues with dependencies.
Ephemeral runs interactive Q&A sessions. Paper writer updates LaTeX
paper based on recent commits. #7"
```

---

### Task 8: CLI Commands

**Files:**
- Create: `src/daemon/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing test for CLI init**

`tests/test_cli.py`:
```python
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from daemon.cli import main


def test_init_creates_scaffold(tmp_path: Path) -> None:
    project = tmp_path / "my-research"
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(project), "--repo", "me/my-research"])
    assert result.exit_code == 0

    assert (project / ".ph.daemon" / "config.json").exists()
    assert (project / ".ph.daemon" / "logs").exists()
    assert (project / "CLAUDE.md").exists()
    assert (project / "docs" / "constraints.md").exists()
    assert (project / "paper").exists()
    assert (project / ".gitignore").exists()
    assert ".ph.daemon/" in (project / ".gitignore").read_text()


def test_init_creates_claude_settings(tmp_path: Path) -> None:
    project = tmp_path / "my-research"
    runner = CliRunner()
    runner.invoke(main, ["init", str(project), "--repo", "me/my-research"])

    settings_path = project / ".claude" / "settings.json"
    assert settings_path.exists()
    import json
    settings = json.loads(settings_path.read_text())
    assert "hooks" in settings
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: ImportError — `daemon.cli` does not exist yet.

- [ ] **Step 3: Implement the CLI**

`src/daemon/cli.py`:
```python
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click
import uvicorn

from daemon.config import ProjectConfig

logger = logging.getLogger(__name__)


def _find_project_dir() -> Path:
    """Walk up from cwd to find a .ph.daemon directory."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".ph.daemon").exists():
            return current
        current = current.parent
    click.echo("Error: not inside a ph.daemon project. Run `phd init` first.", err=True)
    sys.exit(1)


def _get_config() -> ProjectConfig:
    project_dir = _find_project_dir()
    return ProjectConfig.load(project_dir)


@click.group()
def main() -> None:
    """ph.daemon — automated research harness."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@main.command()
@click.argument("project_path", type=click.Path())
@click.option("--repo", required=True, help="GitHub repo in owner/repo format")
def init(project_path: str, repo: str) -> None:
    """Initialize a new research project."""
    project = Path(project_path).resolve()
    config = ProjectConfig(project_dir=project, repo=repo)

    # Create directory structure
    config.daemon_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    (project / "docs").mkdir(parents=True, exist_ok=True)
    config.paper_dir.mkdir(parents=True, exist_ok=True)
    (project / ".claude").mkdir(parents=True, exist_ok=True)

    # Save config
    config.save()

    # CLAUDE.md
    claude_md = project / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(
            "# Research Project\n\n"
            "## Daemon\n\n"
            "This project is managed by ph.daemon. All work is tracked in GitHub issues.\n\n"
            "- Every code change must reference a GitHub issue number in the commit message\n"
            "- Before starting work, check existing issues (open and closed) for prior decisions\n"
            "- After each commit, update the linked issue with a summary of changes\n\n"
            "## Constraints\n\n"
            "@docs/constraints.md\n\n"
            "## Paper\n\n"
            "The research paper lives in `paper/`. Only the paper writer agent modifies it.\n"
            "Do not edit `paper/` directly during implementation tasks.\n"
        )

    # docs/constraints.md
    constraints = config.constraints_path
    if not constraints.exists():
        constraints.write_text(
            "# Constraints\n\n"
            "Rules that must always be followed. Each constraint was added because the LLM\n"
            "made a mistake or the human wants to enforce a specific approach.\n\n"
            "<!-- Constraints are append-only. To remove one, discuss via `phd ask` first. -->\n"
        )

    # .gitignore
    gitignore = project / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    if ".ph.daemon/" not in lines:
        lines.append(".ph.daemon/")
        gitignore.write_text("\n".join(lines) + "\n")

    # .claude/settings.json — post-commit hook
    settings_path = project / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
    settings.setdefault("hooks", {})
    settings["hooks"]["PostCommit"] = [
        {
            "command": "phd hook post-commit",
            "description": "Update linked GitHub issue with commit discussion",
        }
    ]
    settings_path.write_text(json.dumps(settings, indent=2))

    click.echo(f"Initialized ph.daemon project at {project}")
    click.echo(f"  GitHub repo: {repo}")
    click.echo(f"  Run `cd {project} && phd start` to begin")


@main.command()
@click.option("--port", default=8666, help="Web UI port")
def start(port: int) -> None:
    """Start the web UI and implementor loop."""
    config = _get_config()
    click.echo(f"Starting ph.daemon for {config.repo}")
    click.echo(f"Web UI: http://localhost:{port}")

    # Import here to avoid circular imports
    from daemon.app import create_app

    app = create_app(config)
    uvicorn.run(app, host="127.0.0.1", port=port)


@main.command()
@click.argument("description")
def task(description: str) -> None:
    """Submit a task (opens interactive session to refine, then plans)."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.github.issues import GitHubIssues
        from daemon.agents.ephemeral import run_ephemeral_interactive
        from daemon.agents.planner import run_planner

        db = Database(config.db_path)
        await db.init()
        gh = GitHubIssues(config=config, db=db)

        try:
            # Phase 1: Interactive refinement
            click.echo("Opening interactive session to refine your task...")
            click.echo("When done, exit the session and the planner will create issues.")
            await run_ephemeral_interactive(config, db, f"Help me refine this task: {description}")

            # Phase 2: Planner creates issues
            click.echo("\nDispatching planner to create issues...")
            await run_planner(config, db, gh, description)
            click.echo("Done. Issues created. The implementor will pick them up.")
        finally:
            await db.close()

    asyncio.run(_run())


@main.command()
@click.argument("description")
def constrain(description: str) -> None:
    """Add a constraint (opens interactive session to refine)."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.agents.ephemeral import run_ephemeral_interactive

        db = Database(config.db_path)
        await db.init()

        prompt = (
            f"The human wants to add a constraint: {description}\n\n"
            "Help them refine it. When done:\n"
            "1. Append the constraint to `docs/constraints.md` with the standard format "
            "(numbered, dated, with rationale)\n"
            f"2. Create a GitHub issue with label `ph:constraint` using "
            f"`gh issue create --repo {config.repo}`\n"
            "3. Commit the constraints.md change referencing the issue"
        )

        try:
            await run_ephemeral_interactive(config, db, prompt)
        finally:
            await db.close()

    asyncio.run(_run())


@main.command()
@click.argument("question")
def ask(question: str) -> None:
    """Ask a question about the project (interactive Q&A)."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.agents.ephemeral import run_ephemeral_interactive

        db = Database(config.db_path)
        await db.init()
        try:
            await run_ephemeral_interactive(config, db, question)
        finally:
            await db.close()

    asyncio.run(_run())


@main.command()
def paper() -> None:
    """Trigger a manual paper update."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.agents.paper import run_paper_update

        db = Database(config.db_path)
        await db.init()
        try:
            click.echo("Updating paper based on recent commits...")
            session_id = await run_paper_update(config, db)
            if session_id:
                click.echo(f"Paper update complete (session: {session_id})")
            else:
                click.echo("No new commits to incorporate.")
        finally:
            await db.close()

    asyncio.run(_run())


@main.command()
def status() -> None:
    """Show agent status and issue queue."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database

        db = Database(config.db_path)
        await db.init()
        try:
            running = await db.list_sessions(status="running")
            open_issues = await db.list_issues(state="open")

            click.echo(f"Project: {config.repo}")
            click.echo(f"Running agents: {len(running)}")
            for s in running:
                click.echo(f"  [{s['agent_type']}] session {s['id']}"
                          f"{' → #' + str(s['issue_id']) if s['issue_id'] else ''}")
            click.echo(f"Open issues: {len(open_issues)}")
        finally:
            await db.close()

    asyncio.run(_run())


@main.group()
def hook() -> None:
    """Hook commands (called by Claude Code, not humans)."""
    pass


@hook.command("post-commit")
def hook_post_commit() -> None:
    """Post-commit hook: update linked GitHub issue with discussion."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.github.hooks import run_post_commit_hook

        db = Database(config.db_path)
        await db.init()
        try:
            await run_post_commit_hook(config, db)
        finally:
            await db.close()

    asyncio.run(_run())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: All 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/daemon/cli.py tests/test_cli.py
git commit -m "feat: CLI commands for phd init/start/task/constrain/ask/paper/status

All commands work against a target project directory. Interactive
commands (task, constrain, ask) open Claude sessions. Init scaffolds
the full project structure including CLAUDE.md and post-commit hook. #8"
```

---

### Task 9: FastAPI Application + Web UI Foundation

**Files:**
- Create: `src/daemon/app.py`
- Create: `src/daemon/web/__init__.py`
- Create: `src/daemon/web/routes.py`
- Create: `src/daemon/web/sse.py`
- Create: `src/daemon/web/templates/base.html`
- Create: `src/daemon/web/templates/dashboard.html`
- Create: `tests/test_web.py`

- [ ] **Step 1: Write failing test for the dashboard route**

`tests/test_web.py`:
```python
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from daemon.app import create_app
from daemon.config import ProjectConfig


@pytest.fixture
def app(config: ProjectConfig):
    return create_app(config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_dashboard_returns_200(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "ph.daemon" in resp.text


@pytest.mark.asyncio
async def test_agents_page_returns_200(client: AsyncClient) -> None:
    resp = await client.get("/agents")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_issues_page_returns_200(client: AsyncClient) -> None:
    resp = await client.get("/issues")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web.py -v`
Expected: ImportError — `daemon.app` does not exist yet.

- [ ] **Step 3: Implement the FastAPI app factory**

`src/daemon/app.py`:
```python
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from daemon.config import ProjectConfig
from daemon.db import Database
from daemon.github.issues import GitHubIssues
from daemon.agents.implementor import ImplementorLoop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB, start sync loop and implementor loop."""
    config: ProjectConfig = app.state.config
    db = Database(config.db_path)
    await db.init()

    gh = GitHubIssues(config=config, db=db)
    impl_loop = ImplementorLoop(config=config, db=db, gh=gh)

    app.state.db = db
    app.state.gh = gh
    app.state.impl_loop = impl_loop

    # Start background tasks
    sync_task = asyncio.create_task(_sync_loop(gh))
    impl_task = asyncio.create_task(impl_loop.run())

    yield

    # Shutdown
    impl_loop.stop()
    sync_task.cancel()
    impl_task.cancel()
    await db.close()


async def _sync_loop(gh: GitHubIssues) -> None:
    """Periodically sync issues from GitHub."""
    while True:
        try:
            await gh.sync_all()
        except Exception:
            pass  # Log and continue
        await asyncio.sleep(60)


def create_app(config: ProjectConfig) -> FastAPI:
    app = FastAPI(title="ph.daemon", lifespan=lifespan)
    app.state.config = config

    from daemon.web.routes import router
    app.include_router(router)

    from daemon.web.sse import sse_router
    app.include_router(sse_router)

    return app
```

- [ ] **Step 4: Implement the web routes**

`src/daemon/web/__init__.py`:
```python
```

`src/daemon/web/routes.py`:
```python
from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = request.app.state.db
    config = request.app.state.config
    impl_loop = request.app.state.impl_loop

    running = await db.list_sessions(status="running")
    recent = await db.list_sessions()
    open_issues = await db.list_issues(state="open")

    # Recent commits from git log
    try:
        git_log = subprocess.check_output(
            ["git", "log", "--oneline", "-10"],
            cwd=config.project_dir,
        ).decode().strip().splitlines()
    except subprocess.CalledProcessError:
        git_log = []

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "running_agents": running,
        "recent_sessions": recent[:20],
        "open_issues": open_issues,
        "recent_commits": git_log,
        "impl_paused": impl_loop.is_paused,
        "repo": config.repo,
    })


@router.get("/agents", response_class=HTMLResponse)
async def agents_list(request: Request):
    db = request.app.state.db
    sessions = await db.list_sessions()
    return templates.TemplateResponse("agents.html", {
        "request": request,
        "sessions": sessions,
    })


@router.get("/agents/{session_id}", response_class=HTMLResponse)
async def agent_detail(request: Request, session_id: str):
    db = request.app.state.db
    session = await db.get_session(session_id)
    return templates.TemplateResponse("session.html", {
        "request": request,
        "session": session,
    })


@router.get("/issues", response_class=HTMLResponse)
async def issues_list(request: Request):
    db = request.app.state.db
    issues = await db.list_issues()
    return templates.TemplateResponse("issues.html", {
        "request": request,
        "issues": issues,
        "repo": request.app.state.config.repo,
    })


@router.get("/paper", response_class=HTMLResponse)
async def paper_view(request: Request):
    config = request.app.state.config
    pdf_exists = (config.paper_dir / "main.pdf").exists()
    return templates.TemplateResponse("paper.html", {
        "request": request,
        "pdf_exists": pdf_exists,
    })


@router.get("/constraints", response_class=HTMLResponse)
async def constraints_view(request: Request):
    config = request.app.state.config
    content = ""
    if config.constraints_path.exists():
        content = config.constraints_path.read_text()
    return templates.TemplateResponse("constraints.html", {
        "request": request,
        "content": content,
    })


# --- API endpoints for htmx actions ---

@router.post("/api/impl/pause")
async def pause_impl(request: Request):
    request.app.state.impl_loop.pause()
    return HTMLResponse('<span class="status paused">Paused</span>')


@router.post("/api/impl/resume")
async def resume_impl(request: Request):
    request.app.state.impl_loop.resume()
    return HTMLResponse('<span class="status running">Running</span>')


@router.post("/api/agents/{session_id}/kill")
async def kill_agent(request: Request, session_id: str):
    # Find the running agent and kill it
    db = request.app.state.db
    session = await db.get_session(session_id)
    if session and session["status"] == "running" and session["pid"]:
        import os
        import signal
        try:
            os.kill(session["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        await db.update_session(session_id, status="killed")
    return HTMLResponse('<span class="status killed">Killed</span>')
```

- [ ] **Step 5: Implement SSE log streaming**

`src/daemon/web/sse.py`:
```python
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

sse_router = APIRouter()


async def _tail_log(log_path: str):
    """Async generator that tails a log file and yields new lines."""
    path = Path(log_path)
    if not path.exists():
        yield {"data": "Log file not found."}
        return

    with open(path) as f:
        # Start from beginning
        while True:
            line = f.readline()
            if line:
                yield {"data": line.rstrip()}
            else:
                await asyncio.sleep(0.5)


@sse_router.get("/api/agents/{session_id}/logs")
async def stream_logs(request: Request, session_id: str):
    db = request.app.state.db
    session = await db.get_session(session_id)
    if not session:
        return EventSourceResponse(iter([{"data": "Session not found."}]))

    return EventSourceResponse(_tail_log(session["log_path"]))
```

- [ ] **Step 6: Create the base HTML template**

`src/daemon/web/templates/base.html`:
```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{% block title %}ph.daemon{% endblock %}</title>
    <script src="https://unpkg.com/htmx.org@2.0.4"></script>
    <script src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"></script>
    <style>
        :root {
            --bg: #0d1117; --surface: #161b22; --border: #30363d;
            --text: #e6edf3; --text-muted: #8b949e;
            --green: #3fb950; --yellow: #d29922; --red: #f85149;
            --blue: #58a6ff; --purple: #bc8cff;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace;
            background: var(--bg); color: var(--text);
            display: flex; min-height: 100vh;
        }
        nav {
            width: 200px; background: var(--surface); border-right: 1px solid var(--border);
            padding: 1rem; flex-shrink: 0;
        }
        nav h1 { font-size: 1.1rem; margin-bottom: 1.5rem; color: var(--purple); }
        nav a {
            display: block; padding: 0.5rem; color: var(--text-muted);
            text-decoration: none; border-radius: 4px; margin-bottom: 0.25rem;
        }
        nav a:hover, nav a.active { background: var(--bg); color: var(--text); }
        main { flex: 1; padding: 2rem; max-width: 1200px; }
        h2 { margin-bottom: 1rem; }
        .card {
            background: var(--surface); border: 1px solid var(--border);
            border-radius: 8px; padding: 1rem; margin-bottom: 1rem;
        }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1rem; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 0.5rem; text-align: left; border-bottom: 1px solid var(--border); }
        th { color: var(--text-muted); font-weight: normal; }
        .status { padding: 2px 8px; border-radius: 4px; font-size: 0.85rem; }
        .status.running { background: #1a3a1a; color: var(--green); }
        .status.completed { background: #1a2a3a; color: var(--blue); }
        .status.failed { background: #3a1a1a; color: var(--red); }
        .status.killed { background: #3a2a1a; color: var(--yellow); }
        .status.paused { background: #2a1a3a; color: var(--purple); }
        .btn {
            padding: 0.4rem 0.8rem; border: 1px solid var(--border); border-radius: 4px;
            background: var(--surface); color: var(--text); cursor: pointer;
        }
        .btn:hover { border-color: var(--blue); }
        .btn-danger { border-color: var(--red); color: var(--red); }
        .log-stream {
            background: #000; color: #ccc; padding: 1rem; font-family: monospace;
            font-size: 0.85rem; max-height: 600px; overflow-y: auto;
            border-radius: 4px; white-space: pre-wrap;
        }
        .count { font-size: 2rem; font-weight: bold; }
        .count-label { color: var(--text-muted); font-size: 0.85rem; }
    </style>
</head>
<body>
    <nav>
        <h1>ph.daemon</h1>
        <a href="/">Dashboard</a>
        <a href="/agents">Agents</a>
        <a href="/issues">Issues</a>
        <a href="/paper">Paper</a>
        <a href="/constraints">Constraints</a>
    </nav>
    <main>
        {% block content %}{% endblock %}
    </main>
</body>
</html>
```

- [ ] **Step 7: Create the dashboard template**

`src/daemon/web/templates/dashboard.html`:
```html
{% extends "base.html" %}
{% block title %}Dashboard — ph.daemon{% endblock %}
{% block content %}
<h2>Dashboard</h2>
<p style="color: var(--text-muted); margin-bottom: 1.5rem;">{{ repo }}</p>

<div class="grid" hx-get="/" hx-trigger="every 5s" hx-select=".grid" hx-swap="outerHTML">
    <div class="card">
        <div class="count">{{ running_agents|length }}</div>
        <div class="count-label">Running Agents</div>
    </div>
    <div class="card">
        <div class="count">{{ open_issues|length }}</div>
        <div class="count-label">Open Issues</div>
    </div>
    <div class="card">
        <div class="count-label">Implementor Loop</div>
        {% if impl_paused %}
            <span class="status paused">Paused</span>
            <button class="btn" hx-post="/api/impl/resume" hx-swap="outerHTML">Resume</button>
        {% else %}
            <span class="status running">Running</span>
            <button class="btn" hx-post="/api/impl/pause" hx-swap="outerHTML">Pause</button>
        {% endif %}
    </div>
</div>

{% if running_agents %}
<div class="card">
    <h3 style="margin-bottom: 0.5rem;">Active Agents</h3>
    <table>
        <tr><th>Type</th><th>Issue</th><th>Session</th><th>Status</th></tr>
        {% for s in running_agents %}
        <tr>
            <td>{{ s.agent_type }}</td>
            <td>{% if s.issue_id %}<a href="https://github.com/{{ repo }}/issues/{{ s.issue_id }}">#{{ s.issue_id }}</a>{% else %}—{% endif %}</td>
            <td><a href="/agents/{{ s.id }}">{{ s.id }}</a></td>
            <td><span class="status running">running</span></td>
        </tr>
        {% endfor %}
    </table>
</div>
{% endif %}

<div class="card">
    <h3 style="margin-bottom: 0.5rem;">Recent Commits</h3>
    {% if recent_commits %}
    <pre style="color: var(--text-muted); font-size: 0.85rem;">{% for c in recent_commits %}{{ c }}
{% endfor %}</pre>
    {% else %}
    <p style="color: var(--text-muted);">No commits yet.</p>
    {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 8: Create remaining page templates**

`src/daemon/web/templates/agents.html`:
```html
{% extends "base.html" %}
{% block title %}Agents — ph.daemon{% endblock %}
{% block content %}
<h2>Agent Sessions</h2>
<table>
    <tr><th>Session</th><th>Type</th><th>Issue</th><th>Status</th><th>Started</th></tr>
    {% for s in sessions %}
    <tr>
        <td><a href="/agents/{{ s.id }}">{{ s.id }}</a></td>
        <td>{{ s.agent_type }}</td>
        <td>{% if s.issue_id %}#{{ s.issue_id }}{% else %}—{% endif %}</td>
        <td><span class="status {{ s.status }}">{{ s.status }}</span></td>
        <td>{{ s.started_at[:19] }}</td>
    </tr>
    {% endfor %}
</table>
{% endblock %}
```

`src/daemon/web/templates/session.html`:
```html
{% extends "base.html" %}
{% block title %}Session {{ session.id }} — ph.daemon{% endblock %}
{% block content %}
<h2>Session {{ session.id }}</h2>
<div class="card" style="margin-bottom: 1rem;">
    <p><strong>Type:</strong> {{ session.agent_type }}</p>
    <p><strong>Issue:</strong> {% if session.issue_id %}#{{ session.issue_id }}{% else %}None{% endif %}</p>
    <p><strong>Status:</strong> <span class="status {{ session.status }}">{{ session.status }}</span></p>
    <p><strong>PID:</strong> {{ session.pid or '—' }}</p>
    <p><strong>Started:</strong> {{ session.started_at }}</p>
    {% if session.status == 'running' %}
    <button class="btn btn-danger" hx-post="/api/agents/{{ session.id }}/kill" hx-swap="outerHTML">
        Kill Agent
    </button>
    {% endif %}
</div>

<h3>Log Output</h3>
<div class="log-stream"
     hx-ext="sse"
     sse-connect="/api/agents/{{ session.id }}/logs"
     sse-swap="message"
     hx-swap="beforeend">
</div>
{% endblock %}
```

`src/daemon/web/templates/issues.html`:
```html
{% extends "base.html" %}
{% block title %}Issues — ph.daemon{% endblock %}
{% block content %}
<h2>Issues</h2>
<table>
    <tr><th>#</th><th>Title</th><th>State</th><th>Labels</th><th>Assignee</th></tr>
    {% for i in issues %}
    <tr>
        <td><a href="https://github.com/{{ repo }}/issues/{{ i.number }}">#{{ i.number }}</a></td>
        <td>{{ i.title }}</td>
        <td><span class="status {{ 'running' if i.state == 'open' else 'completed' }}">{{ i.state }}</span></td>
        <td>{{ i.labels | join(', ') }}</td>
        <td>{{ i.assignee or '—' }}</td>
    </tr>
    {% endfor %}
</table>
{% endblock %}
```

`src/daemon/web/templates/paper.html`:
```html
{% extends "base.html" %}
{% block title %}Paper — ph.daemon{% endblock %}
{% block content %}
<h2>Paper</h2>
{% if pdf_exists %}
<div class="card">
    <p>PDF available. <button class="btn" hx-post="/api/paper/update">Trigger Update</button></p>
    <iframe src="/paper/main.pdf" style="width: 100%; height: 80vh; border: none; margin-top: 1rem;"></iframe>
</div>
{% else %}
<div class="card">
    <p style="color: var(--text-muted);">No paper compiled yet. Run <code>phd paper</code> or trigger from here.</p>
    <button class="btn" hx-post="/api/paper/update">Trigger Paper Update</button>
</div>
{% endif %}
{% endblock %}
```

`src/daemon/web/templates/constraints.html`:
```html
{% extends "base.html" %}
{% block title %}Constraints — ph.daemon{% endblock %}
{% block content %}
<h2>Constraints</h2>
<div class="card">
    <pre style="white-space: pre-wrap; color: var(--text-muted);">{{ content }}</pre>
</div>

<div class="card">
    <h3 style="margin-bottom: 0.5rem;">Add Constraint</h3>
    <form hx-post="/api/constraints/add" hx-target="body" hx-swap="outerHTML">
        <input type="text" name="description" placeholder="Describe the constraint..."
               style="width: 100%; padding: 0.5rem; background: var(--bg); border: 1px solid var(--border);
                      color: var(--text); border-radius: 4px; margin-bottom: 0.5rem;">
        <button class="btn" type="submit">Add Constraint</button>
    </form>
</div>
{% endblock %}
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `uv run pytest tests/test_web.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 10: Commit**

```bash
git add src/daemon/app.py src/daemon/web/ tests/test_web.py
git commit -m "feat: web UI with dashboard, agent logs, issues, paper, constraints

FastAPI + Jinja2 + htmx. SSE log streaming for live agent output.
Dashboard with agent status, issue counts, implementor loop control.
All pages server-rendered with htmx polling for real-time updates. #9"
```

---

### Task 10: Integration Wiring + Final Polish

**Files:**
- Modify: `src/daemon/agents/__init__.py`
- Modify: `src/daemon/web/routes.py` (add paper update + constraint API endpoints)

- [ ] **Step 1: Add missing API endpoints for web UI actions**

Add to the bottom of `src/daemon/web/routes.py`:
```python
@router.post("/api/paper/update")
async def trigger_paper_update(request: Request):
    from daemon.agents.paper import run_paper_update
    config = request.app.state.config
    db = request.app.state.db
    asyncio.create_task(run_paper_update(config, db))
    return HTMLResponse('<p style="color: var(--green);">Paper update triggered.</p>')


@router.post("/api/constraints/add")
async def add_constraint(request: Request):
    form = await request.form()
    description = form.get("description", "")
    if not description:
        return HTMLResponse("Description required", status_code=400)

    config = request.app.state.config
    db = request.app.state.db

    # Spawn an ephemeral agent to handle the constraint addition
    from daemon.agents.ephemeral import run_ephemeral_interactive
    asyncio.create_task(
        run_ephemeral_interactive(config, db, f"Add this constraint: {description}")
    )

    # Redirect back to constraints page
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/constraints", status_code=303)
```

(Note: add `import asyncio` to the top of routes.py if not already present.)

- [ ] **Step 2: Update agents __init__ to re-export all agent types**

`src/daemon/agents/__init__.py`:
```python
from daemon.agents.base import AgentType, BaseAgent
from daemon.agents.implementor import ImplementorLoop
from daemon.agents.planner import run_planner
from daemon.agents.ephemeral import run_ephemeral_interactive
from daemon.agents.paper import run_paper_update

__all__ = [
    "AgentType",
    "BaseAgent",
    "ImplementorLoop",
    "run_planner",
    "run_ephemeral_interactive",
    "run_paper_update",
]
```

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Run linting**

Run: `uv run ruff check src/ tests/`

Fix any issues found.

- [ ] **Step 5: Verify the CLI entrypoint works**

Run: `uv run phd --help`
Expected: Shows help text with all commands.

Run: `uv run phd init /tmp/test-project --repo test/test-repo`
Expected: Creates the scaffolded project structure.

- [ ] **Step 6: Commit**

```bash
git add src/daemon/
git commit -m "feat: integration wiring and final polish

Wire up web UI API endpoints for paper update and constraint addition.
Clean up agent re-exports. Verify full test suite and CLI entrypoint. #10"
```

---

## Verification Checklist

After all tasks are complete, verify:

- [ ] `uv run pytest tests/ -v` — all tests pass
- [ ] `uv run phd --help` — CLI shows all commands
- [ ] `uv run phd init /tmp/test --repo owner/repo` — scaffolds correctly
- [ ] `uv run phd start` — starts server on localhost:8666 (from inside a project)
- [ ] Dashboard loads and shows empty state
- [ ] Agent sessions page loads
- [ ] Issues page loads
- [ ] Paper page loads
- [ ] Constraints page loads
