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
4. Label with `ph:human` (this is a human-requested task — takes priority over director tasks)
5. Label with `ph:implementor`
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
