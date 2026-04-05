from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = request.app.state.db
    config = request.app.state.config
    impl_loop = request.app.state.impl_loop

    running = await db.list_sessions(status="running")
    recent = await db.list_sessions()
    open_issues = await db.list_issues(state="open")

    # Recent commits from git log
    try:
        git_log = subprocess.check_output(
            ["git", "log", "--oneline", "-10"],
            cwd=config.project_dir,
        ).decode().strip().splitlines()
    except subprocess.CalledProcessError:
        git_log = []

    return templates.TemplateResponse(request, "dashboard.html", {
        "running_agents": running,
        "recent_sessions": recent[:20],
        "open_issues": open_issues,
        "recent_commits": git_log,
        "impl_paused": impl_loop.is_paused,
        "repo": config.repo,
    })


@router.get("/agents", response_class=HTMLResponse)
async def agents_list(request: Request):
    db = request.app.state.db
    sessions = await db.list_sessions()
    return templates.TemplateResponse(request, "agents.html", {
        "sessions": sessions,
    })


@router.get("/agents/{session_id}", response_class=HTMLResponse)
async def agent_detail(request: Request, session_id: str):
    db = request.app.state.db
    session = await db.get_session(session_id)
    return templates.TemplateResponse(request, "session.html", {
        "session": session,
    })


@router.get("/issues", response_class=HTMLResponse)
async def issues_list(request: Request):
    db = request.app.state.db
    issues = await db.list_issues()
    return templates.TemplateResponse(request, "issues.html", {
        "issues": issues,
        "repo": request.app.state.config.repo,
    })


@router.get("/paper", response_class=HTMLResponse)
async def paper_view(request: Request):
    config = request.app.state.config
    pdf_exists = (config.paper_dir / "main.pdf").exists()
    return templates.TemplateResponse(request, "paper.html", {
        "pdf_exists": pdf_exists,
    })


@router.get("/constraints", response_class=HTMLResponse)
async def constraints_view(request: Request):
    config = request.app.state.config
    content = ""
    if config.constraints_path.exists():
        content = config.constraints_path.read_text()
    return templates.TemplateResponse(request, "constraints.html", {
        "content": content,
    })


# --- API endpoints for htmx actions ---

@router.post("/api/impl/pause")
async def pause_impl(request: Request):
    request.app.state.impl_loop.pause()
    return HTMLResponse('<span class="status paused">Paused</span>')


@router.post("/api/impl/resume")
async def resume_impl(request: Request):
    request.app.state.impl_loop.resume()
    return HTMLResponse('<span class="status running">Running</span>')


@router.post("/api/agents/{session_id}/kill")
async def kill_agent(request: Request, session_id: str):
    # Find the running agent and kill it
    db = request.app.state.db
    session = await db.get_session(session_id)
    if session and session["status"] == "running" and session["pid"]:
        import os
        import signal
        try:
            os.kill(session["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        await db.update_session(session_id, status="killed")
    return HTMLResponse('<span class="status killed">Killed</span>')


@router.post("/api/paper/update")
async def trigger_paper_update(request: Request):
    from daemon.agents.paper import run_paper_update
    config = request.app.state.config
    db = request.app.state.db
    asyncio.create_task(run_paper_update(config, db))
    return HTMLResponse('<p style="color: var(--green);">Paper update triggered.</p>')


@router.post("/api/constraints/add")
async def add_constraint(request: Request):
    form = await request.form()
    description = form.get("description", "")
    if not description:
        return HTMLResponse("Description required", status_code=400)

    config = request.app.state.config
    db = request.app.state.db

    # Spawn an ephemeral agent to handle the constraint addition
    from daemon.agents.ephemeral import run_ephemeral_interactive
    asyncio.create_task(
        run_ephemeral_interactive(config, db, f"Add this constraint: {description}")
    )

    # Redirect back to constraints page
    return RedirectResponse("/constraints", status_code=303)
