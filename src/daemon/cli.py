from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click
import uvicorn

from daemon.config import ProjectConfig

logger = logging.getLogger(__name__)


def _find_project_dir() -> Path:
    """Walk up from cwd to find a .ph.daemon directory."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".ph.daemon").exists():
            return current
        current = current.parent
    click.echo("Error: not inside a ph.daemon project. Run `phd init` first.", err=True)
    sys.exit(1)


def _get_config() -> ProjectConfig:
    project_dir = _find_project_dir()
    return ProjectConfig.load(project_dir)


@click.group()
def main() -> None:
    """ph.daemon — automated research harness."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@main.command()
@click.argument("project_path", type=click.Path())
@click.option("--repo", required=True, help="GitHub repo in owner/repo format")
def init(project_path: str, repo: str) -> None:
    """Initialize a new research project."""
    project = Path(project_path).resolve()
    config = ProjectConfig(project_dir=project, repo=repo)

    # Create directory structure
    config.daemon_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    (project / "docs").mkdir(parents=True, exist_ok=True)
    config.paper_dir.mkdir(parents=True, exist_ok=True)
    (project / ".claude").mkdir(parents=True, exist_ok=True)

    # Save config
    config.save()

    # CLAUDE.md
    claude_md = project / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(
            "# Research Project\n\n"
            "## Daemon\n\n"
            "This project is managed by ph.daemon. All work is tracked in GitHub issues.\n\n"
            "- Every code change must reference a GitHub issue number in the commit message\n"
            "- Before starting work, check existing issues (open and closed) for prior decisions\n"
            "- After each commit, update the linked issue with a summary of changes\n\n"
            "## Constraints\n\n"
            "@docs/constraints.md\n\n"
            "## Paper\n\n"
            "The research paper lives in `paper/`. Only the paper writer agent modifies it.\n"
            "Do not edit `paper/` directly during implementation tasks.\n"
        )

    # docs/constraints.md
    constraints = config.constraints_path
    if not constraints.exists():
        constraints.write_text(
            "# Constraints\n\n"
            "Rules that must always be followed. Each constraint was added because the LLM\n"
            "made a mistake or the human wants to enforce a specific approach.\n\n"
            "<!-- Constraints are append-only. To remove one, discuss via `phd ask` first. -->\n"
        )

    # docs/research-state.md
    research_state = config.research_state_path
    if not research_state.exists():
        research_state.write_text(
            "# Research State\n\n"
            "Last updated: (not yet)\n\n"
            "## Current Results\n\nNo results yet.\n\n"
            "## Paper Readiness\n\nPaper not started.\n\n"
            "## Active Hypotheses\n\nNone yet.\n\n"
            "## Optimization Frontier\n\nNo optimizations yet.\n\n"
            "## Dataset Status\n\nNo dataset yet.\n\n"
            "## Next Priorities\n\nAwaiting first task.\n"
        )

    # .gitignore
    gitignore = project / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    if ".ph.daemon/" not in lines:
        lines.append(".ph.daemon/")
        gitignore.write_text("\n".join(lines) + "\n")

    # .claude/settings.json — post-commit hook
    settings_path = project / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
    settings.setdefault("hooks", {})
    settings["hooks"]["PostCommit"] = [
        {
            "command": "phd hook post-commit",
            "description": "Update linked GitHub issue with commit discussion",
        }
    ]
    settings_path.write_text(json.dumps(settings, indent=2))

    click.echo(f"Initialized ph.daemon project at {project}")
    click.echo(f"  GitHub repo: {repo}")
    click.echo(f"  Run `cd {project} && phd start` to begin")


@main.command()
@click.option("--port", default=8666, help="Web UI port")
def start(port: int) -> None:
    """Start the web UI and implementor loop."""
    config = _get_config()
    click.echo(f"Starting ph.daemon for {config.repo}")
    click.echo(f"Web UI: http://localhost:{port}")

    # Import here to avoid circular imports
    from daemon.app import create_app

    app = create_app(config)
    uvicorn.run(app, host="127.0.0.1", port=port)


@main.command()
@click.argument("description")
def task(description: str) -> None:
    """Submit a task (opens interactive session to refine, then plans)."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.github.issues import GitHubIssues
        from daemon.agents.ephemeral import run_ephemeral_interactive
        from daemon.agents.planner import run_planner

        db = Database(config.db_path)
        await db.init()
        gh = GitHubIssues(config=config, db=db)

        try:
            # Phase 1: Interactive refinement
            click.echo("Opening interactive session to refine your task...")
            click.echo("When done, exit the session and the planner will create issues.")
            await run_ephemeral_interactive(config, db, f"Help me refine this task: {description}")

            # Phase 2: Planner creates issues
            click.echo("\nDispatching planner to create issues...")
            await run_planner(config, db, gh, description)
            click.echo("Done. Issues created. The implementor will pick them up.")
        finally:
            await db.close()

    asyncio.run(_run())


@main.command()
@click.argument("description")
def constrain(description: str) -> None:
    """Add a constraint (opens interactive session to refine)."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.agents.ephemeral import run_ephemeral_interactive

        db = Database(config.db_path)
        await db.init()

        prompt = (
            f"The human wants to add a constraint: {description}\n\n"
            "Help them refine it. When done:\n"
            "1. Append the constraint to `docs/constraints.md` with the standard format "
            "(numbered, dated, with rationale)\n"
            f"2. Create a GitHub issue with label `ph:constraint` using "
            f"`gh issue create --repo {config.repo}`\n"
            "3. Commit the constraints.md change referencing the issue"
        )

        try:
            await run_ephemeral_interactive(config, db, prompt)
        finally:
            await db.close()

    asyncio.run(_run())


@main.command()
@click.argument("question")
def ask(question: str) -> None:
    """Ask a question about the project (interactive Q&A)."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.agents.ephemeral import run_ephemeral_interactive

        db = Database(config.db_path)
        await db.init()
        try:
            await run_ephemeral_interactive(config, db, question)
        finally:
            await db.close()

    asyncio.run(_run())


@main.command()
def paper() -> None:
    """Trigger a manual paper update."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.agents.paper import run_paper_update

        db = Database(config.db_path)
        await db.init()
        try:
            click.echo("Updating paper based on recent commits...")
            session_id = await run_paper_update(config, db)
            if session_id:
                click.echo(f"Paper update complete (session: {session_id})")
            else:
                click.echo("No new commits to incorporate.")
        finally:
            await db.close()

    asyncio.run(_run())


@main.command()
def status() -> None:
    """Show agent status and issue queue."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database

        db = Database(config.db_path)
        await db.init()
        try:
            running = await db.list_sessions(status="running")
            open_issues = await db.list_issues(state="open")

            click.echo(f"Project: {config.repo}")
            click.echo(f"Running agents: {len(running)}")
            for s in running:
                click.echo(f"  [{s['agent_type']}] session {s['id']}"
                          f"{' → #' + str(s['issue_id']) if s['issue_id'] else ''}")
            click.echo(f"Open issues: {len(open_issues)}")
        finally:
            await db.close()

    asyncio.run(_run())


@main.group()
def hook() -> None:
    """Hook commands (called by Claude Code, not humans)."""
    pass


@hook.command("post-commit")
def hook_post_commit() -> None:
    """Post-commit hook: update linked GitHub issue with discussion."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.github.hooks import run_post_commit_hook

        db = Database(config.db_path)
        await db.init()
        try:
            await run_post_commit_hook(config, db)
        finally:
            await db.close()

    asyncio.run(_run())
