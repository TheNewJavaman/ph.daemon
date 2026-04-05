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
