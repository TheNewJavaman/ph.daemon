# ph.daemon — Automated Research Harness

## Overview

ph.daemon is a standalone Python tool that orchestrates automated research by
invoking the `claude` CLI. It manages agent lifecycles, tracks all work in
GitHub issues, maintains a LaTeX research paper in real-time, and provides a
web-based control plane for monitoring and interaction.

**Stack:** Python (uv), FastAPI, Jinja2 + htmx, SQLite, SSE, `claude` CLI.

## Architecture

### Separation of Concerns

- **ph.daemon** (this repo) is the tool — installable via `uv tool install`.
- **Target project** (e.g. `foobar/`) is where research happens.
- **`.ph.daemon/`** inside the target project holds local state (SQLite, logs).
  Added to `.gitignore`.

### Project Layout

**ph.daemon (the tool):**

```
ph.daemon/
├── pyproject.toml
├── src/
│   └── daemon/
│       ├── __init__.py
│       ├── app.py              # FastAPI application
│       ├── db.py               # SQLite schema + queries
│       ├── agents/
│       │   ├── __init__.py
│       │   ├── base.py         # Base agent: subprocess lifecycle, log capture
│       │   ├── planner.py      # Decomposes tasks into issues
│       │   ├── implementor.py  # Main loop: picks issues, implements, evaluates
│       │   ├── paper.py        # Paper writer (invokes paper plugin)
│       │   └── ephemeral.py    # Read-only Q&A agents
│       ├── github/
│       │   ├── __init__.py
│       │   ├── issues.py       # Issue CRUD, dependency resolution, sync
│       │   └── hooks.py        # Post-commit hook logic
│       ├── web/
│       │   ├── __init__.py
│       │   ├── routes.py       # FastAPI routes
│       │   ├── sse.py          # SSE log streaming
│       │   └── templates/      # Jinja2 + htmx templates
│       └── cli.py              # CLI entrypoint
└── prompts/                    # Agent system prompts (markdown)
    ├── implementor.md
    ├── planner.md
    ├── paper.md
    └── ephemeral.md
```

**Target research project (scaffolded by `phd init`):**

```
foobar/
├── .ph.daemon/                 # gitignored — local daemon state
│   ├── daemon.db               # SQLite
│   └── logs/                   # One log file per agent session
├── .claude/
│   └── settings.json           # Post-commit hook configuration
├── CLAUDE.md                   # LLM instructions + @docs/constraints.md
├── docs/
│   └── constraints.md          # Human-pushed constraints
├── paper/                      # LaTeX paper (committed, versioned)
│   ├── main.tex
│   ├── Makefile
│   └── ...
└── ... (research code)
```

## Agents

All agents are built on a common base that manages `claude` CLI subprocesses.

### Base Agent

- Spawns `claude` via `subprocess.Popen`
- Captures stdout/stderr to `.ph.daemon/logs/{session_id}.jsonl`
- Tracks PID, status, start/end time in SQLite
- Every session is tied to a GitHub issue number
- Passes `--system-prompt` assembled from `prompts/{type}.md` + project context

### Planner Agent

- **Triggered by:** human submitting a feature request (additive mode)
- **Read-only to code.** Writes only to GitHub issues.
- **Workflow:**
  1. Takes a high-level feature description (refined via interactive session)
  2. Reads codebase, existing issues, and `docs/constraints.md` for context
  3. Decomposes into ordered GitHub issues with dependency relations
  4. Cross-references related existing issues
  5. Terminates once issues are created

### Implementor Agent

- **Long-running loop** that pulls work from the issue queue.
- **Full read/write access** to the project. Can create child issues for subtasks.
- **Workflow:**
  1. Query for next unassigned, unblocked issue (respects dependency DAG)
  2. Claim it (self-assign)
  3. Implement — commit referencing the issue
  4. Evaluate — run tests, benchmarks, or whatever the task requires
  5. Accept or revert:
     - Works: acceptance commit, close the issue
     - Doesn't work: revert commit, post analysis, optionally create follow-up
  6. Every path produces at least two commits and a rich discussion trail
  7. Loop back to (1)
- When idle (no issues), sleeps 30s and polls.
- Can be paused/resumed from the web UI.

### Paper Writer Agent

- **Triggered by:** new commits on main (non-paper files).
- **Read-only to research code.** Write access only to `paper/`.
- **Workflow:**
  1. Diffs recent commits since last paper update
  2. Determines which sections need updating
  3. Invokes the paper plugin's reviewer/writer agents against `paper/`
  4. Commits paper changes with references to source commits

### Ephemeral Agent

- **Short-lived,** for human Q&A about the codebase.
- **Read-only to code.** Can write to `docs/` and GitHub issues (create, edit, close).
- No persistent session — spins up, answers, terminates.

### Agent Permissions

| Agent | Code read | Code write | Docs write | GitHub issues | Paper write |
|---|---|---|---|---|---|
| Planner | yes | no | no | create, edit, close | no |
| Implementor | yes | yes | no | create, edit, close | no |
| Paper writer | yes | no | no | edit | yes |
| Ephemeral | yes | no | yes | create, edit, close | no |

