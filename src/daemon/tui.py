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
    DataTable, Footer, Input, Label, ListItem, ListView, RichLog, Static,
)

from textual.coordinate import Coordinate
from textual.message import Message

from daemon.config import ProjectConfig


class TrackedTable(DataTable):
    """DataTable that posts a message when the cursor row changes."""

    class CursorMoved(Message):
        def __init__(self, row_key) -> None:
            super().__init__()
            self.row_key = row_key

    def watch_cursor_coordinate(
        self, old: Coordinate, new: Coordinate,
    ) -> None:
        super().watch_cursor_coordinate(old, new)
        if old.row != new.row and self.row_count > 0:
            try:
                row_key, _ = self.coordinate_to_cell_key(new)
                self.post_message(self.CursorMoved(row_key))
            except Exception:
                pass
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
        local_dt = dt.astimezone()
        return local_dt.strftime("%b %-d, %-I:%M %p")
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
        width: 26; dock: left;
        background: $panel;
        border: round $border;
        border-title-color: $accent;
        border-title-style: bold;
        padding: 0 1;
        margin: 0;
    }
    #nav { background: transparent; }
    #nav > ListItem {
        padding: 0 1; color: $text-muted;
        background: transparent;
    }
    #nav > ListItem.-highlight {
        background: $boost; color: $text;
    }
    ListView { background: transparent; background-tint: transparent; }
    ListView:focus { background: transparent; background-tint: transparent; }

    /* --- Content --- */
    #content { padding: 0; margin: 0; }

    /* --- DataTable --- */
    #table-box {
        height: 1fr; max-height: 50%;
        border: round $border;
        border-title-color: $text-muted;
        border-title-style: bold;
        background: $panel;
        padding: 0; margin: 0;
    }
    #constraints-list {
        display: none;
        height: 1fr;
        background: $panel;
        padding: 0 1;
    }
    #table-box:focus-within {
        border-title-color: $foreground;
    }
    DataTable {
        height: 1fr;
        background: $panel;
        background-tint: transparent;
    }
    DataTable:focus {
        background-tint: transparent;
    }
    DataTable > .datatable--header {
        color: $text-muted; text-style: bold;
        background: $panel;
    }
    DataTable > .datatable--cursor {
        background: $accent; color: $surface;
    }
    DataTable:focus > .datatable--cursor {
        background: $accent; color: $surface;
    }

    /* --- Detail pane --- */
    #detail-box {
        height: 1fr; min-height: 5;
        border: round $border;
        border-title-color: $text-muted;
        background: $panel;
        padding: 0; margin: 0;
    }
    #detail-box:focus-within {
        border-title-color: $foreground;
    }
    #detail-pane {
        height: 1fr;
        background: $panel;
        background-tint: transparent;
        padding: 0 1;
    }
    #detail-pane:focus {
        background-tint: transparent;
    }

    /* --- Chat input --- */
    #chat-input {
        display: none;
        dock: bottom;
        height: 1;
        margin: 0 1;
        background: $panel;
        border: round $border;
        padding: 0 1;
        overflow-x: hidden;
    }
    #chat-input:focus {
        border: round $accent;
        background-tint: transparent;
    }

    /* --- Footer --- */
    Footer { background: $panel; color: $text-muted; }
    FooterKey { background: $panel; color: $text-muted; }
    FooterKey:hover { background: $boost; }
    FooterKey.-compact .footer-key--key { color: $accent; background: $panel; }
    FooterKey.-compact .footer-key--description { color: $text-muted; background: $panel; }
    FooterKey.-command-palette { border-left: none; }

    /* --- Log viewer --- */
    RichLog { background: $panel; }
    #detail-pane { border: none; }
    #log { border: round $border; }
    .session-meta { color: $text-muted; margin-bottom: 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("t", "toggle_theme", "Theme"),
        Binding("ctrl+s", "save_constraint", "Save constraint"),
        Binding("n", "new_constraint", "New", show=False),
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
        self._data_cache: dict[tuple[str, str], dict] = {}
        self._constraint_session: str | None = None  # Claude session for constraint chat
        self._constraint_history: list[tuple[str, str]] = []  # (role, text) pairs

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="sidebar") as sidebar:
                sidebar.border_title = "ph.daemon"
                yield ListView(
                    ListItem(Label(" Dashboard"), id="nav-dashboard"),
                    ListItem(Label(" Agents"), id="nav-agents"),
                    ListItem(Label(" Tasks"), id="nav-tasks"),
                    ListItem(Label(" Constraints"), id="nav-constraints"),
                    id="nav",
                )
            with Vertical(id="content"):
                with Vertical(id="table-box") as table_box:
                    table_box.border_title = "Dashboard"
                    table = TrackedTable(id="main-table", cursor_type="row")
                    table._show_hover_cursor = False
                    table._set_hover_cursor = lambda active: None
                    yield table
                    yield RichLog(id="constraints-list", wrap=True)
                with Vertical(id="detail-box") as detail_box:
                    detail_box.border_title = "Details"
                    yield RichLog(id="detail-pane", wrap=True)
                    yield Input(
                        id="chat-input",
                        placeholder="Describe a constraint…",
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
                f"Recovered: {stale} sessions, {recovered} tasks marked for resume"
            )

        self.orchestrator = Orchestrator(config=self.config, db=self.db)
        self._bg_task = asyncio.create_task(self.orchestrator.run())

        self.set_interval(5.0, self._refresh)
        await self._show_dashboard()

    async def _shutdown(self) -> None:
        """Gracefully stop orchestrator and clean up DB."""
        if self.orchestrator:
            await self.orchestrator.stop()
            self.orchestrator = None
        if self._bg_task:
            self._bg_task.cancel()
            self._bg_task = None
        if self.db:
            await self.db.mark_stale_running()
            await self.db.close()
            self.db = None

    async def action_quit(self) -> None:
        await self._shutdown()
        self.exit()

    async def on_unmount(self) -> None:
        await self._shutdown()

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

    async def action_save_constraint(self) -> None:
        """Extract the last CONSTRAINT/RATIONALE from chat and append to constraints.md."""
        if self._view != "constraints" or not self._constraint_history:
            return

        # Find the last assistant message that contains CONSTRAINT:
        constraint_text = None
        rationale_text = None
        for role, msg in reversed(self._constraint_history):
            if role != "assistant":
                continue
            for line in msg.splitlines():
                stripped = line.strip()
                if stripped.upper().startswith("CONSTRAINT:"):
                    constraint_text = stripped.split(":", 1)[1].strip()
                if stripped.upper().startswith("RATIONALE:"):
                    rationale_text = stripped.split(":", 1)[1].strip()
            if constraint_text:
                break

        if not constraint_text:
            self.notify(
                "No finalized constraint found — keep refining",
                severity="warning",
            )
            return

        # Determine next constraint number
        existing = self._parse_constraints()
        next_num = max((int(c["id"]) for c in existing), default=0) + 1

        # Append to constraints.md
        entry = f"\n{next_num}. {constraint_text}\n"
        if rationale_text:
            entry += f"   Rationale: {rationale_text}\n"

        path = self.config.constraints_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(entry)

        # Reset chat state
        self._constraint_history.clear()
        self._constraint_session = None
        self.notify(f"Saved constraint #{next_num}: {constraint_text[:50]}")
        await self._show_constraints()

    async def action_new_constraint(self) -> None:
        """Reset the constraint chat to start a new conversation."""
        if self._view != "constraints":
            return
        self._constraint_history.clear()
        self._constraint_session = None
        await self._show_constraints()

    def _cancel_detail_worker(self) -> None:
        if hasattr(self, "_detail_worker") and self._detail_worker is not None:
            self._detail_worker.cancel()
            self._detail_worker = None

    def _show_table_mode(self, constraints: bool = False) -> None:
        """Toggle between the DataTable and the constraints RichLog."""
        self.query_one("#main-table", DataTable).display = not constraints
        self.query_one("#constraints-list", RichLog).display = constraints

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._cancel_detail_worker()
        handlers = {
            "nav-dashboard": self._show_dashboard,
            "nav-tasks": self._show_tasks,
            "nav-agents": self._show_agents,
            "nav-constraints": self._show_constraints,
        }
        # Hide chat input when leaving constraints
        chat_input = self.query_one("#chat-input", Input)
        if event.item.id != "nav-constraints":
            chat_input.display = False

        handler = handlers.get(event.item.id)
        if handler:
            await handler()
            if event.item.id != "nav-constraints":
                table = self.query_one("#main-table", DataTable)
                if table.row_count > 0:
                    table.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle chat input submission for constraint refinement."""
        if self._view != "constraints" or event.input.id != "chat-input":
            return
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        event.input.disabled = True

        pane = self.query_one("#detail-pane", RichLog)
        self._constraint_history.append(("user", text))
        pane.write(Text(f"▶ {text}", style="bold"))
        pane.write(Text(""))

        self.run_worker(self._constraint_chat(text), exclusive=False)

    async def _constraint_chat(self, user_msg: str) -> None:
        """Send the constraint conversation to Claude and display the response."""
        pane = self.query_one("#detail-pane", RichLog)
        chat_input = self.query_one("#chat-input", Input)

        existing = ""
        if self.config.constraints_path.exists():
            existing = self.config.constraints_path.read_text().strip()

        # Build full conversation as a single prompt
        system = (
            "You are helping a researcher define a project constraint. "
            "Constraints are rules that ALL agents must follow. "
            "They live in docs/constraints.md as numbered entries.\n\n"
            "Help refine the idea into something precise and actionable. "
            "Ask clarifying questions if the idea is vague. "
            "When the constraint is clear, present the final version as:\n\n"
            "CONSTRAINT: <one-line title>\n"
            "RATIONALE: <why this matters>\n\n"
            "Keep responses concise (2-4 sentences). "
            "The human will press ctrl+s to save once satisfied."
        )
        if existing:
            system += f"\n\nExisting constraints:\n{existing}"

        # Replay conversation as a single prompt so each call is stateless
        parts = []
        for role, msg in self._constraint_history:
            if role == "user":
                parts.append(f"Human: {msg}")
            else:
                parts.append(f"Assistant: {msg}")
        prompt = "\n\n".join(parts) + "\n\nAssistant:"

        cmd = [
            "claude", "--print",
            "--model", "claude-sonnet-4-6",
            "--max-turns", "1",
            "--append-system-prompt", system,
            prompt,
        ]

        pane.write(Text("…", style="#71717a italic"))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.config.project_dir,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            response = stdout.decode().strip() if stdout else "(no response)"
        except TimeoutError:
            response = "(timed out)"
        except Exception as e:
            response = f"(error: {e})"

        self._constraint_history.append(("assistant", response))

        # Redraw full chat
        pane.clear()
        for role, msg in self._constraint_history:
            if role == "user":
                pane.write(Text(f"▶ {msg}", style="bold"))
            else:
                pane.write(Text(msg))
            pane.write(Text(""))

        chat_input.disabled = False
        chat_input.focus()

    async def on_tracked_table_cursor_moved(self, event: TrackedTable.CursorMoved) -> None:
        """Update detail pane when cursor moves via arrow keys."""
        if event.row_key:
            await self._update_detail_for_key(str(event.row_key.value))

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Update detail pane on click/enter."""
        if event.row_key:
            await self._update_detail_for_key(str(event.row_key.value))

    async def _show_task_detail(self, task: dict) -> None:
        """Populate the detail pane with task info."""
        deps = ", ".join(f"#{d}" for d in task["dependencies"]) or "none"
        status_label = _STATUS_GLYPHS.get(task["status"], task["status"])
        priority = "human" if task["priority"] == 0 else "auto"
        self.query_one("#detail-box").border_title = (
            f"Task #{task['id']} · {status_label} · {priority} · deps: {deps}"
        )
        pane = self.query_one("#detail-pane", RichLog)
        pane.clear()
        desc = task.get("description", "") or "No description."
        pane.write(Text(desc))

    async def _show_agent_detail(self, session: dict) -> None:
        """Populate the detail pane with agent session info and log."""
        task_ref = f"Task #{session['task_id']}" if session["task_id"] else ""
        status_label = _STATUS_GLYPHS.get(session["status"], session["status"])
        parts = [session["agent_type"], session["id"][:8], status_label]
        if task_ref:
            parts.append(task_ref)
        self.query_one("#detail-box").border_title = " · ".join(parts)
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

        is_done = session["status"] not in ("running",)

        with open(path) as f:
            if is_done:
                # For completed/failed sessions, only show last 200 lines
                lines = f.readlines()[-200:]
            else:
                lines = f.readlines()

            for line in lines:
                formatted = _format_log_line(line)
                if formatted:
                    pane.write(formatted)

            if is_done:
                return

            # Live tail for running sessions
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


    async def _refresh(self) -> None:
        refreshable = {"dashboard", "tasks", "agents"}
        if self._view in refreshable:
            handlers = {
                "dashboard": self._show_dashboard,
                "tasks": self._show_tasks,
                "agents": self._show_agents,
            }
            await handlers[self._view]()

    def _set_titles(self, table_title: str, detail_title: str = "Details") -> None:
        self.query_one("#table-box").border_title = table_title
        self.query_one("#detail-box").border_title = detail_title

    def _save_cursor(self) -> str | None:
        """Return the key of the currently selected row, if any."""
        table = self.query_one("#main-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return str(row_key.value)
        except Exception:
            return None

    def _update_table(
        self,
        columns: tuple[str, ...],
        rows: list[tuple[str, ...]],
        keys: list[str],
    ) -> bool:
        """Update the table in-place if possible. Returns True if rebuilt from scratch."""
        table = self.query_one("#main-table", DataTable)

        # Check if we can do an in-place update (same columns, same keys in same order)
        if table.row_count > 0 and len(table.columns) == len(columns):
            existing_keys = [str(k.value) for k in table.rows]
            if existing_keys == keys:
                # Same structure — update changed cells only
                col_keys = list(table.columns.keys())
                for row_idx, key in enumerate(keys):
                    for col_idx, col_key in enumerate(col_keys):
                        old_val = table.get_cell(key, col_key)
                        new_val = rows[row_idx][col_idx]
                        if old_val != new_val:
                            table.update_cell(key, col_key, new_val)
                return False

        # Different structure — full rebuild
        saved = self._save_cursor()
        table.clear(columns=True)
        table.add_columns(*columns)
        for key, row in zip(keys, rows):
            table.add_row(*row, key=key)
        if saved:
            for idx, k in enumerate(table.rows):
                if str(k.value) == saved:
                    table.move_cursor(row=idx)
                    break
        return True

    async def _update_detail_for_key(self, key: str) -> None:
        """Update the detail pane for the given row key."""
        if self._view in ("dashboard", "agents"):
            # Use cache first for instant response, fall back to DB
            session = self._data_cache.get(("session", key))
            if not session:
                session = await self.db.get_session(key)
            if session:
                await self._show_agent_detail(session)
        elif self._view == "tasks":
            task = self._data_cache.get(("task", key))
            if not task:
                try:
                    task = await self.db.get_task(int(key))
                except (ValueError, TypeError):
                    return
            if task:
                await self._show_task_detail(task)

    async def _show_dashboard(self) -> None:
        self._view = "dashboard"
        self._show_table_mode(constraints=False)
        running = await self.db.list_sessions(status="running")
        open_tasks = await self.db.list_tasks(status="open")
        paused = self.orchestrator.is_paused if self.orchestrator else False

        status = (
            f"Running: {len(running)}  "
            f"Open: {len(open_tasks)}  "
            f"Loop: {'Paused' if paused else 'Active'}"
        )
        self._set_titles(f"Dashboard — {status}", "Activity")

        self._data_cache = {("session", s["id"]): s for s in running}
        rows = [
            (
                s["agent_type"],
                f"#{s['task_id']}" if s["task_id"] else "—",
                s["id"],
                _status(s["status"]),
                _humantime(s["started_at"]),
            )
            for s in running
        ]
        keys = [s["id"] for s in running]
        rebuilt = self._update_table(
            ("Type", "Task", "Session", "Status", "Started"), rows, keys,
        )

        if not running:
            pane = self.query_one("#detail-pane", RichLog)
            pane.clear()
            activity_path = self.config.daemon_dir / "activity.md"
            if activity_path.exists():
                pane.write(Text(activity_path.read_text()))
            else:
                pane.write(Text(
                    "No activity yet. The orchestrator will generate a "
                    "summary after its first cycle.",
                    style="#71717a",
                ))
        elif rebuilt:
            await self._show_agent_detail(running[0])

    async def _show_tasks(self) -> None:
        self._view = "tasks"
        self._show_table_mode(constraints=False)
        self._set_titles("Tasks")

        tasks = await self.db.list_tasks()
        self._data_cache = {("task", str(t["id"])): t for t in tasks}
        rows = [
            (
                str(t["id"]),
                t["title"][:60],
                _status(t["status"]),
                "human" if t["priority"] == 0 else "auto",
                _humantime(t["created_at"]),
            )
            for t in tasks
        ]
        keys = [str(t["id"]) for t in tasks]
        rebuilt = self._update_table(
            ("#", "Title", "Status", "Priority", "Created"), rows, keys,
        )

        if not tasks:
            self.query_one("#detail-box").border_title = "Details"
            self.query_one("#detail-pane", RichLog).clear()
        elif rebuilt:
            await self._show_task_detail(tasks[0])

    async def _show_agents(self) -> None:
        self._view = "agents"
        self._show_table_mode(constraints=False)
        self._set_titles("Agents")

        sessions = await self.db.list_sessions()
        self._data_cache = {("session", s["id"]): s for s in sessions}
        rows = [
            (
                s["id"],
                s["agent_type"],
                f"#{s['task_id']}" if s["task_id"] else "—",
                _status(s["status"]),
                _humantime(s["started_at"]),
            )
            for s in sessions
        ]
        keys = [s["id"] for s in sessions]
        rebuilt = self._update_table(
            ("Session", "Type", "Task", "Status", "Started"), rows, keys,
        )

        if not sessions:
            self.query_one("#detail-box").border_title = "Log"
            self.query_one("#detail-pane", RichLog).clear()
        elif rebuilt:
            await self._show_agent_detail(sessions[0])

    def _parse_constraints(self) -> list[dict]:
        """Parse constraints.md into a list of {id, title, body} dicts."""
        if not self.config.constraints_path.exists():
            return []
        content = self.config.constraints_path.read_text()
        constraints = []
        current: dict | None = None
        for line in content.splitlines():
            stripped = line.strip()
            # Match numbered constraints like "1. ..." or "## 1. ..."
            if stripped and (stripped[0].isdigit() or stripped.startswith("## ")):
                # Try to extract a number prefix
                text = stripped.lstrip("#").strip()
                if text and text[0].isdigit():
                    dot = text.find(".")
                    if dot > 0:
                        num = text[:dot].strip()
                        title = text[dot + 1:].strip()
                        if current:
                            constraints.append(current)
                        current = {"id": num, "title": title, "body": ""}
                        continue
            if current:
                current["body"] += line + "\n"
        if current:
            constraints.append(current)
        return constraints

    async def _show_constraints(self) -> None:
        self._view = "constraints"
        self._show_table_mode(constraints=True)

        constraints = self._parse_constraints()
        self._set_titles(
            f"Constraints ({len(constraints)})",
            "New Constraint — chat to refine, ctrl+s to save",
        )

        clist = self.query_one("#constraints-list", RichLog)
        clist.clear()
        if constraints:
            for c in constraints:
                clist.write(Text(f"{c['id']}. {c['title']}", style="bold"))
                body = c["body"].strip()
                if body:
                    clist.write(Text(f"   {body}", style="#a1a1aa"))
                clist.write(Text(""))
        else:
            clist.write(Text("No constraints defined.", style="#71717a"))

        # Show chat input
        chat_input = self.query_one("#chat-input", Input)
        chat_input.display = True

        # Show existing chat history or welcome message
        pane = self.query_one("#detail-pane", RichLog)
        pane.clear()
        if self._constraint_history:
            for role, text in self._constraint_history:
                if role == "user":
                    pane.write(Text(f"▶ {text}", style="bold"))
                else:
                    pane.write(Text(text))
                pane.write(Text(""))
        else:
            pane.write(Text(
                "Describe a constraint you'd like to add. "
                "I'll help you refine it into something precise and actionable.",
                style="#71717a",
            ))
            pane.write(Text(""))
            pane.write(Text(
                "Examples: \"agents should never modify the database schema\", "
                "\"all experiments must be reproducible with a fixed seed\"",
                style="#71717a italic",
            ))

        chat_input.focus()
