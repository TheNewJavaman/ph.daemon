from __future__ import annotations

import asyncio
import logging
import subprocess

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database
from daemon.github.issues import GitHubIssues

logger = logging.getLogger(__name__)


class DirectorLoop:
    """Generates research tasks when the implementor queue has no human work.

    The director continuously analyzes the research state — paper claims,
    codebase capabilities, benchmark results, dataset quality — and creates
    GitHub issues for experiments, optimizations, and dataset curation.

    Issues are labeled ph:director so the implementor prioritizes human
    tasks (ph:human) above director-generated ones.
    """

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

    def pause(self) -> None:
        self._paused = True
        logger.info("Director loop paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Director loop resumed")

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main loop: analyze research state → generate tasks → wait → repeat."""
        self._running = True
        logger.info("Director loop started")
        try:
            while self._running:
                if self._paused:
                    await asyncio.sleep(10)
                    continue

                # Only generate work when director queue is low
                director_issues = await self._count_open_director_issues()
                if director_issues >= 3:
                    # Enough queued work — wait for implementor to finish some
                    await asyncio.sleep(60)
                    continue

                await self._generate_tasks()

                # Wait before next cycle — give implementor time to work
                await asyncio.sleep(120)
        finally:
            self._running = False
            logger.info("Director loop stopped")

    async def _count_open_director_issues(self) -> int:
        """Count open issues with ph:director label."""
        open_issues = await self.db.list_issues(state="open")
        return sum(
            1 for i in open_issues
            if "ph:director" in i.get("labels", [])
        )

    async def _generate_tasks(self) -> None:
        """Analyze research state and create new issues."""
        logger.info("Director analyzing research state...")

        context = await self._build_context()

        agent = BaseAgent(
            agent_type=AgentType.DIRECTOR,
            config=self.config,
            db=self.db,
            issue_id=None,
        )
        self._current_session = await agent.spawn(context, interactive=False)
        exit_code = await agent.wait()
        self._current_session = None

        if exit_code == 0:
            # Sync to pick up newly created issues
            await self.gh.sync_all()
            # Update research state file
            logger.info("Director cycle complete")
        else:
            logger.warning(f"Director agent failed (exit code {exit_code})")

    async def _build_context(self) -> str:
        """Assemble context for the director: paper, results, history."""
        parts = []

        # Current research state
        if self.config.research_state_path.exists():
            state = self.config.research_state_path.read_text()
            if state.strip():
                parts.append("## Current Research State")
                parts.append(state)
                parts.append("")

        # Paper summary (if paper exists)
        paper_tex = self.config.paper_dir / "main.tex"
        if paper_tex.exists():
            paper_content = paper_tex.read_text()
            # Truncate to avoid blowing context
            parts.append("## Current Paper (main.tex)")
            parts.append(paper_content[:50000])
            parts.append("")

        # Recent closed issues — what's been tried
        closed_issues = await self.db.list_issues(state="closed")
        if closed_issues:
            parts.append("## Recently Completed Work")
            for issue in closed_issues[-20:]:
                parts.append(f"- #{issue['number']}: {issue['title']}")
                # Include key discussion points
                for c in issue.get("comments", [])[-2:]:
                    parts.append(f"  > {c['body'][:300]}")
            parts.append("")

        # Open issues — what's already queued
        open_issues = await self.db.list_issues(state="open")
        if open_issues:
            parts.append("## Currently Queued Work")
            for issue in open_issues:
                labels = ", ".join(issue.get("labels", []))
                parts.append(f"- #{issue['number']}: {issue['title']} [{labels}]")
            parts.append("")

        # Constraints
        if self.config.constraints_path.exists():
            constraints = self.config.constraints_path.read_text()
            if constraints.strip():
                parts.append("## Active Constraints")
                parts.append(constraints)
                parts.append("")

        # Recent git activity
        try:
            git_log = subprocess.check_output(
                ["git", "log", "--oneline", "-20", "--no-merges"],
                cwd=self.config.project_dir,
            ).decode().strip()
            if git_log:
                parts.append("## Recent Commits")
                parts.append(git_log)
                parts.append("")
        except subprocess.CalledProcessError:
            pass

        return "\n".join(parts)
