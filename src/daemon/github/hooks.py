from __future__ import annotations

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
