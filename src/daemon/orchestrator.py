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
import json
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
        """Gracefully stop: kill current agent, save session for resume."""
        self._running = False
        if self._current_agent:
            logger.info("Stopping current agent gracefully...")
            claude_sid = self._current_agent.get_claude_session_id()
            await self._current_agent.kill()
            if self._current_task_id:
                await self.db.update_task(
                    self._current_task_id,
                    status="interrupted",
                    claude_session=claude_sid,
                )
                logger.info(f"Task #{self._current_task_id} interrupted (session saved)")

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
        task = await self.db.pick_next_task()
        if task:
            await self._engineer(task)
        else:
            await self._research()

        # Update activity summary in background (non-blocking)
        asyncio.create_task(self._update_activity())

    async def _engineer(self, task: dict) -> None:
        """Implement a single task, or resume an interrupted one."""
        task_id = task["id"]
        resume_session = task.get("claude_session")
        resuming = task["status"] == "interrupted" and resume_session

        if resuming:
            logger.info(f"Resuming task #{task_id}: {task['title']}")
            prompt = "Continue where you left off. Check the current state of your work and proceed."
        else:
            logger.info(f"Implementing task #{task_id}: {task['title']}")
            prompt = self._build_engineer_prompt(task)

        await self.db.update_task(task_id, status="in_progress")
        self._current_task_id = task_id

        agent = BaseAgent(
            agent_type=AgentType.ENGINEER,
            config=self.config,
            db=self.db,
            task_id=task_id,
        )
        self._current_agent = agent
        self._current_session = await agent.spawn(
            prompt, interactive=False,
            resume_session=resume_session if resuming else None,
        )
        exit_code = await agent.wait()

        # Capture Claude's session ID for potential resumption
        claude_sid = agent.get_claude_session_id()

        self._current_agent = None
        self._current_session = None
        self._current_task_id = None

        if exit_code == 0:
            await self.db.update_task(task_id, status="completed", claude_session=None)
        elif exit_code < 0 or not self._running:
            # Killed by signal or orchestrator shutting down — save session for resume
            await self.db.update_task(task_id, status="interrupted", claude_session=claude_sid)
            logger.info(f"Task #{task_id} interrupted (session saved for resume)")
        else:
            await self.db.update_task(task_id, status="failed", claude_session=claude_sid)
            logger.warning(f"Task #{task_id} failed (exit {exit_code})")

    async def _research(self) -> None:
        """Analyze state and create new tasks."""
        logger.info("Generating new tasks...")

        # Clear any stale drop file
        drop_file = self.config.daemon_dir / "new_tasks.json"
        drop_file.unlink(missing_ok=True)

        context = await self._build_researcher_context()
        prompt = (
            "Analyze the following research state and create 2-3 tasks "
            "by writing them to `.phd/new_tasks.json`.\n\n"
            + (context or "No prior work exists. This is a new project. "
               "Read the codebase to understand what it does, then create "
               "initial tasks.")
        )

        agent = BaseAgent(
            agent_type=AgentType.RESEARCHER,
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
            logger.warning(f"Researcher failed (exit {exit_code})")

        # Import tasks from drop file
        await self._import_tasks(drop_file)

    async def _import_tasks(self, path) -> None:
        """Read tasks from a JSON drop file and create them in the DB."""
        if not path.exists():
            return
        try:
            tasks = json.loads(path.read_text())
            if not isinstance(tasks, list):
                tasks = [tasks]
            for t in tasks:
                if not isinstance(t, dict) or "title" not in t:
                    continue
                task_id = await self.db.create_task(
                    title=t["title"],
                    description=t.get("description", ""),
                    dependencies=t.get("depends_on", []),
                )
                logger.info(f"Imported task #{task_id}: {t['title']}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse new_tasks.json: {e}")
        finally:
            path.unlink(missing_ok=True)

    def _build_engineer_prompt(self, task: dict) -> str:
        """Build context for an engineer agent."""
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

    async def _build_researcher_context(self) -> str:
        """Build context for the researcher agent."""
        parts = []

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

    async def _update_activity(self) -> None:
        """Generate an LLM-written activity summary for the dashboard."""
        try:
            git_log = ""
            try:
                git_log = subprocess.check_output(
                    ["git", "log", "--oneline", "-15", "--no-merges"],
                    cwd=self.config.project_dir,
                ).decode().strip()
            except subprocess.CalledProcessError:
                pass

            completed = await self.db.list_tasks(status="completed")
            in_progress = await self.db.list_tasks(status="in_progress")
            failed = await self.db.list_tasks(status="failed")

            task_lines = []
            for t in completed[-10:]:
                task_lines.append(f"- [completed] #{t['id']}: {t['title']}")
            for t in in_progress:
                task_lines.append(f"- [in progress] #{t['id']}: {t['title']}")
            for t in failed[-5:]:
                task_lines.append(f"- [failed] #{t['id']}: {t['title']}")

            if not git_log and not task_lines:
                return

            prompt = (
                "Write a concise activity summary (5-8 bullet points) for a "
                "research project dashboard. Focus on what was accomplished and "
                "what's in flight. Be specific. No headers, just bullets.\n\n"
                f"## Recent Commits\n{git_log or 'None'}\n\n"
                f"## Task Activity\n" + ("\n".join(task_lines) or "None")
            )

            proc = await asyncio.create_subprocess_exec(
                "claude", "--print", "--model", "claude-haiku-4-5-20251001",
                "--max-turns", "1", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.config.project_dir,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode == 0 and stdout:
                activity_path = self.config.daemon_dir / "activity.md"
                activity_path.write_text(stdout.decode().strip())
        except Exception:
            logger.debug("Activity summary update failed", exc_info=True)
