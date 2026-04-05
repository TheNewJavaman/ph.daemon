from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectConfig:
    """Configuration for a target research project."""

    project_dir: Path
    repo: str  # "owner/repo" format

    @property
    def daemon_dir(self) -> Path:
        return self.project_dir / ".ph.daemon"

    @property
    def db_path(self) -> Path:
        return self.daemon_dir / "daemon.db"

    @property
    def logs_dir(self) -> Path:
        return self.daemon_dir / "logs"

    @property
    def constraints_path(self) -> Path:
        return self.project_dir / "docs" / "constraints.md"

    @property
    def paper_dir(self) -> Path:
        return self.project_dir / "paper"

    @property
    def research_state_path(self) -> Path:
        return self.project_dir / "docs" / "research-state.md"

    @classmethod
    def load(cls, project_dir: Path) -> ProjectConfig:
        """Load config from .ph.daemon/config.json."""
        config_path = project_dir / ".ph.daemon" / "config.json"
        data = json.loads(config_path.read_text())
        return cls(project_dir=project_dir, repo=data["repo"])

    def save(self) -> None:
        """Save config to .ph.daemon/config.json."""
        config_path = self.daemon_dir / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"repo": self.repo}, indent=2))
