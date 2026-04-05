from __future__ import annotations

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database


async def run_planner_interactive(
    config: ProjectConfig,
    db: Database,
    feature_description: str,
) -> str:
    """Interactive planner session: refine the task AND create subtasks."""
    all_tasks = await db.list_tasks()
    task_summaries = "\n".join(
        f"- #{t['id']} ({t['status']}): {t['title']}"
        for t in all_tasks[-50:]
    )

    constraints = ""
    if config.constraints_path.exists():
        constraints = config.constraints_path.read_text()

    context = f"""## Feature Request

{feature_description}

## Existing Tasks (for reference / deduplication)

{task_summaries or "No existing tasks."}

## Active Constraints

{constraints or "No constraints yet."}

## Instructions

You are in an interactive session with the human. First, discuss and refine
the feature request with them. Ask clarifying questions if needed. Once the
task is clear, decompose it into tasks using `phd create-task`.

For each task:
  phd create-task "Task title" -d "Detailed description" -p 0

Use -p 0 for human-requested tasks (higher priority).
Use --depends-on N to declare dependencies on other task IDs.
"""

    agent = BaseAgent(
        agent_type=AgentType.PLANNER,
        config=config,
        db=db,
        task_id=None,
    )
    session_id = await agent.spawn(context, interactive=True)
    await agent.wait()
    return session_id