"Edit" covers updating descriptions, adding comments, changing labels, and
modifying dependency links.

## Communication Modes

| Mode | CLI command | What happens | Agent |
|---|---|---|---|
| **Ask** | `phd ask` | Human asks a question, interactive back-and-forth | Ephemeral |
| **Task** | `phd task` | Human requests feature → interactive refinement → planner creates issues → implementor picks them up | Planner → Implementor |
| **Constraint** | `phd constrain` | Human pushes a constraint → interactive refinement → written to `docs/constraints.md` + issue created | Ephemeral |

## GitHub Integration

### Issue Schema

Every issue created by the daemon uses a consistent structure:

```markdown
## Context
Spawned from #10. Part of: [feature description]

## Task
[What the implementor should do]

## Dependencies
- Blocked by: #11, #12
- Blocks: #14

## Constraints
- Must not use global state (C-001)

## Activity
[Updated by post-commit hook — rich discussion, not just log entries]
```

Labels:
- `ph:planner`, `ph:implementor`, `ph:paper`, `ph:ephemeral` — agent ownership
- `ph:blocked`, `ph:ready`, `ph:in-progress`, `ph:done` — status
- `ph:constraint` — for subtractive mode constraint issues

Dependencies use GitHub task list syntax:

```markdown
- [ ] #12
- [ ] #13
```

The implementor's issue selection resolves the dependency DAG — only issues
whose blockers are all closed are eligible.

### Post-Commit Hook

Configured in `.claude/settings.json`:

```json
{
  "hooks": {
    "PostCommit": [
      {
        "command": "phd hook post-commit",
        "description": "Update linked GitHub issue with commit details"
      }
    ]
  }
}
```

The hook spawns a short non-interactive Claude session (`--print` mode) that
reads the diff and writes a substantive comment on the linked issue.

### Commit-Discuss Protocol

Every idea produces at least two commits (implementation + accept/revert) and
each commit generates a rich discussion comment on the linked issue.

**Implementation commit comment:**

```markdown
## Attempt: [description] (`abc1234`)

**Approach:** [What was done and how]
**Justification:** [Why this approach was chosen]
**Risks:** [Known concerns]
**Status:** Pending evaluation.
```

**Revert commit comment:**

```markdown
## Reverted: [description] (`def5678`, reverts `abc1234`)

**What went wrong:** [What failed and why]
**What we learned:** [Lessons for future attempts]
**Next step:** [Follow-up plan, linked issue if created]
```

**Acceptance commit comment:**

```markdown
## Accepted: [description] (`ghi9012`)

**Evaluation results:** [Evidence it works]
**Why it works:** [Explanation]
**Resolved:** Closing #N.
```

### Memoization via Issues

Before any agent starts work, it queries existing issues (open and closed) for
relevant context:
- Planner: "Has something similar been attempted? What was the outcome?"
- Implementor: "Are there closed issues with relevant decisions or gotchas?"
- Prevents re-deriving solutions or repeating known mistakes.

## Constraints System

### CLAUDE.md (scaffolded by `phd init`)

```markdown
# Research Project

## Daemon

This project is managed by ph.daemon. All work is tracked in GitHub issues.

- Every code change must reference a GitHub issue number in the commit message
- Before starting work, check existing issues (open and closed) for prior decisions
- After each commit, update the linked issue with a summary of changes

## Constraints

@docs/constraints.md

## Paper

The research paper lives in `paper/`. Only the paper writer agent modifies it.
Do not edit `paper/` directly during implementation tasks.
```

### docs/constraints.md

Starts empty, grows via `phd constrain`:

```markdown
# Constraints

Rules that must always be followed. Each constraint was added because the LLM
made a mistake or the human wants to enforce a specific approach.

<!-- Constraints are append-only. To remove one, discuss via `phd ask` first. -->
```

Each constraint entry:

```markdown
## C-001: No global mutable state (2026-04-04)
Issue: #15

Never use module-level mutable variables. All state must be passed explicitly
through function arguments or held in class instances.

Rationale: The optimizer agent introduced a module-level cache dict that caused
stale results across evaluation runs.
```

Constraints are numbered, dated, linked to an issue, and include rationale.

## Data Model

### SQLite (`.ph.daemon/daemon.db`) — local state only

```sql
sessions (
    id          TEXT PRIMARY KEY,    -- uuid
    agent_type  TEXT NOT NULL,       -- planner | implementor | paper | ephemeral
    issue_id    INTEGER,             -- linked GitHub issue (nullable for ephemeral)
    status      TEXT NOT NULL,       -- running | completed | failed | killed
    pid         INTEGER,             -- OS process ID
    log_path    TEXT NOT NULL,       -- .ph.daemon/logs/{id}.jsonl
    started_at  TEXT NOT NULL,
    ended_at    TEXT
)
```

This is the only table. Sessions are purely local state with no GitHub equivalent.

### Everything else comes from authoritative sources

- **Issues:** GitHub API via `gh` CLI, cached in-memory with 30s TTL
- **Commits:** `git log`, authoritative and fast
- **Constraints:** Parsed from `docs/constraints.md` on demand
- **Dependency graph:** Parsed from GitHub issue task lists on each resolution

