from __future__ import annotations

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
