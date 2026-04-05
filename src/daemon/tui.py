"""Terminal UI for ph.daemon using Textual."""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import (
    DataTable, Footer, Header, Input, Label, ListItem, ListView, RichLog, Static,
)

from daemon.config import ProjectConfig
from daemon.db import Database
from daemon.orchestrator import Orchestrator


# --- Log formatting (ported from sse.py → Rich Text) ---


def _tool_summary(name: str, inp: dict) -> str:
    if name in ("Read", "Write", "Edit"):
        return inp.get("file_path", "")
    if name == "Bash":
        cmd = inp.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    if name == "Grep":
        p, path = inp.get("pattern", ""), inp.get("path", "")
        return f'"{p}" in {path}' if path else f'"{p}"'
    if name == "Glob":
        return inp.get("pattern", "")
    for v in inp.values():
        if isinstance(v, str) and v:
            return v[:60] + ("..." if len(v) > 60 else "")
    return ""


def _format_log_line(raw: str) -> Text | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Text(raw)

    t = data.get("type", "")

    if t in ("system", "rate_limit_event"):
        return None

    if t == "user":
        msg = data.get("message", {})
        blocks = msg.get("content", [])
        texts = [
            b.get("text", "").strip()
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        text = " ".join(texts)
        if len(text) > 200:
            text = text[:200] + "…"
        return Text(f"▶ {text}", style="dim italic") if text else None

    if t == "assistant":
        msg = data.get("message", {})
        blocks = msg.get("content", [])
        result = Text()
        for b in blocks:
            bt = b.get("type", "")
            if bt == "text":
                text = b.get("text", "").strip()
                if text:
                    result.append(text + "\n")
            elif bt == "tool_use":
                name = b.get("name", "?")
                summary = _tool_summary(name, b.get("input", {}))
                result.append(f"▸ {name}", style="bold magenta")
                result.append(f" {summary}\n", style="dim")
        return result if result.plain.strip() else None

    if t == "tool":
        content = data.get("content", "")
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            content = "\n".join(texts)
        if isinstance(content, str):
            lines = content.split("\n")
            if len(lines) > 5:
                content = "\n".join(lines[:5]) + f"\n… ({len(lines) - 5} more lines)"
            elif len(content) > 300:
                content = content[:300] + "…"
        return Text(f"◂ {content}", style="dim")

    if t == "result":
        cost = data.get("cost_usd", 0)
        turns = data.get("num_turns", "?")
        is_error = data.get("is_error", False)
        lbl = "Error" if is_error else "Completed"
        style = "red bold" if is_error else "green bold"
        return Text(f"── {lbl} ({turns} turns, ${cost:.4f}) ──", style=style)

    return None


def _humantime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%b %-d, %-I:%M %p")
    except (ValueError, TypeError):
        return value


# --- Session log viewer ---


class SessionScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, session: dict, db: Database):
        super().__init__()
        self.session = session
        self._db = db

    def compose(self) -> ComposeResult:
        s = self.session
        yield Header()
        yield Static(
            f"[bold]{s['agent_type']}[/] session {s['id']}  "
            f"Task: {'#' + str(s['task_id']) if s['task_id'] else '—'}  "
            f"Status: {s['status']}  "
            f"Started: {_humantime(s['started_at'])}",
        )
        yield RichLog(id="log", highlight=True, markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.run_worker(self._tail_log())

    async def _tail_log(self) -> None:
        log_widget = self.query_one("#log", RichLog)
        path = Path(self.session["log_path"])
        if not path.exists():
            log_widget.write(Text("Log file not found.", style="red"))
            return
        with open(path) as f:
            while True:
                line = f.readline()
                if line:
                    formatted = _format_log_line(line)
                    if formatted:
                        log_widget.write(formatted)
                else:
                    session = await self._db.get_session(self.session["id"])
                    if session and session["status"] != "running":
                        break
                    await asyncio.sleep(0.5)


# --- Main application ---


_PHD_THEME = Theme(
    name="phd",
    primary="ansi_blue",
    secondary="ansi_cyan",
    warning="ansi_yellow",
    error="ansi_red",
    success="ansi_green",
    accent="ansi_magenta",
    foreground="ansi_default",
    background="ansi_default",
    surface="ansi_default",
    panel="ansi_default",
    boost="ansi_default",
    dark=True,
    variables={
        "input-selection-background": "ansi_blue",
        "input-cursor-text-style": "reverse",
        "scrollbar": "ansi_bright_black",
        "border": "ansi_bright_black",
        "border-blurred": "ansi_bright_black",
    },
)


class DaemonApp(App):
    TITLE = "ph.daemon"
    theme = "phd"

    CSS = """
    Screen { background: ansi_default; color: ansi_default; }
    #sidebar {
        width: 24; dock: left;
        background: ansi_black; border-right: solid ansi_bright_black;
        padding: 1 0;
    }
    #sidebar-title {
        color: ansi_magenta; text-style: bold;
        padding: 0 2; margin-bottom: 1;
    }
    #nav { background: transparent; }
    #nav > ListItem { color: ansi_bright_black; padding: 0 2; }
    #nav > ListItem.-highlight { background: ansi_default; color: ansi_default; }
    #content { padding: 1 2; }
    #status-bar { height: auto; margin-bottom: 1; color: ansi_default; }
    DataTable { height: 1fr; background: ansi_default; }
    DataTable > .datatable--header { color: ansi_bright_black; text-style: bold; }
    DataTable > .datatable--cursor { background: ansi_bright_black; color: ansi_white; }
    #detail { margin-top: 1; height: auto; color: ansi_bright_black; }
    #msg-input {
        dock: bottom; margin-top: 1;
        background: ansi_black; color: ansi_default;
        border: solid ansi_bright_black;
    }
    Header { background: ansi_black; color: ansi_magenta; }
    Footer { background: ansi_black; color: ansi_bright_black; }
    FooterKey { background: ansi_black; color: ansi_bright_black; }
    FooterKey:hover { background: ansi_bright_black; }
    FooterKey.-compact .footer-key--key { color: ansi_magenta; }
    RichLog { background: ansi_black; border: solid ansi_bright_black; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "toggle_pause", "Pause/Resume"),
    ]

    def __init__(self, config: ProjectConfig):
        super().__init__()
        self.register_theme(_PHD_THEME)
        self.config = config
        self.db: Database | None = None
        self.orchestrator: Orchestrator | None = None
        self._bg_task: asyncio.Task | None = None
        self._view = "dashboard"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("ph.daemon", id="sidebar-title")
                yield ListView(
                    ListItem(Label(" Dashboard"), id="nav-dashboard"),
                    ListItem(Label(" Tasks"), id="nav-tasks"),
                    ListItem(Label(" Agents"), id="nav-agents"),
                    ListItem(Label(" Constraints"), id="nav-constraints"),
                    id="nav",
                )
            with Vertical(id="content"):
                yield Static(id="status-bar")
                yield DataTable(id="main-table", cursor_type="row")
                yield Static(id="detail", markup=True)
                yield Input(
                    placeholder="Message the orchestrator...",
                    id="msg-input",
                )
        yield Footer()

    async def on_mount(self) -> None:
        self.db = Database(self.config.db_path)
        await self.db.init()

        # Recover from previous crash/exit
        stale = await self.db.mark_stale_running()
        recovered = await self.db.recover_interrupted_tasks()
        if stale or recovered:
            self.notify(
                f"Recovered: {stale} sessions, {recovered} tasks reset to open"
            )

        # Check for auto-stash from previous exit
        self._check_git_stash()

        self.orchestrator = Orchestrator(config=self.config, db=self.db)
        self._bg_task = asyncio.create_task(self.orchestrator.run())

        self.set_interval(5.0, self._refresh)
        await self._show_dashboard()

    async def on_unmount(self) -> None:
        # Gracefully stop orchestrator (kills agent, resets task)
        if self.orchestrator:
            await self.orchestrator.stop()
        if self._bg_task:
            self._bg_task.cancel()

        # Stash dirty working tree so the project is clean
        self._git_stash_if_dirty()

        if self.db:
            await self.db.mark_stale_running()
            await self.db.close()

    def _git_stash_if_dirty(self) -> None:
        """Stash uncommitted changes so the repo is left clean."""
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.config.project_dir,
                capture_output=True, text=True,
            )
            if status.stdout.strip():
                subprocess.run(
                    ["git", "stash", "push", "--include-untracked",
                     "-m", "phd: auto-stash on exit"],
                    cwd=self.config.project_dir,
                    capture_output=True,
                )
        except FileNotFoundError:
            pass

    def _check_git_stash(self) -> None:
        """Notify if there's an auto-stash from a previous session."""
        try:
            result = subprocess.run(
                ["git", "stash", "list"],
                cwd=self.config.project_dir,
                capture_output=True, text=True,
            )
            for line in result.stdout.splitlines():
                if "phd: auto-stash on exit" in line:
                    self.notify(
                        f"Found stashed changes from previous session. "
                        f"Run `git stash pop` to restore.",
                        timeout=10,
                    )
                    break
        except FileNotFoundError:
            pass

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        msg = event.value.strip()
        if msg and self.db:
            await self.db.send_message(msg)
            event.input.value = ""
            self.notify(f"Sent: {msg[:50]}")

    def action_toggle_pause(self) -> None:
        if self.orchestrator:
            if self.orchestrator.is_paused:
                self.orchestrator.resume()
                self.notify("Orchestrator resumed")
            else:
                self.orchestrator.pause()
                self.notify("Orchestrator paused")

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        handlers = {
            "nav-dashboard": self._show_dashboard,
            "nav-tasks": self._show_tasks,
            "nav-agents": self._show_agents,
            "nav-constraints": self._show_constraints,
        }
        handler = handlers.get(event.item.id)
        if handler:
            await handler()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._view == "agents" and event.row_key:
            session = await self.db.get_session(str(event.row_key.value))
            if session:
                self.push_screen(SessionScreen(session, self.db))

    async def _refresh(self) -> None:
        refreshable = {"dashboard", "tasks", "agents"}
        if self._view in refreshable:
            handlers = {
                "dashboard": self._show_dashboard,
                "tasks": self._show_tasks,
                "agents": self._show_agents,
            }
            await handlers[self._view]()

    async def _show_dashboard(self) -> None:
        self._view = "dashboard"
        running = await self.db.list_sessions(status="running")
        open_tasks = await self.db.list_tasks(status="open")
        paused = self.orchestrator.is_paused if self.orchestrator else False

        self.query_one("#status-bar", Static).update(
            f"[bold]Dashboard[/]  "
            f"Running: [green]{len(running)}[/]  "
            f"Open Tasks: [yellow]{len(open_tasks)}[/]  "
            f"Loop: [{'red' if paused else 'green'}]"
            f"{'Paused' if paused else 'Running'}[/]  "
            f"[dim](p to toggle)[/]"
        )

        table = self.query_one("#main-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Type", "Task", "Session", "Status", "Started")
        for s in running:
            table.add_row(
                s["agent_type"],
                f"#{s['task_id']}" if s["task_id"] else "—",
                s["id"],
                s["status"],
                _humantime(s["started_at"]),
            )

        try:
            git_log = subprocess.check_output(
                ["git", "log", "--oneline", "-10"],
                cwd=self.config.project_dir,
            ).decode().strip()
            self.query_one("#detail", Static).update(
                f"[dim]Recent Commits:[/]\n{git_log}"
            )
        except subprocess.CalledProcessError:
            self.query_one("#detail", Static).update("")

    async def _show_tasks(self) -> None:
        self._view = "tasks"
        self.query_one("#status-bar", Static).update("[bold]Tasks[/]")

        table = self.query_one("#main-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Title", "Status", "Priority", "Created")
        for t in await self.db.list_tasks():
            table.add_row(
                str(t["id"]),
                t["title"][:60],
                t["status"],
                "human" if t["priority"] == 0 else "auto",
                _humantime(t["created_at"]),
            )
        self.query_one("#detail", Static).update("")

    async def _show_agents(self) -> None:
        self._view = "agents"
        self.query_one("#status-bar", Static).update(
            "[bold]Agent Sessions[/]  [dim](enter to view logs)[/]"
        )

        table = self.query_one("#main-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Session", "Type", "Task", "Status", "Started")
        for s in await self.db.list_sessions():
            table.add_row(
                s["id"],
                s["agent_type"],
                f"#{s['task_id']}" if s["task_id"] else "—",
                s["status"],
                _humantime(s["started_at"]),
                key=s["id"],
            )
        self.query_one("#detail", Static).update("")

    async def _show_constraints(self) -> None:
        self._view = "constraints"
        self.query_one("#status-bar", Static).update("[bold]Constraints[/]")

        table = self.query_one("#main-table", DataTable)
        table.clear(columns=True)

        content = ""
        if self.config.constraints_path.exists():
            content = self.config.constraints_path.read_text()
        self.query_one("#detail", Static).update(
            content or "[dim]No constraints defined.[/]"
        )
