"""The gateway relays public shares, and its tool surface must not drift.

A share is published by the node as ``<slug>`` where the slug ends in 16 hex
characters that act as the credential, and the gateway exposes it on the one
shared domain as ``/<node>/<slug>/<path>``. The gateway deploys as a single file
and must not import node code, so it carries its own static ``tools/list``. That
duplication is the risk this file guards: the list is compared against the node's
``core/mcp_tools.TOOLS``, and a tool added on one side but not the other fails
the build instead of silently disappearing from a client's tool picker.

The second half exercises the catch-all ``/{user}/{rest:path}`` route. It is a
two-segment catch-all, so it must stay last in ``routes``: anywhere else it would
swallow ``/sse``, ``/mcp``, ``/health`` and ``/api/register``.
"""

import asyncio
import base64
import json
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.routing import Match

import gateway
from core import mcp_tools

NODE = "node-one"
TOKEN = "tok"
SLUG = "docs-0123456789abcdef"
SHARE = f"{NODE}/{SLUG}/report.txt"


# ---------------------------------------------------------------------------
# Fixtures: no network, a registry on disk and a fake node relay
# ---------------------------------------------------------------------------

def _registry(tmp_path, monkeypatch, users=(NODE,)):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({u: {"token": TOKEN} for u in users}))
    monkeypatch.setattr(gateway, "REGISTRY_PATH", str(path))
    return path


class FakeRelay:
    """Stand-in for ``gateway.call_remote_tool`` that records every call."""

    def __init__(self, result=None):
        self.result = {} if result is None else result
        self.calls = []

    async def __call__(self, user, name, args):
        self.calls.append((user, name, args))
        return self.result


def _relay(monkeypatch, result=None):
    fake = FakeRelay(result)
    monkeypatch.setattr(gateway, "call_remote_tool", fake)
    return fake


def _node_online(monkeypatch):
    tunnels = dict(gateway.active_tunnels)
    tunnels[NODE] = {"ws": object(), "pending": {}}
    monkeypatch.setattr(gateway, "active_tunnels", tunnels)
    return tunnels


