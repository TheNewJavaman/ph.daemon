from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from daemon.cli import main


def test_init_creates_scaffold(tmp_path: Path) -> None:
    project = tmp_path / "my-research"
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(project), "--repo", "me/my-research"])
    assert result.exit_code == 0

    assert (project / ".ph.daemon" / "config.json").exists()
    assert (project / ".ph.daemon" / "logs").exists()
    assert (project / "CLAUDE.md").exists()
    assert (project / "docs" / "constraints.md").exists()
    assert (project / "paper").exists()
    assert (project / ".gitignore").exists()
    assert ".ph.daemon/" in (project / ".gitignore").read_text()


def test_init_creates_claude_settings(tmp_path: Path) -> None:
    project = tmp_path / "my-research"
    runner = CliRunner()
    runner.invoke(main, ["init", str(project), "--repo", "me/my-research"])

    settings_path = project / ".claude" / "settings.json"
    assert settings_path.exists()
    import json
    settings = json.loads(settings_path.read_text())
    assert "hooks" in settings
