import asyncio
import io
from unittest.mock import Mock

from api import sse


def test_background_start_is_windows_safe_and_records_clone_failure(monkeypatch):
    import api.routes as routes
    db = Mock()
    monkeypatch.setattr(routes, 'SessionLocal', Mock(return_value=db))
    monkeypatch.setattr(routes.git_ops, 'clone_repo', Mock(return_value=False))
    monkeypatch.setattr(routes, 'emit', Mock())
    update = Mock()
    monkeypatch.setattr(routes.crud, 'update_run', update)
    output = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
    monkeypatch.setattr('sys.stdout', output)
    routes.background_agent_run('example-run', 'https://github.com/example/repo', 'team', 'leader', 'main')
    assert update.call_args.kwargs['status'] == 'failed'
    assert 'cloning' in update.call_args.kwargs['memory']['error']
    db.close.assert_called_once()


def test_sse_worker_events_wake_the_waiting_event_loop():
    async def check():
        queue = sse.get_queue('thread-test')
        waiter = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        await asyncio.to_thread(sse.emit, 'thread-test', 'AnalyzerAgent', 'Analysis started', 'ANALYSIS_STARTED')
        event = await asyncio.wait_for(waiter, timeout=1)
        assert event['type'] == 'ANALYSIS_STARTED'
        sse.event_queues.pop('thread-test')
        sse.event_loops.pop('thread-test')

    asyncio.run(check(), debug=True)


def test_run_failure_details_are_persisted_as_json(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from db import crud, models
    monkeypatch.setattr(crud, '_resolve_enum_label', lambda db, enum, value, fallback=None: value)
    engine = create_engine('sqlite:///:memory:')
    models.Run.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.Run(id='json-test', repo_url='local', team_name='test', leader_name='test'))
        db.commit()
        crud.update_run(db, 'json-test', status='failed', memory={'error': 'clone failed'})
        row = crud.get_run(db, 'json-test')
        assert row.memory == {'error': 'clone failed'}
        assert row.status.value == 'failed'
    engine.dispose()
