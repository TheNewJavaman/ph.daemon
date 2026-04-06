# ph.daemon

A terminal UI that orchestrates Claude CLI agents to implement tasks, run experiments, and write papers autonomously.

## What it does

`phd` launches a TUI that manages a loop: a researcher agent analyzes your project state and creates tasks, then engineer agents pick them up and implement them. You stay in control — create tasks, define constraints, ask questions, pause/resume — all from the terminal.

```
You (human)
  │
  ├── Create tasks         (CLI or TUI)
  ├── Define constraints   (interactive refinement chat)
  ├── Ask questions        (ephemeral Q&A agent)
  │
  ▼
Orchestrator (30s loop)
  │
  ├── Has tasks? → spawn engineer agent → implement → commit
  ├── No tasks?  → spawn researcher agent → analyze state → create tasks
  │
  ▼
SQLite DB (tasks, sessions, logs)
```

## Install

Requires Python 3.12+, [Claude CLI](https://docs.anthropic.com/en/docs/claude-code), and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install -e .
```

## Quick start

```bash
cd my-research-project
phd init
phd create-task "Implement baseline evaluation" -d "Run the standard benchmark suite"
phd
```

## TUI

The interface has four views, navigated via the left sidebar:

**Dashboard** — running agents, orchestrator status, and an LLM-generated activity summary that updates after each cycle.

**Tasks** — full task queue sorted by priority then ID. Select a task to see its description, dependencies, and metadata in the detail pane.

**Agents** — all sessions (running, completed, failed). Select a session to stream its logs live or review completed output. Logs are parsed from Claude's stream-JSON format and displayed with formatted tool calls, assistant messages, and results.

**Constraints** — numbered project constraints on the left, interactive refinement chat on the right. Type a constraint idea, Claude refines it, and press `Ctrl+S` to save when satisfied.

### Keybindings

| Key | Action |
|-----|--------|
| `p` | Pause / resume orchestrator |
| `q` | Quit gracefully |
| `t` | Toggle dark / light theme |
| `Enter` | View selected row's details |
| `Esc` | Back from detail view |
| `Ctrl+S` | Save constraint (in constraints view) |

The input bar at the bottom accepts messages to the orchestrator.

## CLI commands

| Command | Description |
|---------|-------------|
| `phd` | Launch TUI + orchestrator |
| `phd init [path]` | Initialize project (creates `.phd/`, `docs/`, etc.) |
| `phd create-task "title" [-d desc] [-p priority] [--depends-on N]` | Create a task |
| `phd task "description"` | Interactive planner — decomposes into subtasks |
| `phd constrain "description"` | Interactive constraint refinement |
| `phd ask "question"` | Q&A about the project |
| `phd paper` | Trigger paper update from recent commits |
| `phd status` | Show running agents and task counts |
| `phd reset-task N` | Reset a failed/interrupted task to open |

## How it works

### Orchestrator loop

1. **Pick a task** — next unblocked task, sorted by priority. Interrupted tasks (with a resumable session) are preferred.
2. **Spawn engineer** — Claude agent gets the task description, project constraints, and research state. Runs with full codebase access.
3. **On completion** — mark done, update activity summary, loop back.
4. **No tasks available** — spawn researcher agent to analyze the codebase, paper, and completed work, then generate 2–3 new tasks.

### Agents

| Agent | Role |
|-------|------|
| **Engineer** | Implements one task per session. Commits with task reference. Supports benchmark workflows (baseline → implement → measure → keep/revert). |
| **Researcher** | Analyzes project state and generates high-value tasks. Updates `docs/research-state.md`. |
| **Planner** | Interactive — decomposes a feature request into subtasks with dependencies. |
| **Ephemeral** | Interactive — answers questions about the codebase, manages constraints. Read-only to code. |
| **Paper** | Updates LaTeX paper in `paper/` based on recent commits. |

### Task lifecycle

Tasks have five states: `open` → `in_progress` → `completed` / `failed` / `interrupted`.

- **Priority 0** (human-created) is picked before **priority 1** (auto-generated).
- **Dependencies**: `phd create-task "Run eval" --depends-on 1 --depends-on 2` — blocked until all dependencies complete.
- **Retries**: failed tasks retry up to 2 times with failure context appended.
- **Session resume**: interrupted tasks resume the same Claude session instead of starting over.

### Graceful shutdown

When you quit (`q` or `Ctrl+C`):
- Running agents receive SIGTERM, then SIGKILL after 5 seconds
- In-progress tasks are marked `interrupted` with session ID preserved for resume
- Dirty git working tree is auto-stashed

## Project structure

After `phd init`, your project gets:

```
my-project/
├── .phd/
│   ├── config.json          # project config
│   ├── daemon.db            # SQLite (WAL mode): tasks, sessions, conversations
│   └── logs/                # agent session logs (JSONL)
├── docs/
│   ├── constraints.md       # numbered project constraints
│   └── research-state.md    # maintained by researcher agent
├── paper/                   # LaTeX paper (optional)
└── CLAUDE.md                # instructions for Claude agents
```

## Agent prompts

Agent behavior is defined in `prompts/`:

- **engineer.md** — task implementation, commit workflow, benchmark evaluation
- **researcher.md** — state analysis, task generation, research-state updates
- **planner.md** — feature decomposition into dependent subtasks
- **ephemeral.md** — Q&A and constraint management
- **paper.md** — LaTeX paper updates from commit history