GitHub API rate limit is 5000 req/hr authenticated. A single-user tool polling
every 5s uses ~720 req/hr — well within budget. No local mirror needed.

## Web UI (Control Plane)

FastAPI + Jinja2 + htmx. Server-rendered. SSE for real-time. Localhost only.

### Pages

**Dashboard (`/`)** — Active agents, recent commits, issue queue (ready/blocked/
in-progress counts), paper status, quick actions (submit task, add constraint,
spawn ephemeral).

**Agent Sessions (`/agents`)** — List all sessions (current + historical).
Filter by type, status, issue. Click into session for live log view.

**Session Detail (`/agents/{session_id}`)** — Real-time log stream via SSE.
Metadata sidebar. Kill button (SIGTERM → 5s grace → SIGKILL).

**Issues (`/issues`)** — Mirrors GitHub issues with `ph:*` labels. Dependency
graph visualization (DAG). Click-through to GitHub. Create new issue directly.

**Paper (`/paper`)** — Rendered PDF viewer. Section-level diff since last view.
Trigger manual paper update.

**Constraints (`/constraints`)** — View `docs/constraints.md`. Add new constraint
inline (writes file, creates issue, commits).

### Real-time Updates

- Agent logs: SSE stream from tailing log files
- Dashboard stats: htmx `hx-trigger="every 5s"` polling
- Issue status: SSE from GitHub polling or webhook listener

## CLI

```bash
# Initialize a new research project
phd init <project-path> --repo <github-repo>
# Scaffolds: CLAUDE.md, docs/constraints.md, paper/, .ph.daemon/
# Sets up post-commit hook in .claude/settings.json
# Adds .ph.daemon/ to .gitignore

# Start the web UI + implementor loop
phd start
# Starts FastAPI on localhost:8666
# Starts implementor loop (polls for ready issues)
# Starts GitHub issue sync

# Submit a task — opens interactive Claude session to refine, then dispatches planner
phd task "Add a caching layer for API responses"

# Add a constraint — opens interactive session to refine, then writes + commits
phd constrain "Never use global mutable state"

# Ask a question — opens interactive Claude session for back-and-forth Q&A
phd ask "How does the data pipeline work?"

# Trigger paper update manually
phd paper

# View agent status
phd status

# Post-commit hook (called by .claude/settings.json, not by humans)
phd hook post-commit
```

`phd task`, `phd constrain`, and `phd ask` open interactive Claude sessions.
The session transcript is captured to `.ph.daemon/logs/` for the web UI.
When the session ends, the finalized output dispatches to the appropriate action.

The web UI equivalents use a chat widget proxied via websocket.

## Agent Invocation

### CLI construction

```python
claude_cmd = [
    "claude",
    "--print",                          # omitted for interactive sessions
    "--output-format", "json",
    "--model", "claude-opus-4-6",       # always opus
    "--max-turns", "100",               # safety bound
    "--dangerously-skip-permissions",   # fully automated
    "--append-system-prompt", prompt,   # APPEND, not replace — preserves
                                        # Claude Code harness (memories, etc.)
    "--allowedTools", tools,            # scoped per agent type
]
```

**Important:** We use `--append-system-prompt` (not `--system-prompt`) so the
Claude Code harness remains intact — memories, CLAUDE.md, plugin features all
continue to work. Our prompt is additive context, not a replacement.

### Context Window Budget

Each task dispatched to the implementor should fit within a single context
window (1M tokens for Opus 4.6). The controller (implementor loop) is
responsible for:

- **Providing full context upfront** — don't make the subagent search for files.
  Include the issue body, relevant constraints, and prior issue discussions
  directly in the prompt.
- **Scoping tasks tightly** — if the planner creates an issue that would exceed
  one context window, it should be decomposed further.
- **Status protocol** — subagents report back with:
  - `DONE` — proceed to post-commit discussion
  - `DONE_WITH_CONCERNS` — completed but flagged doubts; review before proceeding
  - `BLOCKED` — cannot complete; controller provides more context or decomposes
  - `NEEDS_CONTEXT` — missing information; controller provides and re-dispatches

### Process Lifecycle

```
spawn → running → {completed | failed | killed}
```

- **Spawn:** `subprocess.Popen`, PID in SQLite, log file opened.
- **Running:** stdout/stderr piped to log file. SSE tails this.
- **Completed:** Exit 0. Post-processing (e.g., planner parses output for issues).
- **Failed:** Non-zero exit. Issue gets comment: "Agent failed, see session {id}."
- **Killed:** SIGTERM → 5s → SIGKILL. Issue gets comment.

### Implementor Loop

```python
async def implementor_loop():
    while True:
        issue = pick_next_issue()   # unblocked, unassigned, oldest first
        if issue is None:
            await asyncio.sleep(30)
            continue
        assign_issue(issue)
        session = spawn_implementor(issue)
        await session.wait()
        # post-commit hook handles issue discussion
        # loop continues to next issue
```

Runs as a background `asyncio.Task` inside FastAPI. Pausable from web UI.
