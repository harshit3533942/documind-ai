"""Server-Sent Events (SSE) helpers.

SSE is a tiny text protocol over a normal HTTP response that stays open:

    event: token
    data: {"text": "Employees get "}

    event: done
    data: {...}

Each event ends with a blank line. Compared with WebSockets it is one-way
(server -> client), works through ordinary HTTP proxies, needs no extra
protocol upgrade, and is exactly what "stream an answer" / "push status
updates" needs.
"""

import json
from typing import Any

from fastapi.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # nginx buffers proxied responses by default - that would deliver the whole
    # stream at once at the end. This header turns buffering off per response.
    "X-Accel-Buffering": "no",
}


def sse_event(event: str, data: Any) -> str:
    # json.dumps never emits raw newlines, so `data:` is always a single line.
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


def sse_response(events) -> StreamingResponse:  # noqa: ANN001 - (async) iterator of str
    return StreamingResponse(events, media_type="text/event-stream", headers=SSE_HEADERS)
