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
    DataTable, Footer, Header, Label, ListItem, ListView, RichLog, Static,
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
        return Text(f"▶ {text}", style="italic #71717a") if text else None

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
                result.append(f"▸ {name}", style="bold #a855f7")
                result.append(f" {summary}\n", style="#71717a")
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
        return Text(f"◂ {content}", style="#52525b")

    if t == "result":
        cost = data.get("cost_usd", 0)
        turns = data.get("num_turns", "?")
        is_error = data.get("is_error", False)
        lbl = "Error" if is_error else "Completed"
        style = "bold #ef4444" if is_error else "bold #22c55e"
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


_STATUS_GLYPHS = {
    "running":     "● running",
    "in_progress": "● in progress",
    "open":        "○ open",
    "completed":   "✓ completed",
    "failed":      "✗ failed",
    "interrupted": "◑ interrupted",
    "killed":      "✗ killed",
}

_STATUS_STYLES = {
    "running":     "#22c55e",
    "in_progress": "#22c55e",
    "open":        "#a1a1aa",
    "completed":   "#a855f7",
    "failed":      "#ef4444",
    "interrupted": "#f59e0b",
    "killed":      "#ef4444",
}


def _status(status: str) -> Text:
    label = _STATUS_GLYPHS.get(status, f"? {status}")
    style = _STATUS_STYLES.get(status, "")
    return Text(label, style=style)


def _status_markup(status: str) -> str:
    label = _STATUS_GLYPHS.get(status, f"? {status}")
    color = _STATUS_STYLES.get(status, "")
    return f"[{color}]{label}[/]" if color else label


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
            f"{_status_markup(s['status'])}  "
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
            log_widget.write(Text("Log file not found.", style="#ef4444"))
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


PHD_DARK = Theme(
    name="phd-dark",
    primary="#a855f7",
    secondary="#7c3aed",
    warning="#f59e0b",
    error="#ef4444",
    success="#22c55e",
    accent="#a855f7",
    foreground="#d4d4d8",
    background="#09090b",
    surface="#09090b",
    panel="#111113",
    boost="#1a1a1f",
    dark=True,
    variables={
        "border": "#27272a",
        "border-blurred": "#1c1c1f",
        "scrollbar": "#27272a",
        "scrollbar-hover": "#a855f7",
        "input-selection-background": "#7c3aed40",
        "input-cursor-text-style": "reverse",
    },
)

PHD_LIGHT = Theme(
    name="phd-light",
    primary="#7c3aed",
    secondary="#a855f7",
    warning="#d97706",
    error="#dc2626",
    success="#16a34a",
    accent="#7c3aed",
    foreground="#18181b",
    background="#fafafa",
    surface="#fafafa",
    panel="#f4f4f5",
    boost="#e4e4e7",
    dark=False,
    variables={
        "border": "#d4d4d8",
        "border-blurred": "#e4e4e7",
        "scrollbar": "#d4d4d8",
        "scrollbar-hover": "#7c3aed",
        "input-selection-background": "#7c3aed30",
        "input-cursor-text-style": "reverse",
    },
)


