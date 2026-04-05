from __future__ import annotations

import asyncio
import enum
import signal
import uuid
from datetime import datetime, timezone
from pathlib import Path

from daemon.config import ProjectConfig
from daemon.db import Database


class AgentType(enum.StrEnum):
    PLANNER = "planner"
    IMPLEMENTOR = "implementor"
    PAPER = "paper"
    EPHEMERAL = "ephemeral"


# Prompt file basenames per agent type
_PROMPT_FILES = {
    AgentType.PLANNER: "planner.md",
    AgentType.IMPLEMENTOR: "implementor.md",
    AgentType.PAPER: "paper.md",
    AgentType.EPHEMERAL: "ephemeral.md",
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
        issue_id: int | None = None,
    ) -> None:
        self.agent_type = agent_type
        self.config = config
        self.db = db
        self.issue_id = issue_id
        self.session_id: str | None = None
        self._proc: asyncio.subprocess.Process | None = None

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
        prompt: str,
        interactive: bool = False,
    ) -> list[str]:
        """Build the claude CLI command."""
        cmd = [
            "claude",
            "--model", "claude-opus-4-6",
            "--max-turns", "100",
            "--dangerously-skip-permissions",
            "--append-system-prompt", prompt,
        ]
        if not interactive:
            cmd.extend(["--print", "--output-format", "json"])
        return cmd

    async def spawn(
        self,
        prompt: str,
        interactive: bool = False,
    ) -> str:
        """Spawn a claude subprocess and track it in the database."""
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)

        # Pre-generate session_id so log_path property works before DB call
        self.session_id = uuid.uuid4().hex[:12]

        # Create session record
        self.session_id = await self.db.create_session(
            agent_type=self.agent_type.value,
            issue_id=self.issue_id,
            log_path=str(self.log_path),
            session_id=self.session_id,
        )

        full_prompt = self._load_prompt() + "\n\n" + prompt
        cmd = self.build_command(full_prompt, interactive=interactive)

        if interactive:
            # Interactive: connect to terminal directly
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.config.project_dir,
            )
        else:
            # Non-interactive: capture output to log file
            log_file = open(self.log_path, "w")
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.config.project_dir,
            )

        await self.db.update_session(self.session_id, pid=self._proc.pid)
        return self.session_id

    async def wait(self) -> int:
        """Wait for the subprocess to finish. Returns exit code."""
        assert self._proc is not None
        code = await self._proc.wait()
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
        now = datetime.now(timezone.utc).isoformat()
        await self.db.update_session(
            self.session_id, status="killed", ended_at=now
        )
