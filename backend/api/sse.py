import asyncio
import json
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

router = APIRouter()
event_queues = {}
event_loops = {}
event_history = defaultdict(lambda: deque(maxlen=200))
subscribers = defaultdict(dict)
event_ids = defaultdict(int)
event_lock = threading.RLock()
TERMINAL_EVENTS = {'RUN_COMPLETED', 'RUN_FAILED', 'error'}


def get_queue(run_id):
    if run_id not in event_queues:
        event_queues[run_id] = asyncio.Queue(maxsize=200)
    try:
        event_loops[run_id] = asyncio.get_running_loop()
    except RuntimeError:
        pass
    return event_queues[run_id]


def _put(queue, event):
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(event)


def emit(run_id, agent, message, event_type='thought'):
    with event_lock:
        event_ids[run_id] += 1
        event = {'id': event_ids[run_id], 'run_id': run_id, 'agent': agent,
                 'message': message, 'timestamp': datetime.now(timezone.utc).isoformat(),
                 'type': event_type}
        event_history[run_id].append(event)
        targets = list(subscribers[run_id].items())
        if run_id in event_queues:
            targets.append((event_queues[run_id], event_loops.get(run_id)))
        for queue, loop in targets:
            if loop and loop.is_running():
                loop.call_soon_threadsafe(_put, queue, event)
            else:
                _put(queue, event)


async def emit_async(run_id, agent, message, event_type='thought'):
    get_queue(run_id)
    emit(run_id, agent, message, event_type)


@router.get('/stream/{run_id}')
async def stream(run_id: str, request: Request):
    queue = asyncio.Queue(maxsize=200)
    loop = asyncio.get_running_loop()
    try:
        last_id = int(request.headers.get('last-event-id', '0'))
    except ValueError:
        last_id = 0
    with event_lock:
        replay = [event for event in event_history[run_id] if event['id'] > last_id]
        subscribers[run_id][queue] = loop

    async def event_generator():
        try:
            for event in replay:
                yield {'id': str(event['id']), 'data': json.dumps(event)}
                if event['type'] in TERMINAL_EVENTS:
                    return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield {'id': str(event['id']), 'data': json.dumps(event)}
                    if event['type'] in TERMINAL_EVENTS:
                        return
                except asyncio.TimeoutError:
                    yield {'data': json.dumps({'type': 'heartbeat', 'message': 'keepalive'})}
        finally:
            with event_lock:
                subscribers[run_id].pop(queue, None)

    return EventSourceResponse(event_generator())