class DaemonApp(App):
    TITLE = "ph.daemon"

    CSS = """
    * {
        scrollbar-size: 1 1;
        scrollbar-background: $surface;
        scrollbar-color: $panel;
        scrollbar-color-hover: $accent;
        scrollbar-color-active: $accent;
    }

    Screen { background: $surface; color: $foreground; }

    /* --- Sidebar --- */
    #sidebar {
        width: 24; dock: left;
        background: $panel;
        padding: 1 0;
    }
    #sidebar-title {
        color: $accent; text-style: bold;
        padding: 0 2; margin-bottom: 1;
    }
    #nav { background: transparent; }
    #nav > ListItem {
        padding: 0 2; color: $text-muted;
        background: transparent;
    }
    #nav > ListItem.-highlight {
        background: $boost; color: $text;
    }
    ListView { background: transparent; }
    ListView:focus { background: transparent; }

    /* --- Content --- */
    #content { padding: 1 2; }
    #status-bar { height: auto; margin-bottom: 1; }

    /* --- DataTable --- */
    DataTable {
        height: 1fr;
        max-height: 50%;
        background: $surface;
    }
    DataTable > .datatable--header {
        color: $text-muted; text-style: bold;
        background: $surface;
    }
    DataTable > .datatable--cursor {
        background: $accent; color: $surface;
    }
    DataTable:focus > .datatable--cursor {
        background: $accent; color: $surface;
    }

    /* --- Detail pane --- */
    #detail-title { height: auto; margin-top: 1; color: $text-muted; }
    #detail-pane {
        height: 1fr; min-height: 5;
        background: $panel; border: solid $border;
    }

    /* --- Header / Footer --- */
    Header { background: $panel; color: $accent; }
    HeaderTitle { color: $accent; text-style: bold; background: $panel; }
    Footer { background: $panel; color: $text-muted; }
    FooterKey { background: $panel; color: $text-muted; }
    FooterKey:hover { background: $boost; }
    FooterKey.-compact .footer-key--key { color: $accent; background: $panel; }
    FooterKey.-compact .footer-key--description { color: $text-muted; background: $panel; }

    /* --- Log viewer --- */
    RichLog { background: $panel; border: solid $border; }
    .session-meta { color: $text-muted; margin-bottom: 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("t", "toggle_theme", "Theme"),
    ]

    def __init__(self, config: ProjectConfig):
        super().__init__()
        self.register_theme(PHD_DARK)
        self.register_theme(PHD_LIGHT)
        self.theme = "phd-dark"
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
                yield Static(id="detail-title", markup=True)
                yield RichLog(id="detail-pane", wrap=True)
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

        if self.db:
            await self.db.mark_stale_running()
            await self.db.close()

    def action_toggle_pause(self) -> None:
        if self.orchestrator:
            if self.orchestrator.is_paused:
                self.orchestrator.resume()
                self.notify("Orchestrator resumed")
            else:
                self.orchestrator.pause()
                self.notify("Orchestrator paused")

    def action_toggle_theme(self) -> None:
        self.theme = "phd-light" if self.theme == "phd-dark" else "phd-dark"

    def _cancel_detail_worker(self) -> None:
        if hasattr(self, "_detail_worker") and self._detail_worker is not None:
            self._detail_worker.cancel()
            self._detail_worker = None

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._cancel_detail_worker()
        handlers = {
            "nav-dashboard": self._show_dashboard,
            "nav-tasks": self._show_tasks,
            "nav-agents": self._show_agents,
            "nav-constraints": self._show_constraints,
        }
        handler = handlers.get(event.item.id)
        if handler:
            await handler()

    async def on_data_table_cursor_moved(self, event: DataTable.CursorMoved) -> None:
        """Populate the detail pane when cursor moves over a row."""
        row_key = event.row_key
        if not row_key:
            return

        key = str(row_key.value)
        detail_title = self.query_one("#detail-title", Static)
        detail_pane = self.query_one("#detail-pane", RichLog)

        if self._view in ("dashboard", "agents"):
            session = await self.db.get_session(key)
            if session:
                await self._show_agent_detail(session)
                return

        if self._view == "tasks":
            try:
                task = await self.db.get_task(int(key))
            except (ValueError, TypeError):
                return
            if task:
                await self._show_task_detail(task)
                return

        detail_title.update("")
        detail_pane.clear()

    async def _show_task_detail(self, task: dict) -> None:
        """Populate the detail pane with task info."""
        detail_title = self.query_one("#detail-title", Static)
        detail_pane = self.query_one("#detail-pane", RichLog)

        deps = ", ".join(f"#{d}" for d in task["dependencies"]) or "none"
        detail_title.update(
            f"[bold]Task #{task['id']}[/]  "
            f"{_status_markup(task['status'])}  "
            f"Priority: {'human' if task['priority'] == 0 else 'auto'}  "
            f"Deps: {deps}  "
            f"Created: {_humantime(task['created_at'])}"
        )
        detail_pane.clear()
        desc = task.get("description", "") or "No description."
        detail_pane.write(Text(desc))

    async def _show_agent_detail(self, session: dict) -> None:
        """Populate the detail pane with agent session info and log."""
        detail_title = self.query_one("#detail-title", Static)
        task_ref = f"Task #{session['task_id']}" if session["task_id"] else "—"
        detail_title.update(
            f"[bold]{session['agent_type']}[/] {session['id']}  "
            f"{task_ref}  "
            f"{_status_markup(session['status'])}  "
            f"Started: {_humantime(session['started_at'])}"
        )
        self._show_detail_log(session)

    def _show_detail_log(self, session: dict) -> None:
        """Start tailing a session log in the detail pane."""
        self._cancel_detail_worker()
        pane = self.query_one("#detail-pane", RichLog)
        pane.clear()
        self._detail_worker = self.run_worker(
            self._tail_detail(session), exclusive=False
        )

    async def _tail_detail(self, session: dict) -> None:
        """Tail a log file into the detail pane."""
        pane = self.query_one("#detail-pane", RichLog)
        path = Path(session["log_path"])
        if not path.exists():
            pane.write(Text("Log file not found.", style="#ef4444"))
            return
        with open(path) as f:
            while True:
                line = f.readline()
                if line:
                    formatted = _format_log_line(line)
                    if formatted:
                        pane.write(formatted)
                else:
                    s = await self.db.get_session(session["id"])
                    if s and s["status"] != "running":
                        break
                    await asyncio.sleep(0.5)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter pushes full-screen log viewer for agents."""
        if self._view in ("dashboard", "agents") and event.row_key:
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
                _status(s["status"]),
                _humantime(s["started_at"]),
                key=s["id"],
            )

        # Show first running agent's log, or activity summary
        if running:
            await self._show_agent_detail(running[0])
        else:
            detail_pane = self.query_one("#detail-pane", RichLog)
            detail_pane.clear()
            self.query_one("#detail-title", Static).update("[dim]Recent Activity[/]")
            activity_path = self.config.daemon_dir / "activity.md"
            if activity_path.exists():
                detail_pane.write(Text(activity_path.read_text()))
            else:
                detail_pane.write(Text("No activity yet. The orchestrator will generate a summary after its first cycle.", style="#71717a"))

    async def _show_tasks(self) -> None:
        self._view = "tasks"
        self.query_one("#status-bar", Static).update("[bold]Tasks[/]")

        table = self.query_one("#main-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Title", "Status", "Priority", "Created")
        tasks = await self.db.list_tasks()
        for t in tasks:
            table.add_row(
                str(t["id"]),
                t["title"][:60],
                _status(t["status"]),
                "human" if t["priority"] == 0 else "auto",
                _humantime(t["created_at"]),
                key=str(t["id"]),
            )

        # Show first task in detail pane
        if tasks:
            await self._show_task_detail(tasks[0])
        else:
            self.query_one("#detail-title", Static).update("")
            self.query_one("#detail-pane", RichLog).clear()

    async def _show_agents(self) -> None:
        self._view = "agents"
        self.query_one("#status-bar", Static).update(
            "[bold]Agent Sessions[/]  [dim](enter for fullscreen)[/]"
        )

        table = self.query_one("#main-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Session", "Type", "Task", "Status", "Started")
        sessions = await self.db.list_sessions()
        for s in sessions:
            table.add_row(
                s["id"],
                s["agent_type"],
                f"#{s['task_id']}" if s["task_id"] else "—",
                _status(s["status"]),
                _humantime(s["started_at"]),
                key=s["id"],
            )

        # Show first session's log in detail pane
        if sessions:
            await self._show_agent_detail(sessions[0])
        else:
            self.query_one("#detail-title", Static).update("")
            self.query_one("#detail-pane", RichLog).clear()

    async def _show_constraints(self) -> None:
        self._view = "constraints"
        self.query_one("#status-bar", Static).update("[bold]Constraints[/]")

        table = self.query_one("#main-table", DataTable)
        table.clear(columns=True)

        self.query_one("#detail-title", Static).update("[dim]Constraints[/]")
        pane = self.query_one("#detail-pane", RichLog)
        pane.clear()
        if self.config.constraints_path.exists():
            content = self.config.constraints_path.read_text().strip()
            pane.write(Text(content or "No constraints defined."))
        else:
            pane.write(Text("No constraints defined."))
