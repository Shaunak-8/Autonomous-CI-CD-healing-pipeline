import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.db as api_db
import api.routes as routes
from db.db import Base


def test_project_list_does_not_block_event_loop(monkeypatch):
    import time
    async def verify(_request):
        pass
    def slow_projects(_user):
        time.sleep(0.1)
        return []
    monkeypatch.setattr(routes, 'verify_token', verify)
    monkeypatch.setattr(routes, 'get_user_id', lambda _: 'user-1')
    monkeypatch.setattr(routes, 'get_projects', slow_projects)
    async def check():
        task = asyncio.create_task(routes.list_projects(SimpleNamespace()))
        await asyncio.sleep(0.02)
        assert not task.done(), 'Database query blocked the API event loop'
        assert await task == {'projects': []}
    asyncio.run(check())


def test_run_endpoint_persists_to_same_database_as_orchestrator(monkeypatch, tmp_path):
    from fastapi import BackgroundTasks
    from api.models import RunRequest
    from db.models import Run
    engine = create_engine(f'sqlite:///{tmp_path / "runs.db"}')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(routes, 'SessionLocal', sessions)
    async def verify(_request):
        pass
    monkeypatch.setattr(routes, 'verify_token', verify)
    monkeypatch.setattr(routes, 'get_user_id', lambda _: 'user-1')
    monkeypatch.setattr(routes, 'get_queue', lambda _: None)
    monkeypatch.setattr(routes, 'get_project', lambda _: {'user_id': 'user-1'})
    tasks = BackgroundTasks()
    try:
        result = asyncio.run(routes.run_agent(
            RunRequest(repo_url='https://github.com/example/repo', team_name='team', leader_name='leader', project_id='project-1'),
            SimpleNamespace(headers={}), tasks, None))
        with sessions() as db:
            run = db.get(Run, result['run_id'])
            assert run.project_id == 'project-1'
            assert run.status.value == 'running'
        assert len(tasks.tasks) == 1
    finally:
        engine.dispose()


def test_project_persistence_uses_configured_database(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine)
    monkeypatch.setattr(api_db, "SessionLocal", test_session)

    try:
        api_db.ensure_user("user-1", "user@example.com", {"source": "test"})
        project = api_db.create_project(
            "user-1",
            "https://github.com/example/repository",
            "repository",
            ["imported"],
            "private",
        )

        assert project["id"]
        assert project["visibility"] == "private"
        assert api_db.get_projects("user-1")[0]["repo_url"] == project["repo_url"]
        assert api_db.get_project(project["id"])["name"] == "repository"
        assert api_db.get_project_stats(project["id"]) == {"runs": 0, "issues": 0}
    finally:
        engine.dispose()


def test_project_detail_route_is_owner_scoped(monkeypatch):
    async def verify_token(_request):
        return None

    monkeypatch.setattr(routes, "verify_token", verify_token)
    monkeypatch.setattr(routes, "get_user_id", lambda _request: "user-1")
    monkeypatch.setattr(
        routes,
        "get_project",
        lambda _project_id: {"id": "project-1", "user_id": "user-1", "repo_url": "https://github.com/example/repo"},
    )
    request = SimpleNamespace()

    project = asyncio.run(routes.fetch_project("project-1", request))
    assert project["id"] == "project-1"

    monkeypatch.setattr(
        routes,
        "get_project",
        lambda _project_id: {"id": "project-2", "user_id": "other-user", "repo_url": "https://github.com/other/repo"},
    )
    with pytest.raises(routes.HTTPException) as error:
        asyncio.run(routes.fetch_project("project-2", request))
    assert error.value.status_code == 404
