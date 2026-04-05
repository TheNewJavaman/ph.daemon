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
