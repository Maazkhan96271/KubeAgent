"""Server-Sent Events endpoints.

``GET /api/v1/incidents/{id}/stream`` streams everything that happens to one
incident: status transitions, the agent's thought/observation events, tool
execution results, the final diagnosis and human approvals.  Events are
replayed from the incident's history on connect, so a browser that attaches
late still sees the whole sequence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.api.incidents import GLOBAL_CHANNEL, store

logger = logging.getLogger(__name__)

#: No prefix: route paths below are already absolute under the API prefix.
router = APIRouter(tags=["stream"])

#: Keepalive interval so proxies do not close an idle stream.
HEARTBEAT_SECONDS = 15.0


def _sse(event_name: str, payload: dict[str, Any]) -> dict[str, str]:
    return {"event": event_name, "data": json.dumps(payload, default=str)}


async def _incident_events(
    incident_id: str, request: Request
) -> AsyncIterator[dict[str, str]]:
    """Replay the incident's history, then forward live events."""
    async with store.subscription(incident_id) as queue:
        incident = store.get(incident_id)
        yield _sse(
            "snapshot",
            {
                "type": "snapshot",
                "incident": incident.model_dump(mode="json") if incident else None,
            },
        )

        # Subscribe first, then replay: a duplicate is dropped via ``seen``,
        # whereas subscribing second could lose an event in the gap.
        seen: set[str] = set()
        for event in store.history(incident_id):
            seen.add(event.id)
            yield _sse(event.type, event.model_dump(mode="json"))

        while True:
            if await request.is_disconnected():
                logger.debug("SSE client disconnected from %s", incident_id)
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield _sse("ping", {"type": "ping"})
                continue
            if event.id in seen:
                continue
            seen.add(event.id)
            yield _sse(event.type, event.model_dump(mode="json"))


async def _global_events(request: Request) -> AsyncIterator[dict[str, str]]:
    """Forward every incident event (used by the dashboard overview)."""
    async with store.subscription(GLOBAL_CHANNEL) as queue:
        yield _sse("connected", {"type": "connected"})
        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield _sse("ping", {"type": "ping"})
                continue
            yield _sse(event.type, event.model_dump(mode="json"))


@router.get(
    "/incidents/{incident_id}/stream",
    response_class=EventSourceResponse,
    summary="Live event stream for one incident (SSE)",
)
async def stream_incident(incident_id: str, request: Request) -> EventSourceResponse:
    if store.get(incident_id) is None:
        raise HTTPException(
            status_code=404, detail=f"Incident '{incident_id}' not found"
        )
    return EventSourceResponse(_incident_events(incident_id, request))


@router.get(
    "/stream/incidents",
    response_class=EventSourceResponse,
    summary="Live event stream for all incidents (SSE)",
)
async def stream_all_incidents(request: Request) -> EventSourceResponse:
    return EventSourceResponse(_global_events(request))
