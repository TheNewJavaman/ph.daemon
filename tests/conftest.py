from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from daemon.config import ProjectConfig
from daemon.db import Database


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a temporary project directory."""
    d = tmp_path / "test-project"
    d.mkdir()
    (d / ".ph.daemon").mkdir()
    (d / ".ph.daemon" / "logs").mkdir()
    return d


@pytest.fixture
def config(project_dir: Path) -> ProjectConfig:
    return ProjectConfig(project_dir=project_dir, repo="test-owner/test-repo")


@pytest.fixture
async def db(config: ProjectConfig) -> Database:
    database = Database(config.db_path)
    await database.init()
    yield database
    await database.close()
