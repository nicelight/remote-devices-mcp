"""Offline resilience guards for the gateway: fast fail, SSE caps and /health.

The incident behind these tests: the node went offline after a Windows reboot.
The client then showed its own generic frontend error, ``/health`` reported
``open_sse_streams: 9`` for a single user while ``active_tunnels_count`` was 0,
and every ``tools/call`` sat on the no-tunnel wait for ~8 s before answering.

Everything here runs without a network and without a node. The tunnel is either
absent or a stub, and the SSE streams are driven directly through the endpoint
(the same request-stub pattern as ``test_gateway_url_contract`` and
``test_gateway_session_id``).
"""

import asyncio
import json
import time
from collections import deque

from starlette.requests import Request

import gateway

USER = "node-one"
TOKEN = "tok"


# ---------------------------------------------------------------------------
# Fixtures: a registry on disk and hand-built ASGI requests (no sockets)
# ---------------------------------------------------------------------------

def _request(query="", path="/sse", method="GET", body=b"", headers=None):
    hdrs = [(b"host", b"smart-server.online"), (b"content-type", b"application/json")]
    for key, value in (headers or {}).items():
        hdrs.append((key.lower().encode(), value.encode()))

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "headers": hdrs,
        "query_string": query.lstrip("?").encode(),
        "scheme": "https",
        "server": ("smart-server.online", 443),
        "client": ("127.0.0.1", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _sse_request(session_id=None):
    headers = {"accept": "text/event-stream"}
    if session_id:
        headers["mcp-session-id"] = session_id
    return _request("user=%s&token=%s" % (USER, TOKEN), path="/sse", headers=headers)


def _initialize_request():
    body = json.dumps({
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25"},
    }).encode()
    return _request("user=%s&token=%s" % (USER, TOKEN), path="/sse", method="POST",
                    body=body, headers={"accept": "application/json, text/event-stream"})


def _registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({USER: {"token": TOKEN}}))
    monkeypatch.setattr(gateway, "REGISTRY_PATH", str(path))


def _clean_sse_state(monkeypatch):
    """Every container the SSE paths touch, replaced so tests cannot leak."""
    monkeypatch.setattr(gateway, "active_sse_subscribers", {})
    monkeypatch.setattr(gateway, "active_sse_sessions", {})
    monkeypatch.setattr(gateway, "active_sse_session_queues", {})
    monkeypatch.setattr(gateway, "active_sessions", {})
    monkeypatch.setattr(gateway, "latest_session_by_user", {})


# ---------------------------------------------------------------------------
# (1) tools/call with no tunnel fails fast
# ---------------------------------------------------------------------------

def test_tools_call_without_a_tunnel_answers_fast(monkeypatch):
    """A rebooted host must not hold the client's call open for ~8 s.

    The client's own frontend gives a tool call roughly 30 s, so the offline
    answer has to be quick enough to be useful: the whole call is bounded to
    ~2 s here, which leaves room for the ~2 s post-reconnect retry wait.
    """
    monkeypatch.setattr(gateway, "active_tunnels", {})

    started = time.monotonic()
    result = asyncio.run(gateway.call_remote_tool(USER, "bash_exec", {"command": "echo hi"}))
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, "the no-tunnel path still waits %.2fs" % elapsed
    assert isinstance(result, dict)
    assert result["exit_code"] != 0
    assert result["exit_code"] == 1
    assert result["stdout"] == ""
    assert USER in result["stderr"], "the message must name the node"
    assert "not connected" in result["stderr"], "and say the agent is not connected"


# ---------------------------------------------------------------------------
# (2) the per-user SSE cap
# ---------------------------------------------------------------------------

