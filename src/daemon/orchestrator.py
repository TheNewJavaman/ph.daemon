"""Single orchestrator loop that manages all agent work.

Each cycle:
1. Read human messages
2. Analyze state (tasks, constraints, research)
3. Decide: create new tasks, implement an existing task, or wait
4. Spawn a Claude session to execute
5. Wait, update status, repeat
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

from daemon.agents.base import AgentType, BaseAgent
from daemon.config import ProjectConfig
from daemon.db import Database

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: ProjectConfig, db: Database) -> None:
        self.config = config
        self.db = db
        self._paused = False
        self._running = False
        self._current_session: str | None = None
        self._current_agent: BaseAgent | None = None
        self._current_task_id: int | None = None

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        logger.info("Orchestrator paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Orchestrator resumed")

    async def stop(self) -> None:
        """Gracefully stop: kill current agent, reset its task to open."""
        self._running = False
        if self._current_agent:
            logger.info("Stopping current agent gracefully...")
            await self._current_agent.kill()
            # Reset the task so it gets picked up on restart
            if self._current_task_id:
                await self.db.update_task(self._current_task_id, status="open")
                logger.info(f"Task #{self._current_task_id} reset to open")

    async def run(self) -> None:
        self._running = True
        logger.info("Orchestrator started")
        try:
            while self._running:
                if self._paused:
                    await asyncio.sleep(5)
                    continue

                await self._cycle()
                await asyncio.sleep(30)
        finally:
            self._running = False
            logger.info("Orchestrator stopped")

    async def _cycle(self) -> None:
        """One orchestrator cycle: decide what to do and do it."""
        # Check for a task to implement first
        task = await self.db.pick_next_task()
        if task:
            await self._implement(task)
            return

        # No ready tasks — check if we should create some
        open_tasks = await self.db.list_tasks(status="open")
        in_progress = await self.db.list_tasks(status="in_progress")
        if len(open_tasks) + len(in_progress) <= 3:
            await self._direct()

    async def _implement(self, task: dict) -> None:
        """Implement a single task."""
        task_id = task["id"]
        logger.info(f"Implementing task #{task_id}: {task['title']}")
        await self.db.update_task(task_id, status="in_progress")
        self._current_task_id = task_id

        prompt = self._build_impl_prompt(task)

        agent = BaseAgent(
            agent_type=AgentType.IMPLEMENTOR,
            config=self.config,
            db=self.db,
            task_id=task_id,
        )
        self._current_agent = agent
        self._current_session = await agent.spawn(prompt, interactive=False)
        exit_code = await agent.wait()
        self._current_agent = None
        self._current_session = None
        self._current_task_id = None

        if exit_code == 0:
            await self.db.update_task(task_id, status="completed")
        else:
            await self.db.update_task(task_id, status="failed")
            logger.warning(f"Task #{task_id} failed (exit {exit_code})")

    async def _direct(self) -> None:
        """Analyze state and create new tasks."""
        logger.info("Generating new tasks...")

        context = await self._build_director_context()
        prompt = (
            "Analyze the following research state and create 2-3 tasks "
            "for the highest-value next work. Use `phd create-task` to create "
            "each task.\n\n"
            + (context or "No prior work exists. This is a new project. "
               "Read the codebase to understand what it does, then create "
               "initial tasks.")
        )

        agent = BaseAgent(
            agent_type=AgentType.DIRECTOR,
            config=self.config,
            db=self.db,
            task_id=None,
        )
        self._current_agent = agent
        self._current_session = await agent.spawn(prompt, interactive=False)
        exit_code = await agent.wait()
        self._current_agent = None
        self._current_session = None

        if exit_code != 0:
            logger.warning(f"Director failed (exit {exit_code})")

    def _build_impl_prompt(self, task: dict) -> str:
        """Build context for an implementor agent."""
        # Include any unread human messages as directives
        parts = [
            f"## Current Task: #{task['id']}",
            f"**Title:** {task['title']}",
            "",
            task.get("description", ""),
            "",
        ]

        if self.config.constraints_path.exists():
            constraints = self.config.constraints_path.read_text()
            if constraints.strip():
                parts.append("## Active Constraints")
                parts.append(constraints)
                parts.append("")

        if self.config.research_state_path.exists():
            state = self.config.research_state_path.read_text()
            if state.strip():
                parts.append("## Research State")
                parts.append(state)

        return "\n".join(parts)

    async def _build_director_context(self) -> str:
        """Build context for the director agent."""
        parts = []

        # Human messages
        messages = await self.db.read_messages()
        if messages:
            parts.append("## Human Messages")
            for m in messages:
                parts.append(f"- {m['content']}")
            parts.append("")

        if self.config.research_state_path.exists():
            state = self.config.research_state_path.read_text()
            if state.strip():
                parts.append("## Current Research State")
                parts.append(state)
                parts.append("")

        paper_tex = self.config.paper_dir / "main.tex"
        if paper_tex.exists():
            parts.append("## Current Paper (main.tex)")
            parts.append(paper_tex.read_text()[:50000])
            parts.append("")

        completed = await self.db.list_tasks(status="completed")
        if completed:
            parts.append("## Completed Work")
            for t in completed[-20:]:
                parts.append(f"- #{t['id']}: {t['title']}")
            parts.append("")

        open_tasks = await self.db.list_tasks(status="open")
        in_progress = await self.db.list_tasks(status="in_progress")
        queued = open_tasks + in_progress
        if queued:
            parts.append("## Currently Queued Work")
            for t in queued:
                parts.append(f"- #{t['id']}: {t['title']} [{t['status']}]")
            parts.append("")

        if self.config.constraints_path.exists():
            constraints = self.config.constraints_path.read_text()
            if constraints.strip():
                parts.append("## Active Constraints")
                parts.append(constraints)
                parts.append("")

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