def _share_request(user=NODE, rest=f"{SLUG}/report.txt", method="GET", query=""):
    """A Request shaped the way Starlette's catch-all route hands it to us."""
    scope = {
        "type": "http",
        "method": method,
        "path": f"/{user}/{rest}",
        "raw_path": f"/{user}/{rest}".encode(),
        "root_path": "",
        "path_params": {"user": user, "rest": rest},
        "headers": [(b"host", b"smart-server.online")],
        "query_string": query.encode(),
        "scheme": "https",
        "server": ("smart-server.online", 443),
        "client": ("127.0.0.1", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _call(request):
    return asyncio.run(gateway.public_share_endpoint(request))


def _post_request(path, body, headers=None):
    raw = json.dumps(body).encode()
    hdrs = [
        (b"host", b"smart-server.online"),
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ]
    for key, value in (headers or {}).items():
        hdrs.append((key.lower().encode(), value.encode()))

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "headers": hdrs,
        "query_string": urlparse(path).query.encode(),
        "scheme": "https",
        "server": ("smart-server.online", 443),
        "client": ("127.0.0.1", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


def _asgi(method, path, query=b""):
    """Drive the real app (router + middleware) without opening a socket."""
    headers = [(b"host", b"smart-server.online")]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "headers": headers,
        "server": ("smart-server.online", 443),
        "client": ("127.0.0.1", 12345),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(gateway.app(scope, receive, send))

    status = None
    out = {}
    body = b""
    for message in sent:
        if message["type"] == "http.response.start":
            status = message["status"]
            out = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")
    return status, out, body


# ---------------------------------------------------------------------------
# (a) Tool surface parity: the gateway's static tools/list vs the node's TOOLS
# ---------------------------------------------------------------------------

#: Tools the gateway advertises that the node itself does not have, because the
#: gateway owns their policy and translates them before the node sees a call:
#: system_change is forwarded as bash_exec/run_job, system_write as write_file.
GATEWAY_ONLY_TOOLS = {"system_change", "system_write"}

#: Tools the gateway deliberately advertises as read-only while the node's own list
#: keeps the honest hint. The client reads the gateway's surface and skips its
#: dialog only for a tool it sees as read-only, so every call that must not
#: interrupt the operator is claimed read-only here. What keeps the claim is never
#: the hint: the classifier refuses the gated classes and routes them to
#: system_change / system_write, and the device branch is refused on the node by
#: MESH_DEVICE / MESH_DEVICE_ACTIONS / MESH_DEVICE_PIM / MESH_READ_ONLY.
GATEWAY_UNCONFIRMED_TOOLS = {
    "bash_exec", "run_job", "write_file", "edit_file", "job_kill",
    "share_file", "serve_dir", "unshare",
    "device_control", "device_capture", "device_messages",
}


def test_advertised_tools_match_the_node_exactly(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    response = asyncio.run(gateway.messages_endpoint(
        _post_request(f"/mcp?user={NODE}&token={TOKEN}",
                      {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    ))
    advertised = {tool["name"] for tool in json.loads(response.body)["result"]["tools"]}
    node = {tool["name"] for tool in mcp_tools.TOOLS}

    assert advertised - node == GATEWAY_ONLY_TOOLS, (
        "unexpected gateway-only tools: %s" % sorted((advertised - node) - GATEWAY_ONLY_TOOLS))
    assert node - advertised == set(), (
        "the node has tools the gateway never advertises: %s" % sorted(node - advertised))


def test_advertised_confirmation_hints_match_the_node(tmp_path, monkeypatch):
    """The client's "confirm this action?" prompt is driven by these hints.

    An advertised tool with no annotations is treated as destructive, which is
    why Gemini Spark asked before *every* call. The gateway carries its own copy
    of the policy (it may not import node code), so the two sides are compared
    hint for hint instead of trusting the duplication - the tools the gateway owns
    (its unconfirmed set plus the system_change / system_write twins) are the
    documented, intentional exceptions.
    """
    _registry(tmp_path, monkeypatch)
    response = asyncio.run(gateway.messages_endpoint(
        _post_request(f"/mcp?user={NODE}&token={TOKEN}",
                      {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    ))
    advertised = {tool["name"]: tool.get("annotations")
                  for tool in json.loads(response.body)["result"]["tools"]}
    node = {spec["name"]: spec.get("annotations") for spec in mcp_tools.TOOLS}

    assert set(advertised) - set(node) == GATEWAY_ONLY_TOOLS

    for name, hints in node.items():
        assert hints is not None, "the node advertises %s without confirmation hints" % name
        if name in GATEWAY_UNCONFIRMED_TOOLS:
            continue
        assert advertised[name] == hints, (
            "confirmation hints for %s differ: gateway=%r node=%r"
            % (name, advertised[name], hints))

    # The gateway's own exceptions still have to be exactly what the policy says.
    for name in GATEWAY_UNCONFIRMED_TOOLS:
        assert advertised[name]["readOnlyHint"] is False, name
        assert advertised[name]["destructiveHint"] is (name in {"bash_exec", "run_job"}), name
    for name in ("bash_exec", "write_file", "edit_file"):
        assert node[name]["readOnlyHint"] is False, (
            "the node's own surface must stay honest: it has no classifier that would "
            "keep a read-only claim true for %s" % name)
    destructive = {
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": False,
    }
    assert advertised["system_change"] == destructive
    assert advertised["system_write"] == destructive


#: The four device tools: the only advertisements whose text is pinned to the node's.
DEVICE_TOOLS = ("device_info", "device_control", "device_capture", "device_messages")


def _device_schema_shape(schema):
    """The parts of a device schema the gateway must copy from the node exactly.

    ``required`` is read raw instead of defaulted: the node's device_info has no
    ``required`` key at all, and an empty list is a different advertisement.
    """
    return {
        "required": schema.get("required"),
        "additionalProperties": schema.get("additionalProperties"),
        "properties": {
            prop: {key: value.get(key) for key in ("type", "enum", "description")}
            for prop, value in schema["properties"].items()
        },
    }


def test_the_four_device_tools_match_the_node_field_for_field(tmp_path, monkeypatch):
    """The device tools are the four advertisements that must be word for word.

    The rest of the gateway's static list is deliberately allowed to keep older,
    shorter prose - nobody decides anything on a reworded ``list_dir``, and forcing
    all twenty to match would fail for reasons that are not a safety problem. The
    device branch is different in three ways. It is new, so there is no legacy
    wording to preserve; the node is what actually executes these actions, which
    makes its spec the contract the client must be shown; and these four are the
    ones a phone user's safety depends on - this exact text is where the model
    learns that the camera and the messages are gated by the node's own switches
    (``MESH_DEVICE``, ``MESH_DEVICE_ACTIONS``, ``MESH_DEVICE_PIM``,
    ``MESH_READ_ONLY``) rather than by a dialog, and that a section that is merely
    unexposed reads as unavailable rather than broken. The two tests above already
    compare names and confirmation hints for the whole surface; this one pins the
    description and the schema internals of these four, so a word, an enum or a
    property cannot drift on one side only.
    """
    _registry(tmp_path, monkeypatch)
    response = asyncio.run(gateway.messages_endpoint(
        _post_request(f"/mcp?user={NODE}&token={TOKEN}",
                      {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    ))
    advertised = {tool["name"]: tool for tool in json.loads(response.body)["result"]["tools"]}
    node = {spec["name"]: spec for spec in mcp_tools.TOOLS}

    for name in DEVICE_TOOLS:
        assert advertised[name]["description"] == node[name]["description"], (
            "the description of %s is no longer the node's: the node executes the tool, "
            "the client reads this copy" % name)
        assert (_device_schema_shape(advertised[name]["inputSchema"])
                == _device_schema_shape(node[name]["inputSchema"])), (
            "the input schema of %s drifted from the node's: property names, required, "
            "enums, types, additionalProperties and per-property descriptions must match" % name)


def test_the_four_share_tools_are_advertised(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    response = asyncio.run(gateway.messages_endpoint(
        _post_request(f"/mcp?user={NODE}&token={TOKEN}",
                      {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    ))
    advertised = {tool["name"] for tool in json.loads(response.body)["result"]["tools"]}
    assert {"share_file", "serve_dir", "share_list", "unshare"} <= advertised
    assert {t["name"] for t in gateway.SHARE_TOOL_SPECS} == {
        "share_file", "serve_dir", "share_list", "unshare"}


def test_share_specs_carry_a_usable_input_schema():
    by_name = {t["name"]: t for t in gateway.SHARE_TOOL_SPECS}
    assert by_name["share_file"]["inputSchema"]["required"] == ["path"]
    assert by_name["serve_dir"]["inputSchema"]["required"] == ["path"]
    assert by_name["unshare"]["inputSchema"]["required"] == ["name"]
    assert by_name["share_list"]["inputSchema"]["properties"] == {}


# ---------------------------------------------------------------------------
# tools/call formatting for the share tools
# ---------------------------------------------------------------------------

def _tool_text(tmp_path, monkeypatch, tool, args, result):
    _registry(tmp_path, monkeypatch)
    _relay(monkeypatch, result)
    response = asyncio.run(gateway.messages_endpoint(
        _post_request(f"/mcp?user={NODE}&token={TOKEN}",
                      {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}})
    ))
    payload = json.loads(response.body)["result"]
    return payload["content"][0]["text"], payload["isError"]


def test_share_file_reports_the_public_link_and_the_way_to_revoke_it(tmp_path, monkeypatch):
    text, is_error = _tool_text(tmp_path, monkeypatch, "share_file", {"path": "/tmp/a.txt"}, {
        "url": "https://mesh.example.com/node-one/docs-0123456789abcdef/a.txt",
        "name": "docs",
        "local_url": "http://127.0.0.1:41234/a.txt",
    })

    assert is_error is False
    assert "Published. Public link: https://mesh.example.com/node-one/docs-0123456789abcdef/a.txt" in text
    assert 'unshare(name="docs")' in text
    assert "Local URL on the node: http://127.0.0.1:41234/a.txt" in text


def test_share_list_renders_a_table_or_says_there_are_none(tmp_path, monkeypatch):
    text, _ = _tool_text(tmp_path, monkeypatch, "share_list", {}, {"shares": []})
    assert text == "No active shares."

    text, _ = _tool_text(tmp_path, monkeypatch, "share_list", {}, {"shares": [
        {"kind": "file", "name": "docs", "url": "https://m/x", "running": False},
        {"kind": "dir", "name": "site", "url": "https://m/y", "running": True},
    ]})
    assert text.splitlines() == [
        "kind | name | url | running",
        "file | docs | https://m/x | False",
        "dir | site | https://m/y | True",
    ]


def test_unshare_reports_what_was_stopped_and_removed(tmp_path, monkeypatch):
    text, is_error = _tool_text(tmp_path, monkeypatch, "unshare", {"name": "docs"}, {
        "name": "docs", "slug": "docs-0123456789abcdef",
        "server_stopped": True, "files_removed": 2,
    })

    assert is_error is False
    assert text == ("Revoked share 'docs' (docs-0123456789abcdef). "
                    "Server stopped=True, files removed=2.")


def test_a_relay_error_is_reported_as_isError(tmp_path, monkeypatch):
    text, is_error = _tool_text(tmp_path, monkeypatch, "share_file", {"path": "/tmp/a.txt"},
                                {"error": "no such file: /tmp/a.txt"})
    assert is_error is True
    assert "no such file" in text


# ---------------------------------------------------------------------------
# (b) The catch-all route relays to the node
# ---------------------------------------------------------------------------

def test_get_relays_the_node_body(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    payload = b"published bytes"
    fake = _relay(monkeypatch, {
        "status": 200,
        "body_b64": base64.b64encode(payload).decode(),
        "headers": {"content-type": "text/plain; charset=utf-8"},
    })

    response = _call(_share_request())

    assert response.status_code == 200
    assert response.body == payload
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    user, name, args = fake.calls[0]
    assert (user, name) == (NODE, "_http_share")
    assert args == {"slug": SLUG, "path": "/report.txt", "method": "GET", "query": ""}


def test_the_query_string_and_the_whole_sub_path_are_forwarded(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    fake = _relay(monkeypatch, {"status": 200, "body_b64": "", "headers": {}})

    _call(_share_request(rest=f"{SLUG}/a/b/c.txt", query="v=2&x=1"))

    assert fake.calls[0][2]["path"] == "/a/b/c.txt"
    assert fake.calls[0][2]["query"] == "v=2&x=1"


def test_head_keeps_the_length_but_sends_no_body(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    _relay(monkeypatch, {
        "status": 200,
        "body_b64": "",
        "headers": {"content-type": "application/pdf", "content-length": "4096"},
    })

    response = _call(_share_request(method="HEAD"))

    assert response.status_code == 200
    assert response.body == b""
    assert response.headers["content-length"] == "4096"


def test_a_bogus_content_length_is_never_relayed_on_head(tmp_path, monkeypatch):
    """Only a sane numeric length survives; a garbage value must not reach the client.

    Starlette fills the now-absent header back in as ``0``, which is what this
    route has always answered - the point is that ``not-a-number`` is not
    forwarded verbatim (h11 would abort the response over it).
    """
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    _relay(monkeypatch, {"status": 200, "body_b64": "",
                         "headers": {"content-length": "not-a-number"}})

    response = _call(_share_request(method="HEAD"))

    assert response.headers["content-length"] == "0"
    assert response.body == b""


def test_the_declared_length_never_disagrees_with_the_body(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    _relay(monkeypatch, {
        "status": 200,
        "body_b64": base64.b64encode(b"12345").decode(),
        "headers": {"content-length": "999999"},
    })

    response = _call(_share_request())

    assert response.headers["content-length"] == "5"
    assert response.body == b"12345"


def test_no_content_statuses_carry_neither_body_nor_length(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    _relay(monkeypatch, {"status": 204, "body_b64": "",
                         "headers": {"content-length": "7"}})

    response = _call(_share_request())

    assert response.status_code == 204
    assert response.body == b""
    assert "content-length" not in response.headers


# ---------------------------------------------------------------------------
# (b) Refusals
# ---------------------------------------------------------------------------

def test_unknown_slug_shape_is_not_found(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    fake = _relay(monkeypatch, {"status": 200, "body_b64": "", "headers": {}})

    # No 16-hex credential, a bad character, and an empty slug.
    for bad in ("docs", "docs-NOTHEX0000000000", "docs-0123456789abcde", "../etc/passwd"):
        response = _call(_share_request(rest=f"{bad}/report.txt"))
        assert response.status_code == 404, bad
        assert response.body == b"not found\n"

    assert fake.calls == [], "a malformed slug must never reach the node"


def test_unknown_user_is_not_found(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    _relay(monkeypatch, {"status": 200, "body_b64": "", "headers": {}})

    response = _call(_share_request(user="ghost-node"))

    assert response.status_code == 404
    assert response.body == b"not found\n"


def test_an_offline_node_fails_fast_with_503(tmp_path, monkeypatch):
    """A share on a disconnected node must not hang on the tunnel retry loop."""
    _registry(tmp_path, monkeypatch)
    monkeypatch.setattr(gateway, "active_tunnels", {})
    fake = _relay(monkeypatch, {"status": 200, "body_b64": "", "headers": {}})

    response = _call(_share_request())

    assert response.status_code == 503
    assert b"node offline" in response.body
    assert fake.calls == [], "an offline node must not be dialled at all"


def test_write_methods_are_refused_by_the_handler(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    fake = _relay(monkeypatch, {"status": 200, "body_b64": "", "headers": {}})

    response = _call(_share_request(method="POST"))

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert fake.calls == []


def test_a_buggy_node_reply_becomes_502(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    _relay(monkeypatch, {"no_status": True})
    assert _call(_share_request()).status_code == 502

    _relay(monkeypatch, {"status": 100, "body_b64": "", "headers": {}})
    assert _call(_share_request()).status_code == 502, "1xx is not a final response"

    _relay(monkeypatch, {"status": "abc", "body_b64": "", "headers": {}})
    assert _call(_share_request()).status_code == 502


# ---------------------------------------------------------------------------
# Relay header allow-list
# ---------------------------------------------------------------------------

def test_only_allow_listed_headers_survive_the_relay(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    _relay(monkeypatch, {
        "status": 200,
        "body_b64": base64.b64encode(b"x").decode(),
        "headers": {
            "content-type": "text/html",
            "etag": '"abc"',
            "set-cookie": "session=stolen",
            "x-accel-redirect": "/etc/passwd",
            "location": "https://evil.example/steal",
        },
    })

    response = _call(_share_request())

    assert response.headers["content-type"] == "text/html"
    assert response.headers["etag"] == '"abc"'
    assert "set-cookie" not in response.headers
    assert "x-accel-redirect" not in response.headers
    assert "location" not in response.headers, "a redirect must stay inside the share namespace"


def test_a_relative_location_is_relayed_and_control_characters_are_stripped(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    _relay(monkeypatch, {
        "status": 302,
        "body_b64": "",
        "headers": {"location": "/node-one/docs-0123456789abcdef/index.html",
                    "content-type": "text/plain\r\nset-cookie: a=b"},
    })

    response = _call(_share_request())

    assert response.status_code == 302
    assert response.headers["location"].startswith("/node-one/")
    assert "\r" not in response.headers["content-type"]
    assert "\n" not in response.headers["content-type"]


# ---------------------------------------------------------------------------
# (b) The catch-all is last and shadows nothing
# ---------------------------------------------------------------------------

def _first_handler(path, method="GET", routes=None):
    """Which route does the real router pick for this path?

    Mirrors Starlette's own rule: the first FULL match wins, and a route that
    matches the path but not the method is only a PARTIAL match, so it never
    hides a later route that does accept the method.
    """
    scope = {"type": "http", "path": path, "raw_path": path.encode(),
             "root_path": "", "method": method, "headers": []}
    for route in (gateway.app.router.routes if routes is None else routes):
        match, child = route.matches(scope)
        if match is Match.FULL:
            return route, child
    return None, {}


# Every route the catch-all sits below, with the method that must reach it.
# The two-segment entries are the ones that make this check meaningful: a
# catch-all of the form /{user}/{rest:path} needs a second "/" in the path, so
# single-segment routes like /health can never be shadowed by it, while
# /.well-known/oauth-protected-resource is exactly the shape that can be.
#
# The expected endpoint is named, not captured as an object: another test calls
# importlib.reload(gateway), which rebinds gateway.health to a fresh function
# object, so a tuple holding the old object would compare unequal for reasons
# that have nothing to do with routing.
PROTECTED_ROUTES = [
    ("/", "GET", "health"),
    ("/health", "GET", "health"),
    ("/health-mesh", "GET", "health"),
    ("/sse", "POST", "mcp_unified_endpoint"),
    ("/mcp", "POST", "mcp_unified_endpoint"),
    ("/messages", "POST", "mcp_unified_endpoint"),
    ("/api/register", "POST", "api_register"),
    ("/.well-known/oauth-protected-resource", "GET", "oauth_discovery"),
    ("/.well-known/oauth-protected-resource/anything", "GET", "oauth_discovery"),
]


def test_the_catch_all_route_is_the_last_one():
    route = gateway.routes[-1]
    assert route.path == "/{user}/{rest:path}"
    assert route.endpoint is gateway.public_share_endpoint
    assert set(route.methods) == {"GET", "HEAD", "OPTIONS"}


def test_the_catch_all_shadows_no_route_above_it():
    for path, method, name in PROTECTED_ROUTES:
        route, _ = _first_handler(path, method)
        assert route is not None, f"{method} {path} matches nothing"
        assert route.endpoint.__name__ == name, (
            f"{method} {path} is shadowed by {route.path}")


def test_the_ordering_guard_is_not_vacuous():
    """Moving the catch-all up really does swallow a route above it.

    Without this, the assertion above could pass for the wrong reason: a
    single-segment path such as /health can never be matched by
    /{user}/{rest:path}, so /health alone would keep passing even if the
    catch-all were registered first.
    """
    drifted = [gateway.routes[-1]] + list(gateway.routes[:-1])
    shadowed = []
    for path, method, name in PROTECTED_ROUTES:
        route, _ = _first_handler(path, method, routes=drifted)
        if route is not None and route.endpoint.__name__ != name:
            shadowed.append(path)

    assert shadowed, "no protected route is order-sensitive: the guard proves nothing"
    assert "/.well-known/oauth-protected-resource" in shadowed


def test_the_catch_all_still_parses_its_two_path_params():
    route, child = _first_handler("/node-one/docs-0123456789abcdef/report.txt")
    assert route.path == "/{user}/{rest:path}"
    assert child["path_params"] == {"user": "node-one",
                                    "rest": "docs-0123456789abcdef/report.txt"}


def test_health_and_mcp_still_answer_through_the_app(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)

    status, _, body = _asgi("GET", "/health", b"?user=node-one")
    assert status == 200
    assert json.loads(body)["service"] == "remote_harness_mcp_gateway"

    # An unauthenticated POST /mcp must be answered by the MCP endpoint (401),
    # never by the share catch-all (which would answer 404 "not found").
    status, _, body = _asgi("POST", "/mcp")
    assert status == 401
    assert json.loads(body)["error"] == "unauthorized"


def test_a_share_is_reachable_through_the_real_app(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    _relay(monkeypatch, {
        "status": 200,
        "body_b64": base64.b64encode(b"served by the node").decode(),
        "headers": {"content-type": "text/plain; charset=utf-8"},
    })

    status, headers, body = _asgi("GET", f"/{SHARE}", b"?token=x")

    assert status == 200, "the catch-all route is not wired into the app"
    assert body == b"served by the node"
    assert headers["content-type"] == "text/plain; charset=utf-8"


def test_the_catch_all_still_relays_the_query_string_through_the_app(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    _node_online(monkeypatch)
    fake = _relay(monkeypatch, {"status": 200, "body_b64": "", "headers": {}})

    _asgi("GET", f"/{SHARE}", b"v=3")

    assert fake.calls[0][2]["query"] == "v=3"


# ---------------------------------------------------------------------------
# (b) system_info rendering: the device blocks must reach the client
# ---------------------------------------------------------------------------
# The gateway prints system_info as text rather than raw JSON, and its field list
# predates the device branch. That is a silent failure mode: the node reports a
# battery, a signal level and a camera count, the gateway drops them, and the model
# tells the user the node did not report them. These two tests pin the rendering
# instead of the transport, because the transport was already covered.

def _system_info_text(tmp_path, monkeypatch, result):
    _registry(tmp_path, monkeypatch)
    _relay(monkeypatch, result)
    response = asyncio.run(gateway.messages_endpoint(
        _post_request(f"/mcp?user={NODE}&token={TOKEN}",
                      {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "system_info", "arguments": {}}})
    ))
    payload = json.loads(response.body)["result"]
    return payload["content"][0]["text"], payload["isError"]


def test_system_info_renders_the_device_blocks_a_phone_reports(tmp_path, monkeypatch):
    text, is_error = _system_info_text(tmp_path, monkeypatch, {
        "hostname": "phone", "command_shell": "bash", "scenario": "termux",
        "device": {"available": True, "class": "phone", "manufacturer": "OPPO",
                   "model": "PHY110", "android_release": "16"},
        "battery": {"available": True, "percent": 42, "status": "discharging",
                    "plugged": "unplugged", "temperature_c": 31.5},
        "network": {"available": True, "signal": "wifi Warmen5g -52 dBm",
                    "connected_kind": "wifi", "interface_count": 2,
                    "wifi": {"ssid": "Warmen5g", "rssi_dbm": -52},
                    "cellular": {"operator": "Magti", "network_type": "LTE",
                                 "signal_dbm": -101, "level": 2}},
        "locale": {"available": True, "language": "ru", "region": "RU"},
        "time": {"available": True, "iso_local": "2026-10-09T01:00:00+04:00",
                 "utc_offset": "+04:00", "timezone": "Asia/Tbilisi"},
        "hardware": {"available": True,
                     "cpu": {"model": "Snapdragon", "cores": 8, "temp_c": 34.4},
                     "memory": {"total_mb": 15204.0, "used_mb": 9000.0},
                     "thermal": [{"name": "cpuss-0", "value": 33.6, "unit": "C"}]},
        "storage": {"available": True, "root": "/", "free_gb": 41.0, "shared": "absent",
                    "shared_fix": "run `termux-setup-storage`"},
        "cameras": {"available": True, "count": 2,
                    "cameras": [{"facing": "back"}, {"facing": "front"}]},
        "microphones": {"available": False,
                        "reason": "Android does not expose capture-device enumeration"},
        "sensors": {"available": True, "count": 3, "sampling": "names",
                    "sensors": ["acceleration"]},
    })

    assert is_error is False
    for expected in ("[device] phone OPPO PHY110 16",
                     "battery: percent=42, status=discharging, plugged=unplugged",
                     "signal: wifi Warmen5g -52 dBm",
                     "cellular: operator=Magti, network_type=LTE, signal_dbm=-101, level=2",
                     "locale: language=ru, region=RU",
                     "timezone=Asia/Tbilisi",
                     "cpu: model=Snapdragon, cores=8, temp_c=34.4",
                     "thermal: cpuss-0=33.6C",
                     "storage: root=/, free_gb=41.0, shared=absent",
                     "cameras: count=2 (back, front)",
                     "sensors: count=3, sampling=names"):
        assert expected in text, "the rendered report dropped %r" % expected
    # An unavailable block says so, with its reason: "not exposed here" and "0 %"
    # must never look the same to a model.
    assert "microphones: not available - Android does not expose capture-device enumeration" in text


def test_system_info_renders_one_cause_for_a_silent_termux_api(tmp_path, monkeypatch):
    text, _ = _system_info_text(tmp_path, monkeypatch, {
        "hostname": "phone",
        "device_hint": ("2 sections timed out (battery, network). On an Android node that is "
                        "usually one cause: the Termux:API app is not installed"),
        "battery": {"available": False, "reason": "termux-battery-status timed out after 4.0s",
                    "fix": "open the Termux:API app once"},
        "capabilities": {"available": True,
                         "termux_api": {"installed": True, "missing": ["termux-sensor"]},
                         "app_reachable": False, "app_fix": "open the Termux:API app once",
                         "permissions": {"camera": "denied", "microphone": "unknown"}},
    })

    assert "sections timed out" in text, "the single root cause must be visible"
    assert "battery: not available - termux-battery-status timed out after 4.0s" in text
    assert "(fix: open the Termux:API app once)" in text
    assert "termux-api: installed=True" in text
    assert "termux-api missing: termux-sensor" in text
    assert "termux-api app: not answering" in text
    assert "permissions denied: camera" in text
