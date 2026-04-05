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