def test_three_streams_for_one_user_keep_only_the_cap(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _clean_sse_state(monkeypatch)

    async def open_three():
        opened = []
        for _ in range(3):
            response = await gateway.sse_endpoint(_sse_request())
            assert response.status_code == 200
            opened.append(gateway.active_sse_subscribers[USER][-1])
        return opened

    first, second, third = asyncio.run(open_three())

    queues = gateway.active_sse_subscribers[USER]
    assert gateway.MAX_SSE_PER_USER == 2
    assert len(queues) == gateway.MAX_SSE_PER_USER
    assert queues == [second, third], "the OLDEST stream must be the one evicted"
    assert first.get_nowait() is None, "the evicted stream must get the close sentinel"
    assert second.empty() and third.empty(), "live streams must not be touched"


# ---------------------------------------------------------------------------
# (3) a stream that cannot be written to is dropped, framing intact
# ---------------------------------------------------------------------------

class _BrokenStreamQueue(asyncio.Queue):
    """A queue whose reader is gone: every read raises, as a dead socket does.

    A subclass is used instead of patching the instance because ``asyncio.Queue``
    defines ``__slots__``, so ``queue.get = ...`` is not allowed.
    """

    async def get(self):
        raise RuntimeError("the client socket is gone")


def test_a_stream_whose_queue_write_raises_is_removed(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _clean_sse_state(monkeypatch)
    # sse_endpoint builds its queue through the asyncio module attribute.
    monkeypatch.setattr(asyncio, "Queue", _BrokenStreamQueue)

    async def drain():
        response = await gateway.sse_endpoint(_sse_request())
        queue = gateway.active_sse_subscribers[USER][-1]
        assert isinstance(queue, _BrokenStreamQueue), "the broken queue must be used"
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return queue, chunks

    queue, chunks = asyncio.run(drain())

    assert len(chunks) == 1 and chunks[0].startswith(
        "event: endpoint\ndata: /messages?user=%s&token=%s&stream_id=" % (USER, TOKEN)
    ), "the existing endpoint framing must survive, then the stream closes"
    assert queue not in gateway.active_sse_subscribers.get(USER, [])
    assert queue not in list(gateway.active_sse_sessions.values())
    assert queue not in [q for qs in gateway.active_sse_session_queues.values() for q in qs]


def test_an_evicted_stream_is_also_dropped_when_it_is_read(tmp_path, monkeypatch):
    """The None sentinel really closes the evicted stream, it is not just set."""
    _registry(tmp_path, monkeypatch)
    _clean_sse_state(monkeypatch)

    async def drain():
        oldest = await gateway.sse_endpoint(_sse_request())
        evicted = gateway.active_sse_subscribers[USER][-1]
        for _ in range(gateway.MAX_SSE_PER_USER):
            await gateway.sse_endpoint(_sse_request())
        survivors = list(gateway.active_sse_subscribers[USER])
        chunks = [chunk async for chunk in oldest.body_iterator]
        return evicted, survivors, chunks

    evicted, survivors, chunks = asyncio.run(drain())

    assert len(chunks) == 1 and chunks[0].startswith(
        "event: endpoint\ndata: /messages?user=%s&token=%s&stream_id=" % (USER, TOKEN)
    ), "the evicted stream closes after its endpoint frame"
    assert evicted.empty(), "the evicted queue consumed its close sentinel"
    assert evicted not in gateway.active_sse_subscribers.get(USER, [])
    assert len(survivors) == gateway.MAX_SSE_PER_USER
    assert gateway.active_sse_subscribers[USER] == survivors


# ---------------------------------------------------------------------------
# (4) a new initialize closes the superseded session's stream
# ---------------------------------------------------------------------------

def test_initialize_closes_the_stream_of_the_superseded_session(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _clean_sse_state(monkeypatch)

    async def scenario():
        first = await gateway.messages_endpoint(_initialize_request())
        first_session = first.headers["mcp-session-id"]
        await gateway.sse_endpoint(_sse_request(session_id=first_session))
        stream = gateway.active_sse_subscribers[USER][-1]
        assert gateway.active_sse_session_queues[first_session] == [stream]

        second = await gateway.messages_endpoint(_initialize_request())
        return first_session, second.headers["mcp-session-id"], stream

    first_session, second_session, stream = asyncio.run(scenario())

    assert first_session != second_session, "every initialize still gets its own id"
    assert first_session not in gateway.active_sessions, "the old session must be forgotten"
    assert first_session not in gateway.active_sse_session_queues
    assert stream.get_nowait() is None, "the superseded stream must be told to close"


# ---------------------------------------------------------------------------
# (5) /health reports the tunnel lifecycle
# ---------------------------------------------------------------------------

def test_health_reports_last_tunnel_seen_and_tunnel_events(monkeypatch):
    monkeypatch.setattr(gateway, "active_tunnels", {})
    _clean_sse_state(monkeypatch)

    response = asyncio.run(gateway.health(_request("user=%s" % USER)))
    data = json.loads(response.body)

    assert response.status_code == 200
    assert data["service"] == "remote_harness_mcp_gateway"
    assert "last_tunnel_seen" in data
    assert "tunnel_events" in data
    assert isinstance(data["tunnel_events"], list)
    # The existing diagnostics keep their meaning and types.
    assert data["open_sse_streams"] == 0
    assert data["streams_per_user"] == {}
    assert data["active_tunnels_count"] == 0


def test_tunnel_events_are_a_bounded_ring_buffer(monkeypatch):
    monkeypatch.setattr(gateway, "tunnel_events", deque(maxlen=gateway.TUNNEL_EVENTS_MAX))
    monkeypatch.setattr(gateway, "last_tunnel_seen", None)

    for i in range(gateway.TUNNEL_EVENTS_MAX + 2):
        gateway.record_tunnel_event("connect" if i % 2 == 0 else "disconnect",
                                    USER, "reason-%d" % i)

    events = list(gateway.tunnel_events)
    assert gateway.TUNNEL_EVENTS_MAX == 10
    assert len(events) == gateway.TUNNEL_EVENTS_MAX
    assert events[0]["reason"] == "reason-2", "the oldest entries are dropped"
    assert events[-1]["reason"] == "reason-%d" % (gateway.TUNNEL_EVENTS_MAX + 1)
    assert set(events[-1]) == {"ts", "event", "user", "reason"}
    assert events[-1]["event"] == "disconnect"
    assert gateway.last_tunnel_seen is not None, "a connect stamps last_tunnel_seen"
    assert "T" in gateway.last_tunnel_seen, "and it is an ISO-8601 timestamp"
