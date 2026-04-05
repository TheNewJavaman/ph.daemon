from __future__ import annotations

import json
import logging

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database

logger = logging.getLogger(__name__)


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
task is clear, create tasks by writing them to `.phd/new_tasks.json`:

```json
[
  {{"title": "Task title", "description": "Detailed description", "priority": 0}},
  {{"title": "Dependent task", "description": "...", "depends_on": [1], "priority": 0}}
]
```

Write this file using your Write tool. Use priority 0 for human-requested tasks.
Use `depends_on` with task IDs to declare dependencies.
The orchestrator will import the tasks automatically after this session.
"""

    # Clear stale drop file
    drop_file = config.daemon_dir / "new_tasks.json"
    drop_file.unlink(missing_ok=True)

    agent = BaseAgent(
        agent_type=AgentType.PLANNER,
        config=config,
        db=db,
        task_id=None,
    )
    session_id = await agent.spawn(context, interactive=True)
    await agent.wait()

    # Import tasks from drop file
    if drop_file.exists():
        try:
            tasks = json.loads(drop_file.read_text())
            if not isinstance(tasks, list):
                tasks = [tasks]
            for t in tasks:
                if not isinstance(t, dict) or "title" not in t:
                    continue
                task_id = await db.create_task(
                    title=t["title"],
                    description=t.get("description", ""),
                    priority=t.get("priority", 0),
                    dependencies=t.get("depends_on", []),
                )
                logger.info(f"Imported task #{task_id}: {t['title']}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse new_tasks.json: {e}")
        finally:
            drop_file.unlink(missing_ok=True)

    return session_id
