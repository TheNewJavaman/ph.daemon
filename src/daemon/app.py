from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from daemon.config import ProjectConfig
from daemon.db import Database
from daemon.github.issues import GitHubIssues
from daemon.agents.implementor import ImplementorLoop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB, start sync loop and implementor loop."""
    config: ProjectConfig = app.state.config
    db = Database(config.db_path)
    await db.init()

    gh = GitHubIssues(config=config, db=db)
    impl_loop = ImplementorLoop(config=config, db=db, gh=gh)

    app.state.db = db
    app.state.gh = gh
    app.state.impl_loop = impl_loop

    # Start background tasks
    sync_task = asyncio.create_task(_sync_loop(gh))
    impl_task = asyncio.create_task(impl_loop.run())

    yield

    # Shutdown
    impl_loop.stop()
    sync_task.cancel()
    impl_task.cancel()
    await db.close()


async def _sync_loop(gh: GitHubIssues) -> None:
    """Periodically sync issues from GitHub."""
    while True:
        try:
            await gh.sync_all()
        except Exception:
            pass  # Log and continue
        await asyncio.sleep(60)


def create_app(config: ProjectConfig) -> FastAPI:
    app = FastAPI(title="ph.daemon", lifespan=lifespan)
    app.state.config = config

    from daemon.web.routes import router
    app.include_router(router)

    from daemon.web.sse import sse_router
    app.include_router(sse_router)

    return app
