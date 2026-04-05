from __future__ import annotations


import pytest
from httpx import ASGITransport, AsyncClient

from daemon.app import create_app
from daemon.config import ProjectConfig
from daemon.db import Database
from daemon.github.issues import GitHubIssues
from daemon.agents.implementor import ImplementorLoop


@pytest.fixture
def app(config: ProjectConfig):
    return create_app(config)


@pytest.fixture
async def client(app, config: ProjectConfig, db: Database):
    # Manually wire up state that the lifespan would normally set,
    # since ASGITransport does not trigger ASGI lifespan events.
    gh = GitHubIssues(config=config, db=db)
    impl_loop = ImplementorLoop(config=config, db=db, gh=gh)
    app.state.db = db
    app.state.gh = gh
    app.state.impl_loop = impl_loop

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_dashboard_returns_200(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "ph.daemon" in resp.text


@pytest.mark.asyncio
async def test_agents_page_returns_200(client: AsyncClient) -> None:
    resp = await client.get("/agents")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_issues_page_returns_200(client: AsyncClient) -> None:
    resp = await client.get("/issues")
    assert resp.status_code == 200
