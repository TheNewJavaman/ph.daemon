from __future__ import annotations

import asyncio
import enum
import json
import signal
import uuid
from datetime import datetime, timezone
from pathlib import Path

from daemon.config import ProjectConfig
from daemon.db import Database


class AgentType(enum.StrEnum):
    PLANNER = "planner"
    ENGINEER = "engineer"
    PAPER = "paper"
    EPHEMERAL = "ephemeral"
    RESEARCHER = "researcher"


# Prompt file basenames per agent type
_PROMPT_FILES = {
    AgentType.PLANNER: "planner.md",
    AgentType.ENGINEER: "engineer.md",
    AgentType.PAPER: "paper.md",
    AgentType.EPHEMERAL: "ephemeral.md",
    AgentType.RESEARCHER: "researcher.md",
}


def _prompts_dir() -> Path:
    """Locate the prompts/ directory relative to the package."""
    return Path(__file__).resolve().parent.parent.parent / "prompts"


class BaseAgent:
    """Manages a single claude CLI subprocess."""

    def __init__(
        self,
        agent_type: AgentType,
        config: ProjectConfig,
        db: Database,
        task_id: int | None = None,
    ) -> None:
        self.agent_type = agent_type
        self.config = config
        self.db = db
        self.task_id = task_id
        self.session_id: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._log_file = None

    @property
    def log_path(self) -> Path:
        assert self.session_id is not None
        return self.config.logs_dir / f"{self.session_id}.jsonl"

    def _load_prompt(self) -> str:
        """Load the base prompt for this agent type."""
        prompt_file = _prompts_dir() / _PROMPT_FILES[self.agent_type]
        if prompt_file.exists():
            return prompt_file.read_text()
        return f"You are a {self.agent_type.value} agent."

    def build_command(
        self,
        system_prompt: str,
        user_prompt: str,
        interactive: bool = False,
        resume_session: str | None = None,
    ) -> list[str]:
        """Build the claude CLI command."""
        cmd = ["claude"]
        if resume_session:
            cmd.extend(["--resume", resume_session])
        cmd.extend([
            "--model", "claude-opus-4-6",
            "--max-turns", "100",
            "--dangerously-skip-permissions",
            "--append-system-prompt", system_prompt,
        ])
        if not interactive:
            cmd.extend(["--verbose", "--print", "--output-format",
                         "stream-json", user_prompt])
        return cmd

    async def spawn(
        self,
        prompt: str,
        interactive: bool = False,
        resume_session: str | None = None,
        reuse_session: str | None = None,
    ) -> str:
        """Spawn a claude subprocess and track it in the database.

        Args:
            reuse_session: If set, reuse this phd session record instead of creating a new one.
        """
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)

        if reuse_session:
            self.session_id = reuse_session
            await self.db.update_session(
                self.session_id, status="running", ended_at=None,
            )
        else:
            self.session_id = uuid.uuid4().hex[:12]
            self.session_id = await self.db.create_session(
                agent_type=self.agent_type.value,
                task_id=self.task_id,
                log_path=str(self.log_path),
                session_id=self.session_id,
            )

        system_prompt = self._load_prompt()
        if interactive:
            cmd = self.build_command(
                system_prompt + "\n\n" + prompt, prompt,
                interactive=True, resume_session=resume_session,
            )
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.config.project_dir,
            )
        else:
            cmd = self.build_command(
                system_prompt, prompt,
                interactive=False, resume_session=resume_session,
            )
            mode = "a" if reuse_session else "w"
            self._log_file = open(self.log_path, mode)
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=self._log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.config.project_dir,
            )

        await self.db.update_session(self.session_id, pid=self._proc.pid)
        return self.session_id

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def get_claude_session_id(self) -> str | None:
        """Extract Claude's session ID from the log file."""
        try:
            with open(self.log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if "session_id" in data:
                        return data["session_id"]
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return None

    async def wait(self) -> int:
        """Wait for the subprocess to finish. Returns exit code."""
        assert self._proc is not None
        code = await self._proc.wait()
        self._close_log()
        now = datetime.now(timezone.utc).isoformat()
        status = "completed" if code == 0 else "failed"
        await self.db.update_session(
            self.session_id, status=status, ended_at=now
        )
        return code

    async def kill(self) -> None:
        """Send SIGTERM, wait 5s, then SIGKILL."""
        if self._proc is None or self._proc.returncode is not None:
            return
        self._proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        self._close_log()
        now = datetime.now(timezone.utc).isoformat()
        await self.db.update_session(
            self.session_id, status="interrupted", ended_at=now
        )
