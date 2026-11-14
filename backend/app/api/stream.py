"""Server-Sent Events stream for live incident updates.

The frontend subscribes once to ``/api/v1/stream/incidents`` and receives every
incident lifecycle event emitted by :class:`app.api.incidents.IncidentStore`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.api.incidents import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])

#: Keepalive interval so proxies do not close an idle stream.
HEARTBEAT_SECONDS = 15.0


async def _event_source(request: Request) -> AsyncIterator[dict[str, Any]]:
    async with store.subscription() as queue:
        yield {
            "event": "connected",
            "data": json.dumps({"type": "connected"}),
        }
        while True:
            if await request.is_disconnected():
                logger.debug("SSE client disconnected")
                return
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": json.dumps({"type": "ping"})}
                continue
            yield {
                "event": str(event.get("type", "message")),
                "data": json.dumps(event, default=str),
            }


@router.get(
    "/incidents",
    summary="Live incident event stream (SSE)",
    response_class=EventSourceResponse,
)
async def stream_incidents(request: Request) -> EventSourceResponse:
    return EventSourceResponse(_event_source(request))
