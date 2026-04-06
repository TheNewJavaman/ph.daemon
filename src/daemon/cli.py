from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click

from daemon.config import ProjectConfig

logger = logging.getLogger(__name__)


def _ensure_init(project: Path) -> None:
    """Initialize .phd/ if it doesn't exist. Idempotent."""
    config = ProjectConfig(project_dir=project)

    config.daemon_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    (project / "docs").mkdir(parents=True, exist_ok=True)
    config.paper_dir.mkdir(parents=True, exist_ok=True)

    config.save()

    phd_gitignore = config.daemon_dir / ".gitignore"
    if not phd_gitignore.exists():
        phd_gitignore.write_text("*\n")

    claude_md = project / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(
            "# Research Project\n\n"
            "## Daemon\n\n"
            "This project is managed by ph.daemon. All work is tracked as local tasks.\n\n"
            "- Every code change must reference a task number in the commit message\n"
            "- Use `phd create-task` to create new tasks\n\n"
            "## Constraints\n\n"
            "@docs/constraints.md\n\n"
            "## Paper\n\n"
            "The research paper lives in `paper/`. Only the paper writer agent modifies it.\n"
        )

    constraints = config.constraints_path
    if not constraints.exists():
        constraints.write_text(
            "# Constraints\n\n"
            "Rules that must always be followed.\n\n"
        )

    research_state = config.research_state_path
    if not research_state.exists():
        research_state.write_text(
            "# Research State\n\n"
            "Last updated: (not yet)\n\n"
            "## Current Results\n\nNo results yet.\n\n"
            "## Next Priorities\n\nAwaiting first task.\n"
        )

    gitignore = project / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    if ".phd/" not in lines:
        lines.append(".phd/")
        gitignore.write_text("\n".join(lines) + "\n")


def _find_project_dir() -> Path:
    """Walk up from cwd to find a .phd directory, or use cwd."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".phd").exists():
            return current
        current = current.parent
    return Path.cwd()


def _get_config() -> ProjectConfig:
    project_dir = _find_project_dir()
    _ensure_init(project_dir)
    return ProjectConfig.load(project_dir)


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """ph.daemon — automated research harness."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if ctx.invoked_subcommand is None:
        config = _get_config()
        # Redirect logging to a file so it doesn't corrupt the TUI
        log_file = config.daemon_dir / "daemon.log"
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s: %(message)s",
            filename=str(log_file),
        )
        from daemon.tui import DaemonApp
        try:
            DaemonApp(config=config).run()
        finally:
            # Reset terminal in case a subprocess left it in a bad state
            import os
            os.system("stty sane 2>/dev/null; tput reset 2>/dev/null")


@main.command("reset-task")
@click.argument("task_id", type=int)
def reset_task(task_id: int) -> None:
    """Reset a failed/in-progress task back to open."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database

        db = Database(config.db_path)
        await db.init()
        try:
            task = await db.get_task(task_id)
            if not task:
                click.echo(f"Task #{task_id} not found", err=True)
                return
            await db.update_task(task_id, status="open")
            click.echo(f"Task #{task_id} reset to open")
        finally:
            await db.close()

    asyncio.run(_run())


@main.command("create-task")
@click.argument("title")
@click.option("--description", "-d", default="", help="Task description")
@click.option("--priority", "-p", default=1, type=int, help="0=human, 1=auto")
@click.option("--depends-on", multiple=True, type=int, help="Task IDs this depends on")
def create_task(title: str, description: str, priority: int, depends_on: tuple[int, ...]) -> None:
    """Create a new task."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database

        db = Database(config.db_path)
        await db.init()
        try:
            task_id = await db.create_task(
                title=title,
                description=description,
                priority=priority,
                dependencies=list(depends_on),
            )
            click.echo(f"Created task #{task_id}: {title}")
        finally:
            await db.close()

    asyncio.run(_run())


@main.command()
@click.argument("description")
def task(description: str) -> None:
    """Submit a task (opens interactive planner session)."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database
        from daemon.agents.planner import run_planner_interactive

        db = Database(config.db_path)
        await db.init()

        try:
            click.echo("Opening interactive planner session...")
            click.echo("Refine the task, then the planner will create subtasks.")
            await run_planner_interactive(config, db, description)
            click.echo("Done. Tasks created. The engineer will pick them up.")
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
            "Help them refine it. When done, append the constraint to "
            "`docs/constraints.md` with the standard format "
            "(numbered, dated, with rationale), then commit the change."
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
    """Show agent status and task queue."""
    config = _get_config()

    async def _run() -> None:
        from daemon.db import Database

        db = Database(config.db_path)
        await db.init()
        try:
            running = await db.list_sessions(status="running")
            open_tasks = await db.list_tasks(status="open")
            in_progress = await db.list_tasks(status="in_progress")

            click.echo(f"Running agents: {len(running)}")
            for s in running:
                click.echo(f"  [{s['agent_type']}] session {s['id']}"
                          f"{' → #' + str(s['task_id']) if s['task_id'] else ''}")
            click.echo(f"Open tasks: {len(open_tasks)}")
            click.echo(f"In progress: {len(in_progress)}")
        finally:
            await db.close()

    asyncio.run(_run())
