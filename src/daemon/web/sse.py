from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

sse_router = APIRouter()


async def _tail_log(log_path: str):
    """Async generator that tails a log file and yields new lines."""
    path = Path(log_path)
    if not path.exists():
        yield {"data": "Log file not found."}
        return

    with open(path) as f:
        # Start from beginning
        while True:
            line = f.readline()
            if line:
                yield {"data": line.rstrip()}
            else:
                await asyncio.sleep(0.5)


@sse_router.get("/api/agents/{session_id}/logs")
async def stream_logs(request: Request, session_id: str):
    db = request.app.state.db
    session = await db.get_session(session_id)
    if not session:
        return EventSourceResponse(iter([{"data": "Session not found."}]))

    return EventSourceResponse(_tail_log(session["log_path"]))
