"""
In-memory presence tracking for real-time "who's viewing" indicators.
Uses asyncio.Queue for pub/sub broadcasting to SSE connections.
"""

import asyncio
from collections import defaultdict


# report_id (str) -> { user_id (str): { "user_id": ..., "name": ..., "email": ... } }
_presence: dict[str, dict[str, dict]] = defaultdict(dict)

# report_id (str) -> set of asyncio.Queue for broadcasting updates
_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


def add_viewer(report_id: str, user_id: str, user_info: dict):
    _presence[report_id][user_id] = user_info
    _broadcast(report_id)


def remove_viewer(report_id: str, user_id: str):
    _presence[report_id].pop(user_id, None)
    if not _presence[report_id]:
        _presence.pop(report_id, None)
    _broadcast(report_id)


def get_viewers(report_id: str) -> list[dict]:
    return list(_presence.get(report_id, {}).values())


def subscribe(report_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    _subscribers[report_id].add(q)
    return q


def unsubscribe(report_id: str, q: asyncio.Queue):
    _subscribers[report_id].discard(q)
    if not _subscribers[report_id]:
        _subscribers.pop(report_id, None)


def _broadcast(report_id: str):
    viewers = get_viewers(report_id)
    for q in list(_subscribers.get(report_id, set())):
        try:
            q.put_nowait(viewers)
        except asyncio.QueueFull:
            pass
