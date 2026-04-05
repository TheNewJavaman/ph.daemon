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
