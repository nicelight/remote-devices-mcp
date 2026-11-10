import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import socket
import time
import uuid
from urllib.parse import urlparse, urlencode
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse, RedirectResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
import uvicorn
from gateway_identity import OwnerAuth, page, escape, fields, form_fields

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gateway")

# uvicorn's access log writes the whole request target, query string included, so
# every MCP call used to put the node credential on disk. Redacting at the record
# level keeps the rest of the line (status, client, latency) and removes the
# secret, whether it is a link key or a pre-0.4.6 device token.
_TOKEN_IN_QUERY = re.compile(r"((?:token|code|state)=)[^&\s\"']+")


class CredentialRedactionFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_IN_QUERY.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                for a in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: (_TOKEN_IN_QUERY.sub(r"\1<redacted>", v) if isinstance(v, str) else v)
                for k, v in record.args.items()
            }
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_QUERY.sub(r"\1<redacted>", record.msg)
        return True


for _log_name in ("uvicorn.access", "uvicorn.error", "gateway"):
    logging.getLogger(_log_name).addFilter(CredentialRedactionFilter())

REGISTRY_PATH = os.environ.get("MESH_REGISTRY_PATH", "/etc/remote-harness-mcp/registry.json")
#: Registry location from before the rename. A gateway deployed earlier keeps its
#: node registrations there, so an existing install is read and written in place
#: instead of starting from an empty registry (gateway.py is deployed standalone,
#: so this stays a local fallback rather than importing core.paths).
LEGACY_REGISTRY_PATH = "/etc/antigravity-mesh/registry.json"
if not os.environ.get("MESH_REGISTRY_PATH") and not os.path.exists(REGISTRY_PATH) and os.path.exists(LEGACY_REGISTRY_PATH):
    REGISTRY_PATH = LEGACY_REGISTRY_PATH
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
active_tunnels = {}  # user -> {"ws": WebSocket, "pending": {req_id: Future}}
# user -> list of asyncio.Queue, OLDEST stream first. This was a set, which has
# no order: the per-user cap below has to evict the OLDEST stream, and every
# other reader (broadcast, /health, the watchdog) only iterates or measures the
# container, so the type change stays invisible outside this module.
active_sse_subscribers = {}
active_sse_sessions = {}  # per-connection stream_key -> asyncio.Queue
# client-visible session id -> queues opened under it. active_sse_sessions is
# keyed per CONNECTION on purpose (one DELETE must not close a stream another
# connection is still reading), so this second index is what lets a
# re-initialize close exactly the streams of the session it replaces.
active_sse_session_queues = {}
active_sessions = {}  # session_id -> user, issued once per initialize
latest_session_by_user = {}  # user -> most recently issued session id

# A client that lost its session id opens a new stream every turn and never
# closes the old one (observed: 9 abandoned streams for one user while no tunnel
# was connected). Nothing above this many streams for one user is kept: the
# oldest is closed, because the newest is the one the client just opened.
# MESH_MAX_SSE_PER_USER raises the cap for a deployment whose clients legitimately
# keep several streams open; the default stays deliberately small.
try:
    MAX_SSE_PER_USER = max(1, int(os.environ.get("MESH_MAX_SSE_PER_USER") or "2"))
except (TypeError, ValueError):
    MAX_SSE_PER_USER = 2

# Tunnel lifecycle history for /health. "node_online: false" alone does not say
# WHEN the node was last seen, which is the first question after an unexpected
# reboot; the ring buffer is bounded so a long-lived gateway cannot grow.
last_tunnel_seen = None  # ISO-8601 timestamp of the last successful connection
TUNNEL_EVENTS_MAX = 10
tunnel_events = deque(maxlen=TUNNEL_EVENTS_MAX)  # newest last


def record_tunnel_event(event: str, user: str, reason: str = "") -> None:
    """Record one tunnel connect/disconnect and stamp the last connect time."""
    global last_tunnel_seen
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if event == "connect":
        last_tunnel_seen = ts
    tunnel_events.append({"ts": ts, "event": event, "user": user, "reason": reason})


def forget_sse_queue(user: str, queue) -> None:
    """Unregister a queue from the per-user list and from the session index.

    Shared by the stream's own cleanup and the DELETE teardown so both keep the
    two containers in step; a stream that is still registered after it stopped
    being readable is exactly the leak ``/health`` is meant to expose.
    """
    subscribers = active_sse_subscribers.get(user)
    if subscribers is not None and queue in subscribers:
        subscribers.remove(queue)
    for session_id, queues in list(active_sse_session_queues.items()):
        if queue in queues:
            queues.remove(queue)
            if not queues:
                active_sse_session_queues.pop(session_id, None)


def close_sse_streams_for_session(session_id: str) -> int:
    """Close every SSE stream opened under a session id that is now superseded.

    ``None`` is the sentinel the stream loop treats as "close this stream", the
    same one the DELETE teardown and the per-user cap use; the stream's own
    ``finally`` does the unregistering, so nothing is removed here. Returns how
    many streams were asked to close.
    """
    queues = active_sse_session_queues.pop(session_id, [])
    for queue in queues:
        try:
            queue.put_nowait(None)
        except Exception:
            pass
    return len(queues)

# Negotiated protocol version per user. The value must be identical in the
# initialize result AND in the mcp-protocol-version header of every response: a
# client that asked for 2025-11-25 and reads 2024-11-05 back in the header can
# reject the answer as belonging to a different protocol revision.
active_protocol_versions = {}
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

# Legacy SSE transport experiment. The classic MCP SSE transport answers a POST
# with 202 Accepted and an EMPTY body, and delivers the result only on the stream.
# Set MESH_LEGACY_SSE=1 to switch a client that holds an open stream onto that
# contract; leave it unset for the current "answer in the body too" behaviour.
LEGACY_SSE = os.environ.get("MESH_LEGACY_SSE", "").strip() in ("1", "true", "yes")

# Answering a POST in the HTTP body AND on the caller's SSE stream hands a strict
# streamable-HTTP client the SAME response id twice, and it rejects the session.
# The body alone is what /sse and /mcp clients read - the `initialize` response,
# which is answered before any stream exists, is proof that they do. Only the
# legacy /messages transport needs the stream, and LEGACY_SSE (202, no body)
# deliberately needs it too.
# Set MESH_BROADCAST_HTTP_RESPONSES=1 to duplicate on purpose while debugging.
BROADCAST_HTTP_RESPONSES = os.environ.get(
    "MESH_BROADCAST_HTTP_RESPONSES", "0"
).strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# ONE SHARED DOMAIN FOR EVERY NODE (canonical URL contract)
# ---------------------------------------------------------------------------
# Every node is reached through the same public domain and is selected by the
# ?user= query parameter. Per-device subdomains are deliberately NOT issued:
#   * each extra hostname needs its own DNS record and its own SAN in the TLS
#     certificate, so a device whose name is missing from the certificate fails
#     the handshake and the client reports an opaque "cannot connect to host";
#   * the node name stops being a security-relevant routing key, which keeps the
#     installer's "name already taken -> node-2" behaviour harmless.
# Canonical endpoints (see README.md "Quick start — 2 minutes"):
#   SSE      https://<shared-domain>/sse?user=<node>&token=<token>
#   HTTP     https://<shared-domain>/mcp?user=<node>&token=<token>
#   Tunnel   wss://<shared-domain>/ws/tunnel?user=<node>&token=<token>
# Override the shared domain with MESH_PUBLIC_URL (or its AGY_PUBLIC_BASE_URL
# alias), or with the domain file - core/domain.py owns that order and holds the
# single default, so the domain is configured in exactly one place.
try:
    from core import domain as _domain
except Exception:  # pragma: no cover - gateway.py installed without core/ beside it
    _domain = None

if _domain is not None:
    PUBLIC_BASE_URL = _domain.public_base_url()
else:
    # gateway.py is sometimes installed on its own (for example
    # /opt/remote-harness-mcp/gateway.py with no core/ next to it). Shipping
    # core/domain.py alongside it is part of the deployment; this branch only keeps
    # the service starting when that was forgotten, and it refuses to invent a
    # domain of its own - the project default lives in core/domain.py alone.
    PUBLIC_BASE_URL = (
        os.environ.get("MESH_PUBLIC_URL") or os.environ.get("AGY_PUBLIC_BASE_URL") or ""
    ).strip().rstrip("/")
    if not PUBLIC_BASE_URL:
        logger.error(
            "core/domain.py is not importable and neither MESH_PUBLIC_URL nor "
            "AGY_PUBLIC_BASE_URL is set: the public domain is unknown, so URLs handed "
            "to clients will be relative. Install core/domain.py next to gateway.py, or "
            "set the domain in the domain file / environment."
        )
PUBLIC_HOST = urlparse(PUBLIC_BASE_URL).netloc or PUBLIC_BASE_URL

# Legacy per-device subdomain resolution. Kept only so URLs handed out before the
# shared-domain contract keep working; never advertised, and it logs a warning.
LEGACY_SUBDOMAIN_ACCESS = os.environ.get("MESH_LEGACY_SUBDOMAIN", "1").strip().lower() in (
    "1", "true", "yes", "on"
)


def public_url(path: str, user: str, token: str) -> str:
    """Canonical public URL for one node on the shared domain."""
    return f"{PUBLIC_BASE_URL}{path}?user={user}&token={token}"


def issue_session_id(user: str) -> str:
    """Issue a unique ``Mcp-Session-Id`` for one initialize.

    The spec requires a session id to be unique per session, and Google's
    frontends open several parallel connections per turn (a POST for the call,
    a GET for the stream, a DELETE to tear the session down, often from
    different egress IPs). Returning the user name as the session id -- as this
    gateway used to -- gave every one of those connections the same identity, so
    a DELETE sent by one of them looked like it had terminated the session that
    another connection's in-flight tool call belonged to, and the client dropped
    the answer it had already received. Clients that never echo the header keep
    working: they still fall back to the user name.
    """
    session_id = uuid.uuid4().hex
    active_sessions[session_id] = user
    latest_session_by_user[user] = session_id
    return session_id


def announce_endpoint_for_path(path: str) -> bool:
    """Whether a GET stream on this path may emit the classic ``event: endpoint``.

    The classic SSE transport announces the POST target (/messages). Streamable
    HTTP (GET /mcp) must carry JSON-RPC frames only, so the frame is suppressed
    there - a strict client otherwise drops the stream it reads results from.
    """
    return path != "/mcp"


def should_broadcast_response(path: str) -> bool:
    """Whether a POST response is also queued on the caller's SSE stream.

    Duplicating the answer (body + stream) hands a strict streamable-HTTP client
    the same response id twice; it rejects the session and the model never sees
    the result. Only the legacy transports need the stream.
    """
    return path == "/messages" or LEGACY_SSE or BROADCAST_HTTP_RESPONSES


def resolve_session_id(request: Request, user: str) -> str:
    """The session id this connection must see.

    Prefer the id the client sends back. Otherwise use the session this user most
    recently opened: a client that holds a session but omits the header must not
    be told a different id, or it believes it holds two sessions and aborts one
    (that mismatch was the cause of the original initialize/DELETE churn). The
    bare user name stays as the last resort for header-less legacy clients.
    """
    return (
        request.headers.get("mcp-session-id")
        or latest_session_by_user.get(user)
        or user
    )


def protocol_version_for(user: str) -> str:
    return active_protocol_versions.get(user, DEFAULT_PROTOCOL_VERSION)

def broadcast_sse(user: str, message: dict) -> int:
    """Queue a JSON-RPC frame on every open stream of this user.

    Returns how many streams received it. The count is logged because it is the
    only way to tell a client that reads results from the stream (Gemini Spark)
    whether the answer actually had a live stream to land on: a response
    broadcast to 0 streams reaches nobody on that channel.
    """
    data = json.dumps(message, ensure_ascii=False)
    queues = list(active_sse_subscribers.get(user, ()))
    for q in queues:
        try:
            q.put_nowait(data)
        except Exception:
            pass
    label = message.get("method") or f"id={message.get('id')}"
    logger.info(f"broadcast [{label}] for '{user}' to {len(queues)} open stream(s)")
    return len(queues)

RESERVED_NAMES = {
    "admin", "administrator", "root", "api", "api-ag", "api-agy",
    "drive", "derp", "mcp-tg", "chamber", "proxy-api", "tg-bot-api",
    "anonymous", "gateway", "mesh", "mail", "vpn", "test", "null",
    "undefined", "localhost", "local", "internal", "public", "ws",
    "sse", "mcp", "messages", "health", "health-mesh", "core", "skills",
    "install", "static", "assets"
}

def load_registry() -> dict:
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            # Do not turn a damaged registry into an empty registry and overwrite it.
            raise RuntimeError("Cannot read device registry; refusing to modify registrations") from e
    return {}

def save_registry(data: dict):
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    # Replace atomically: a crash must not truncate all device registrations.
    import tempfile
    fd, staged = tempfile.mkstemp(prefix=".registry-", dir=os.path.dirname(REGISTRY_PATH))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(staged, REGISTRY_PATH)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)

def tokens_equal(a: str, b: str) -> bool:
    """Constant-time token comparison that never raises on non-ASCII input.

    ``hmac.compare_digest`` rejects str arguments containing non-ASCII
    characters (TypeError -> HTTP 500). Comparing UTF-8 bytes with
    ``surrogateescape`` keeps the comparison constant-time and total.
    """
    try:
        return hmac.compare_digest(
            str(a).encode("utf-8", "surrogateescape"),
            str(b).encode("utf-8", "surrogateescape"),
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# NODE CREDENTIALS: a device token and a link key, with different jobs
# ---------------------------------------------------------------------------
# A node used to hold ONE secret and put it in every URL. That secret opens the
# tunnel, i.e. it is the machine's own identity, and a URL is the worst place to
# keep it: it lands in nginx access logs, browser history, the Referer header,
# the connector settings of a third-party client and in the clipboard that the
# installer writes. Two credentials fix that split:
#
#   * device token - opens the tunnel, proves the node's identity, travels only
#     in an Authorization header (or x-mesh-token) between the agent and here.
#   * link key     - the credential that goes into a URL such as
#     /sse?user=<node>&token=lk_<32 hex>. It can invoke tools for that node but
#     it CANNOT open a tunnel, so a leaked link cannot impersonate an agent; and
#     it is rotatable, so a leaked link is fixed by pressing "rotate" instead of
#     reinstalling the node.
#
# The device token is stored as sha256 only. The link key is stored as-is: the
# owner has to be able to read it again (it is the thing pasted into Gemini) and
# it is already public by construction. Registry entries written by gateways
# before 0.4.6 keep a plaintext "token"; that one is still honoured in
# constant time so an upgrade does not lock existing nodes out, and the node is
# upgraded to a hash + link key the next time it proves possession with it.
LINK_KEY_PREFIX = "lk_"


def hash_secret(value: str) -> str:
    """Stable digest for a credential; the plaintext is never stored."""
    return hashlib.sha256(str(value).encode("utf-8", "surrogateescape")).hexdigest()


def new_device_token() -> str:
    return secrets.token_hex(16)


def new_link_key() -> str:
    return LINK_KEY_PREFIX + secrets.token_hex(16)


def is_link_key(value: str) -> bool:
    return bool(value) and str(value).startswith(LINK_KEY_PREFIX)


def token_matches_device(entry: dict, token: str) -> bool:
    """True when *token* is this node's device token (hashed or legacy plaintext)."""
    if not token:
        return False
    stored = str(entry.get("token_sha256") or "")
    if stored:
        return tokens_equal(hash_secret(token), stored)
    legacy = entry.get("token") or ""
    return bool(legacy) and tokens_equal(token, legacy)


def token_matches_link(entry: dict, token: str) -> bool:
    """True when *token* is this node's link key (the URL-safe credential)."""
    stored = entry.get("link_key") or ""
    return bool(stored) and bool(token) and tokens_equal(token, stored)


def request_credential(request: Request) -> tuple[str, str]:
    """Return ``(credential, source)`` with source in header/query/none.

    The header is checked first: a client that can set an Authorization header
    never has to put a secret in a URL, so its token stays out of every log.
    """
    header = (request.headers.get("x-mesh-token") or "").strip()
    if not header:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            header = auth[7:].strip()
    if header:
        return header, "header"
    query = (request.query_params.get("token") or "").strip()
    if query:
        return query, "query"
    return "", "none"


def get_request_token(request: Request) -> str:
    return request_credential(request)[0]


def credential_denial(entry: dict, source: str) -> str:
    if source == "query":
        if entry.get("link_key"):
            return (
                "The device token is not accepted in a URL: use this node's link key "
                "('lk_...') or send the device token in an Authorization: Bearer header."
            )
        return (
            "Invalid or missing link key. Use the 'lk_...' value from registration, "
            "or send the device token in an Authorization: Bearer header."
        )
    return "Invalid or missing credential. Send 'Authorization: Bearer <token>'."


def get_target_user(request: Request) -> str:
    """Resolve which node a request addresses.

    Canonical form: ``<shared-domain>/...?user=<node>&token=<token>``. The shared
    domain only selects the gateway, so ``?user=`` is authoritative. A per-device
    subdomain is accepted as a legacy fallback (MESH_LEGACY_SUBDOMAIN=0 disables
    it) and logs a warning telling the caller to switch to the canonical URL.
    """
    user = (request.query_params.get("user") or "").strip()
    if user:
        return user.lower()

    if LEGACY_SUBDOMAIN_ACCESS:
        host_part = request.headers.get("host", "").split(":")[0].lower()
        base = PUBLIC_HOST.lower()
        if host_part.endswith(f".{base}") and host_part != base:
            sub = host_part[:-len(base)-1]
            parts = sub.split(".")
            legacy_user = parts[-1].lower()
            logger.warning(
                "Legacy per-device subdomain '%s' used instead of the shared domain; "
                "switch to %s/sse?user=%s&token=...", host_part, PUBLIC_BASE_URL, legacy_user,
            )
            return legacy_user

    return "anonymous"

def token_matches_next(entry: dict, token: str) -> bool:
    """True when *token* is the new device token a rotation handed out.

    A rotated token has to work BEFORE the node has restarted, otherwise the
    rotation cannot be completed: the node would write a token the gateway
    rejects and lose the tunnel. It therefore lives beside the current one until
    the node proves it can use it (see ``promote_device_token``), which is the
    moment the old token stops being accepted.
    """
    stored = str(entry.get("token_next_sha256") or "")
    return bool(stored) and bool(token) and tokens_equal(hash_secret(token), stored)


def promote_device_token(user: str, registry: dict, entry: dict) -> bool:
    """Make the pending token current and kill every older credential.

    Called when a tunnel authenticates with the pending token: the node has just
    demonstrated that its configuration holds the new secret, so the old token -
    the one that may have leaked - is retired here, and not a moment earlier.
    """
    pending = str(entry.get("token_next_sha256") or "")
    if not pending:
        return False
    entry["token_sha256"] = pending
    entry.pop("token_next_sha256", None)
    entry.pop("token_rotated_at", None)
    # A pre-0.4.6 plaintext token and the legacy-query allowance must not
    # survive a rotation: that is the whole point of rotating.
    entry.pop("token", None)
    entry.pop("allow_legacy_query", None)
    save_registry(registry)
    logger.info("Device token rotated for '%s'; the previous token is now invalid", user)
    return True


def verify_token(request: Request) -> tuple[bool, str, str]:
    user = get_target_user(request)
    if user == "anonymous":
        return False, (
            "Node identity is missing. Use the shared-domain URL form "
            f"{PUBLIC_BASE_URL}/sse?user=<node-name>&token=<link-key>."
        ), ""
    registry = load_registry()
    entry = registry.get(user)
    if not entry:
        return False, f"User '{user}' is not registered.", user
    token, source = request_credential(request)
    if not token:
        return False, credential_denial(entry, source), user
    if source == "header":
        ok = (token_matches_device(entry, token)
              or token_matches_next(entry, token)
              or token_matches_link(entry, token))
    else:
        # A URL is not a private channel: it is logged by the proxy, kept in
        # browser history, forwarded in Referer and stored in a third-party
        # client's connector settings. Only the rotatable link key may travel in
        # one. A legacy entry (registered before link keys existed) keeps working
        # so an upgrade does not disconnect an already-configured node, but the
        # warning says how to move it off the old secret.
        ok = token_matches_link(entry, token)
        if not ok and (entry.get("allow_legacy_query") or not entry.get("link_key")) \
                and token_matches_device(entry, token):
            ok = True
            logger.warning(
                "Legacy ?token= credential accepted for '%s' (pre-0.4.6 node). "
                "Re-register it or call /v1/devices/%s/rotate to move it to a "
                "rotatable link key.", user, user,
            )
    if not ok:
        return False, credential_denial(entry, source), user
    return True, "ok", user

def get_skill(user: str, host: str) -> str:
    return f"""# 🌐 Remote Harness MCP — Node Orchestrator ({user})

Target Node: {user} (shared gateway {PUBLIC_HOST})
Endpoint: {PUBLIC_BASE_URL}/sse?user={user}&token=<your_token>
Mode: Secure Cloud Gateway + Reverse Tunnel (one shared domain for every node)

MANDATORY GROUNDING & EXECUTION DIRECTIVES:
1. You are connected directly to the user's execution node '{user}' via Remote Harness MCP tools.
2. STRICT ZERO-HALLUCINATION POLICY:
   - You DO NOT know local directory structure, file names, or environment state in advance.
   - NEVER invent file paths, guess folder contents, or claim a directory is empty without checking.
   - You MUST call `list_dir` or `bash_exec` (e.g. `pwd`, `ls -la`) to inspect the real filesystem first.
3. DIRECT EXECUTION:
   - Always execute operations directly via MCP tools (`list_dir`, `read_file`, `bash_exec`, `system_vitals`).
   - Never ask the user to run shell commands manually.
4. Always verify return codes and stdout/stderr before reporting results.
"""

async def call_remote_tool(user: str, name: str, args: dict) -> dict:
    tunnel = active_tunnels.get(user)
    if not tunnel or not tunnel.get("ws"):
        # The agent reconnects within ~2s of a dropped link (VPN flap, network
        # change, service restart), so give it a moment instead of reporting the
        # node as offline immediately. A missing agent is then a rare answer.
        #
        # A host reboot is NOT a flap, though: the agent stays away for minutes,
        # while the client's own frontend gives a tool call roughly 30 s. Waiting
        # 8 s only made the eventual failure arrive later. 4 x 0.5 s covers a real
        # reconnect and keeps the whole tools/call path within ~5 s.
        for _ in range(4):                       # up to ~2 seconds
            await asyncio.sleep(0.5)
            tunnel = active_tunnels.get(user)
            if tunnel and tunnel.get("ws"):
                break
    if not tunnel or not tunnel.get("ws"):
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": (f"[Mesh Gateway] Node '{user}' has no active tunnel: the mesh agent on the "
                       f"host is not connected to the gateway. Start/restart the mesh agent there, "
                       f"then retry; this is a transport problem, not a missing file or permission.")
        }
    req_id = str(uuid.uuid4())
    fut = asyncio.get_running_loop().create_future()
    tunnel["pending"][req_id] = fut
    payload = {
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": args}
    }
    try:
        await tunnel["ws"].send_text(json.dumps(payload))
        # 28s timeout: Agent runs commands with 25s timeout, so 28s gives 3s margin and returns cleanly
        # BEFORE Google's 30s frontend timeout, completely preventing Gemini Error 1076
        res_msg = await asyncio.wait_for(fut, timeout=28.0)
        return res_msg.get("result", res_msg)
    except asyncio.TimeoutError:
        tunnel["pending"].pop(req_id, None)
        return {"exit_code": 124, "error": f"Command timed out after 25s on node '{user}'"}
    except Exception as e:
        tunnel["pending"].pop(req_id, None)
        # Self-heal: a dropped tunnel (network flap, agent restart) is transient.
        # Wait for the agent to come back and replay the call once instead of
        # failing the client's request.
        logger.warning(f"Tunnel call to '{user}' failed ({e}); waiting for reconnect and retrying once")
        # A lost response does not prove a write failed. Never replay mutations.
        if not TOOL_ANNOTATIONS.get(name, {}).get("readOnlyHint", False):
            return {"exit_code": 1, "error": "Tunnel disconnected; execution outcome unknown. Inspect device state before retrying."}
        for _ in range(4):
            await asyncio.sleep(0.5)
            fresh = active_tunnels.get(user)
            if fresh and fresh.get("ws") and fresh.get("ws") is not tunnel.get("ws"):
                try:
                    retry_id = str(uuid.uuid4())
                    retry_fut = asyncio.get_running_loop().create_future()
                    fresh["pending"][retry_id] = retry_fut
                    await fresh["ws"].send_text(json.dumps({"id": retry_id, "method": "tools/call",
                                                            "params": {"name": name, "arguments": args}}))
                    res_msg = await asyncio.wait_for(retry_fut, timeout=20.0)
                    logger.info(f"Retry after reconnect succeeded for '{user}'")
                    return res_msg.get("result", res_msg)
                except Exception as retry_exc:
                    fresh["pending"].pop(retry_id, None)
                    logger.warning(f"Retry after reconnect failed for '{user}': {retry_exc}")
                    break
        return {"exit_code": 1, "error": str(e)}

def remote_tool_error(res):
    """Return a human-readable error string if a remote tool response is a failure, else None.

    Covers three cases:
      * the node returned {"error": "..."} (tool-level failure);
      * the node returned {"ok": false, ...};
      * the gateway itself could not reach the node / the tunnel broke, which yields
        a bare {"exit_code": !=0, "stdout", "stderr"} with no tool payload.
    A legitimate job_output payload always carries a "status" key, so it is never
    mistaken for a transport failure even when the job's exit_code != 0.
    """
    if not isinstance(res, dict):
        return "invalid response from node"
    if res.get("error"):
        return str(res.get("error"))
    if res.get("ok") is False:
        return str(res.get("message") or res.get("stderr") or "operation failed (ok=false)")
    if res.get("exit_code") not in (None, 0) and set(res.keys()) <= {"exit_code", "stdout", "stderr"}:
        return str(res.get("stderr") or res.get("stdout") or f"remote call failed with exit code {res.get('exit_code')}")
    return None


async def health(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Allow": "GET, HEAD, OPTIONS", "Access-Control-Allow-Origin": "*"})
    if os.environ.get("MESH_PRIVATE_HEALTH") == "1":
        authorised, _, _ = verify_token(request)
        if not authorised and not admin_authorised(request):
            return JSONResponse({"status": "healthy"})
    user = get_target_user(request)
    is_online = (user in active_tunnels) if user != "anonymous" else False
    return JSONResponse({
        "status": "healthy",
        "service": "remote_harness_mcp_gateway",
        "host": PUBLIC_HOST,
        "instance_id": INSTANCE_ID,
        "target_user": user,
        "node_online": is_online,
        "active_tunnels_count": len(active_tunnels),
        # Diagnostics: an abandoned SSE stream keeps a queue alive, and a client
        # with a connection cap would then be unable to open a new one.
        # `open_sse_streams` is the total, `streams_per_user` the same count
        # broken down per node - both unchanged in meaning, now fed by the
        # insertion-ordered per-user container.
        "open_sse_streams": sum(len(q) for q in active_sse_subscribers.values()),
        "streams_per_user": {u: len(q) for u, q in active_sse_subscribers.items()},
        # Tunnel lifecycle. `node_online: false` does not say when the node was
        # last seen, which is what tells a reboot apart from a network flap;
        # `tunnel_events` keeps the last 10 connect/disconnect events and why.
        "last_tunnel_seen": last_tunnel_seen,
        "tunnel_events": list(tunnel_events),
    }, headers={"Access-Control-Allow-Origin": "*"})

def node_mcp_url(user: str, link_key: str) -> str:
    """Public MCP endpoint for one node: the link key, never the device token."""
    base = f"{PUBLIC_BASE_URL}/sse?user={user}"
    return f"{base}&token={link_key}" if link_key else base


def unique_node_name(registry: dict, wanted: str) -> str:
    suffix = 2
    cand = f"{wanted}-{suffix}"
    while cand in registry or cand in RESERVED_NAMES:
        suffix += 1
        cand = f"{wanted}-{suffix}"
    return cand


def new_node_entry(mac: str, os_name: str, arch: str, now_iso: str, token: str) -> dict:
    """A fresh registration: hashed device token, plaintext link key, metadata.

    ``mac_address`` is kept as an inventory detail only. It is NOT an identity: a
    MAC is visible to the whole LAN, is trivially spoofed, and on its own used to
    be enough to receive another machine's token from /api/register.
    """
    return {
        "token_sha256": hash_secret(token),
        "link_key": new_link_key(),
        "created_at": now_iso,
        "last_seen": now_iso,
        "mode": "tunnel",
        "status": "active",
        "mac_address": mac,
        "os": os_name,
        "arch": arch,
    }


REGISTER_MODE = os.environ.get("MESH_REGISTER_MODE", "open").strip().lower()
ENROLL_SECRET = os.environ.get("MESH_ENROLL_SECRET", "").strip()


def enrolment_refusal(request: Request) -> str:
    """Why this request may not enrol a node, or "" when it may.

    ``MESH_REGISTER_MODE=pair`` closes self-registration entirely and leaves the
    pairing endpoints as the only way in. ``MESH_ENROLL_SECRET`` keeps
    self-registration but requires a shared secret the installer was given.
    """
    if REGISTER_MODE == "pair" or owner_auth.configured:
        return (
            "Self-registration is disabled on this gateway (MESH_REGISTER_MODE=pair). "
            "Start a pairing with POST /v1/devices/pair and approve the code."
        )
    if ENROLL_SECRET:
        supplied = request.headers.get("x-enroll-secret", "")
        if not tokens_equal(supplied, ENROLL_SECRET):
            return (
                "This gateway requires an enrollment secret. Re-run the installer "
                "with --enroll-secret, or use pairing."
            )
    return ""


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


_rate_windows: dict[str, list[float]] = {}


def rate_limited(bucket: str, key: str, limit: int, window: float = 60.0) -> bool:
    """False when the caller is still under *limit* requests per *window*.

    In-memory on purpose: registration is a control-plane concern and the paid
    deployment delegates it, but the public gateway must not be a free enrolment
    machine for a botnet either. One process, one bucket map.
    """
    now = time.time()
    seen = [t for t in _rate_windows.get(f"{bucket}:{key}", []) if now - t < window]
    if len(seen) >= limit:
        _rate_windows[f"{bucket}:{key}"] = seen
        return True
    seen.append(now)
    _rate_windows[f"{bucket}:{key}"] = seen
    return False


async def api_register(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Allow": "POST, OPTIONS", "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"})
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    if rate_limited("register", client_ip(request), limit=10):
        return JSONResponse(
            {"error": "rate_limited", "message": "Too many registrations from this address."},
            status_code=429,
        )
    refusal = enrolment_refusal(request)
    if refusal:
        return JSONResponse({"error": "registration_disabled", "message": refusal}, status_code=410)

    raw_user = data.get("username", "").strip()
    clean_user = re.sub(r"[^a-z0-9_-]", "", raw_user.lower())
    if not clean_user or len(clean_user) < 2:
        return JSONResponse({"error": "Invalid username (min 2 alphanumeric chars)"}, status_code=400)

    if clean_user in RESERVED_NAMES:
        return JSONResponse({
            "error": "reserved_username",
            "message": f"Username '{clean_user}' is reserved for system services."
        }, status_code=400)

    raw_mac = data.get("mac_address", "").strip()
    clean_mac = ""
    if raw_mac:
        mac_hex = re.sub(r"[^0-9a-fA-F]", "", raw_mac).lower()
        if len(mac_hex) == 12:
            clean_mac = ":".join(mac_hex[i:i+2] for i in range(0, 12, 2))
        else:
            clean_mac = raw_mac.lower()

    clean_os = str(data.get("os", "")).strip()[:100]
    clean_arch = str(data.get("arch", "")).strip()[:40]
    registry = load_registry()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # The device token may arrive as an Authorization header (preferred) or in
    # the JSON body (installers that predate the header). It is never read from
    # the query string here: a registration URL is not a place for a secret.
    supplied, _source = request_credential(request)
    supplied = supplied or str(data.get("token") or "").strip()
    auto_suffix = data.get("auto_suffix", False)

    entry = registry.get(clean_user)
    created = False

    if entry is not None:
        # A name is not an identity. Re-registering under an existing name is
        # only allowed with that node's device token; otherwise the caller gets
        # a conflict or, when it asked for one, a fresh unique name. This is the
        # branch that replaced reuse-by-MAC: proving possession of the token is
        # the only way to inherit an existing registration.
        if not token_matches_device(entry, supplied):
            if not auto_suffix:
                return JSONResponse({
                    "error": "conflict",
                    "message": (
                        f"Node '{clean_user}' already exists and this request did not "
                        "prove possession of its device token. Send it in an "
                        "Authorization: Bearer header, or ask for auto_suffix."
                    ),
                    "suggested_username": unique_node_name(registry, clean_user),
                }, status_code=409)
            clean_user = unique_node_name(registry, clean_user)
            entry = None

    if entry is None:
        token_plain = new_device_token()
        entry = new_node_entry(clean_mac, clean_os, clean_arch, now_iso, token_plain)
        registry[clean_user] = entry
        created = True
    else:
        token_plain = supplied          # echo back the token the caller proved
        if clean_mac:
            entry["mac_address"] = clean_mac
        if clean_os:
            entry["os"] = clean_os
        if clean_arch:
            entry["arch"] = clean_arch
        entry["last_seen"] = now_iso
        if not entry.get("link_key"):
            # Upgrade a pre-0.4.6 node: give it a rotatable link key, so the
            # installer can hand out a URL whose credential is revocable.
            entry["link_key"] = new_link_key()
        if entry.get("token") and supplied and token_matches_device(entry, supplied):
            # Move the device token off plaintext storage. The credential the
            # node was installed with keeps working in the query string until an
            # explicit token rotation (promote_device_token clears the flag):
            # re-running the installer or the updater must not silently break a
            # connector that was pasted into Gemini months ago.
            entry["token_sha256"] = hash_secret(supplied)
            entry.pop("token", None)
            entry["allow_legacy_query"] = True

    save_registry(registry)
    if created:
        logger.info("Registered node '%s' (link key issued, device token hashed)", clean_user)
    else:
        logger.info("Re-registered node '%s' after token proof", clean_user)

    return JSONResponse({
        "status": "success",
        "username": clean_user,
        "token": token_plain,
        "link_key": entry["link_key"],
        "gateway": PUBLIC_HOST,
        # The URL a client pastes carries the link key. The device token stays
        # in the caller's own config and in Authorization headers only.
        "sse_url": node_mcp_url(clean_user, entry["link_key"]),
        "tunnel_url": f"wss://{PUBLIC_HOST}/ws/tunnel?user={clean_user}",
        "reused": not created,
        "token_use": "Authorization: Bearer <token> (the tunnel and CLI use this; never put it in a URL)",
        "instructions": (
            f"Remote Harness MCP node '{clean_user}'. Paste sse_url into Google Gemini "
            "(Settings -> Connectors). Do not guess paths or outputs: use list_dir/"
            "glob_find/grep_search/read_file/bash_exec/system_vitals. Use run_job for "
            "slow commands; page output with max_chars+cursor."
        ),
    })

# ---------------------------------------------------------------------------
# DEVICE PAIRING (/v1/devices)
# ---------------------------------------------------------------------------
# The join flow that replaces "anyone may POST /api/register and receive a token":
#
#   1. the node asks for a code            POST /v1/devices/pair
#   2. an authenticated owner confirms in /pair (or an admin uses the API)
#   3. the node collects its credentials   GET  /v1/devices/pair/{code}
#
# Credentials are issued once; the initiating device may retry delivery until the code dies
# with its 10-minute TTL. Nothing is created in the registry before step 2, so a
# bot that can reach the endpoint can at worst ask for codes.
PAIR_TTL_SECONDS = 600
PAIR_MAX_PENDING = 200
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I confusables
ADMIN_TOKEN = os.environ.get("MESH_ADMIN_TOKEN", "").strip()

pending_pairs: dict[str, dict] = {}
owner_auth = OwnerAuth(PUBLIC_BASE_URL)


def admin_authorised(request: Request) -> bool:
    supplied = (request.headers.get("x-admin-token") or "").strip()
    return bool(ADMIN_TOKEN) and bool(supplied) and tokens_equal(supplied, ADMIN_TOKEN)


def purge_pairs() -> None:
    now = time.time()
    for code, pair in list(pending_pairs.items()):
        if pair.get("expires_at", 0) < now:
            pending_pairs.pop(code, None)


def new_pair_code() -> str:
    while True:
        raw = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
        code = f"{raw[:4]}-{raw[4:]}"
        if code not in pending_pairs:
            return code


async def device_pair_start(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Allow": "POST, OPTIONS", "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"})
    if rate_limited("pair", client_ip(request), limit=20):
        return JSONResponse(
            {"error": "rate_limited", "message": "Too many pairing requests from this address."},
            status_code=429,
        )
    purge_pairs()
    if len(pending_pairs) >= PAIR_MAX_PENDING:
        return JSONResponse(
            {"error": "too_many_pending", "message": "Too many pending pairings; try again shortly."},
            status_code=503,
        )
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    code = new_pair_code()
    device_secret = secrets.token_urlsafe(32)
    pending_pairs[code] = {
        "device_secret_sha256": hash_secret(device_secret),
        "created_at": time.time(),
        "expires_at": time.time() + PAIR_TTL_SECONDS,
        "hostname": str(data.get("hostname") or "")[:100],
        "os": str(data.get("os") or "")[:100],
        "arch": str(data.get("arch") or "")[:40],
        "approved": False,
        "issued": None,
        "collected": False,
        "attempts": 0,
    }
    logger.info("Device pairing request created")
    return JSONResponse({
        "pair_code": code,
        "device_secret": device_secret,
        "pair_url": f"{PUBLIC_BASE_URL}/pair?code={code}",
        "expires_in": PAIR_TTL_SECONDS,
        "message": "Open pair_url, sign in and confirm the device. No admin key is needed in the browser.",
    }, status_code=201, headers={"Cache-Control": "no-store"})


async def device_pair_status(request: Request):
    code = (request.path_params.get("code") or "").strip().upper()
    purge_pairs()
    pair = pending_pairs.get(code)
    if pair is None:
        return JSONResponse(
            {"error": "unknown_pair", "message": "Unknown or expired pairing code."},
            status_code=404,
        )
    if not pair["approved"]:
        pair["attempts"] += 1
        return JSONResponse({
            "status": "pending",
            "expires_in": max(0, int(pair["expires_at"] - time.time())),
        }, headers={"Cache-Control": "no-store"})
    supplied = request.headers.get("x-pair-secret", "")
    if not supplied or not tokens_equal(hash_secret(supplied), pair["device_secret_sha256"]):
        return JSONResponse({"error": "pair_secret_required"}, status_code=403)
    # Same device may retry a lost HTTP response until TTL. No second node/token
    # is created, and knowing the short human code never reveals credentials.
    issued = pair.get("issued") or {}
    entry = load_registry().get(issued.get('username'))
    if not entry or not token_matches_device(entry, issued.get('token', '')):
        return JSONResponse({'error': 'pair_revoked'}, status_code=410, headers={'Cache-Control': 'no-store'})
    pair["collected"] = True
    # A link may have been rotated in the owner UI while installation waited.
    issued = {**issued, 'link_key': entry['link_key'], 'sse_url': node_mcp_url(issued['username'], entry['link_key'])}
    return JSONResponse({"status": "approved", **issued}, headers={"Cache-Control": "no-store"})


async def device_pair_approve(request: Request):
    if not ADMIN_TOKEN:
        return JSONResponse({
            "error": "pairing_not_configured",
            "message": "This gateway has no MESH_ADMIN_TOKEN set, so no code can be approved.",
        }, status_code=503)
    if not admin_authorised(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    code = (request.path_params.get("code") or "").strip().upper()
    purge_pairs()
    pair = pending_pairs.get(code)
    if pair is None:
        return JSONResponse({"error": "unknown_pair"}, status_code=404)
    if pair["approved"]:
        return JSONResponse({"error": "already_approved"}, status_code=409)

    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    wanted = approve_pair(code, pair, data)
    return JSONResponse({"status": "approved", "username": wanted}, headers={"Cache-Control": "no-store"})


def approve_pair(code, pair, data, owner_subject=None):
    """No await between read/issue/save: one worker serialises identity issuance."""
    wanted = re.sub(r"[^a-z0-9_-]", "", str(data.get("username") or pair.get("hostname") or "node").lower())[:40]
    if not wanted or len(wanted) < 2:
        wanted = "node"
    registry = load_registry()
    if wanted in registry or wanted in RESERVED_NAMES:
        wanted = unique_node_name(registry, wanted)

    token = new_device_token()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry = new_node_entry("", pair["os"], pair["arch"], now_iso, token)
    entry["hostname"] = pair["hostname"]
    if owner_subject:
        entry["owner_subject"] = owner_subject
    registry[wanted] = entry
    save_registry(registry)

    pair["approved"] = True
    pair["owner_subject"] = owner_subject
    pair["issued"] = {
        "username": wanted,
        "token": token,
        "link_key": entry["link_key"],
        "gateway": PUBLIC_HOST,
        "sse_url": node_mcp_url(wanted, entry["link_key"]),
        "tunnel_url": f"wss://{PUBLIC_HOST}/ws/tunnel?user={wanted}",
        "token_use": "Authorization: Bearer <token> (the tunnel and CLI use this; never in a URL)",
    }
    logger.info("Paired node '%s'", wanted)
    return wanted


async def device_rotate(request: Request):
    """Issue a new link key, invalidating the old URL. Admin or the node itself."""
    user = (request.path_params.get("user") or "").lower()
    registry = load_registry()
    entry = registry.get(user)
    if entry is None:
        return JSONResponse({"error": "unknown_node"}, status_code=404)
    supplied, _source = request_credential(request)
    if not (admin_authorised(request) or token_matches_device(entry, supplied)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    entry["link_key"] = new_link_key()
    if entry.get("token"):
        # Move a pre-0.4.6 plaintext token to its hash before dropping it: the
        # credential must keep verifying, or rotating the link key would also
        # invalidate the device-token URL this node was installed with.
        entry["token_sha256"] = hash_secret(entry["token"])
        entry.pop("token", None)
    # `allow_legacy_query` deliberately survives: rotating the LINK key retires
    # the URL that carried it, and it is not a reason to also invalidate the
    # device-token URL such a node was originally installed with. Ending that one
    # is `rotate-token`'s job, after the node has the replacement.
    save_registry(registry)
    logger.info("Link key rotated for '%s'", user)
    return JSONResponse({
        "status": "rotated",
        "username": user,
        "link_key": entry["link_key"],
        "sse_url": node_mcp_url(user, entry["link_key"]),
        "message": "The previous URL no longer works. Update the client connector.",
    })


async def device_rotate_token(request: Request):
    """Issue a new DEVICE TOKEN for a node (admin, or the node itself).

    The old token keeps working until the node opens a tunnel with the new one
    (``promote_device_token``). That order matters: a rotation which invalidates
    the credential before the machine could store its replacement locks the node
    out of its own gateway, and the recovery for that is a reinstall - exactly
    what this endpoint exists to avoid. The response therefore carries the token
    exactly once and the caller must persist it.
    """
    user = (request.path_params.get("user") or "").lower()
    registry = load_registry()
    entry = registry.get(user)
    if entry is None:
        return JSONResponse({"error": "unknown_node"}, status_code=404)
    supplied, _source = request_credential(request)
    if not (admin_authorised(request) or token_matches_device(entry, supplied)):
        return JSONResponse({
            "error": "forbidden",
            "message": "Rotating a device token needs the node's current token or an admin token.",
        }, status_code=403)

    token = new_device_token()
    entry["token_next_sha256"] = hash_secret(token)
    entry["token_rotated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_registry(registry)
    logger.warning("Device token rotation requested for '%s' (pending until the node reconnects)", user)
    return JSONResponse({
        "status": "rotation_pending",
        "username": user,
        "token": token,
        "gateway": PUBLIC_HOST,
        "tunnel_url": f"wss://{PUBLIC_HOST}/ws/tunnel?user={user}",
        "note": (
            "Store this token in the node's configuration and restart the agent. The "
            "previous token stops working as soon as the node connects with this one."
        ),
    })


async def device_rotate_token_finish(request: Request):
    """Retire the previous device token immediately, without waiting for the node.

    For an operator who knows the leaked credential is in someone else's hands: the
    node will have to be fixed by hand, but the stolen token dies now.
    """
    user = (request.path_params.get("user") or "").lower()
    registry = load_registry()
    entry = registry.get(user)
    if entry is None:
        return JSONResponse({"error": "unknown_node"}, status_code=404)
    supplied, _source = request_credential(request)
    if not (admin_authorised(request)
            or token_matches_device(entry, supplied)
            or token_matches_next(entry, supplied)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not entry.get("token_next_sha256"):
        return JSONResponse({"error": "no_rotation_pending"}, status_code=409)
    promote_device_token(user, registry, entry)
    return JSONResponse({
        "status": "rotated",
        "username": user,
        "message": "The previous device token is now invalid.",
    })


async def device_revoke(request: Request):
    """Remove a node entirely, closing its tunnel. Admin or the node itself."""
    user = (request.path_params.get("user") or "").lower()
    registry = load_registry()
    entry = registry.get(user)
    if entry is None:
        return JSONResponse({"error": "unknown_node"}, status_code=404)
    supplied, _source = request_credential(request)
    if not (admin_authorised(request) or token_matches_device(entry, supplied)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await revoke_node(user, registry)


async def revoke_node(user, registry):
    registry.pop(user, None)
    save_registry(registry)
    tunnel = active_tunnels.pop(user, None)
    if tunnel and tunnel.get("ws"):
        try:
            await tunnel["ws"].close(code=4001, reason="Node revoked")
        except Exception:
            pass
    for queue in active_sse_subscribers.pop(user, []):
        try:
            queue.put_nowait(None)
        except Exception:
            pass
    logger.warning("Node '%s' revoked", user)
    return JSONResponse({"status": "revoked", "username": user})


async def pairing_page(request: Request):
    purge_pairs()
    code = (request.query_params.get('code') or '').upper().strip()
    pair = pending_pairs.get(code)
    if not pair:
        return page('Ссылка истекла', '<p>Заявка действует 10 минут. Запустите установщик ещё раз. При перезапуске Gateway нужно начать новую привязку.</p>', status=404)
    owner = owner_auth.owner(request)
    if pair['approved']:
        if owner and pair.get('owner_subject') == owner['subject']:
            return RedirectResponse('/account', status_code=303)
        return page('Заявка уже подтверждена', '<p>Вернитесь к установщику. Если это сделали не вы, не продолжайте установку и сообщите владельцу Gateway.</p>', status=409)
    metadata = f'<p>Устройство <strong>{escape(pair["hostname"])}</strong><br><span class="muted">{escape(pair["os"])} · {escape(pair["arch"])}</span></p>'
    steps = '<ol class="steps"><li>1. Установка</li><li aria-current="step">2. Подключение</li><li>3. Проверка связи</li></ol>'
    detail = f'<details><summary>Сверить код устройства</summary><p class="code">{escape(code)}</p><p>Вводить код не нужно. При желании сверьте его с установщиком.</p></details>'
    if not owner:
        if owner_auth.configured:
            action = f'<p>Войдите, чтобы сохранить устройство в своём аккаунте.</p><a class="button" href="{escape(owner_auth.login_link(code))}">Войти через Google</a>'
        else:
            action = '<p class="notice">Google OAuth ещё не настроен владельцем Gateway. Передайте ему адрес этой страницы. Административный ключ вводить не нужно.</p>'
        return page('Подключите устройство', steps + metadata + action + detail)
    content = steps + metadata + f'<p>Вы вошли как <strong>{escape(owner["email"])}</strong>.</p>'
    content += '<p class="notice">Подключайте только своё устройство. Коннектор сможет выполнять команды и изменять файлы с правами пользователя, запустившего агент.</p>'
    content += f'<form method="post" action="/account/pair/{escape(code)}/approve">{form_fields(owner["csrf"])}<button>Подключить устройство</button></form>'
    content += f'<div class="actions"><form method="post" action="/auth/logout">{form_fields(owner["csrf"])}<input type="hidden" name="return_code" value="{escape(code)}"><button class="secondary">Сменить аккаунт</button></form></div>' + detail
    return page('Это ваше устройство?', content)


async def owner_login(request: Request):
    if rate_limited('owner_login', client_ip(request), limit=20):
        return page('Подождите минуту', '<p>Слишком много попыток входа с этого адреса.</p>', status=429)
    return await owner_auth.login(request)


async def owner_pair_approve(request: Request):
    owner = owner_auth.owner(request)
    data = await fields(request)
    if not owner_auth.csrf_ok(request, owner, data):
        return page('Подтверждение отклонено', '<p>Войдите через Google заново и повторите подтверждение со страницы устройства.</p>', status=403)
    purge_pairs()
    code = request.path_params['code']
    pair = pending_pairs.get(code)
    if not pair:
        return page('Ссылка истекла', '<p>Запустите установщик ещё раз.</p>', status=404)
    if pair['approved']:
        if pair.get('owner_subject') != owner['subject']:
            return page('Заявка уже подтверждена', '<p>Устройство привязано к другому аккаунту.</p>', status=409)
    else:
        approve_pair(code, pair, {}, owner['subject'])
    return RedirectResponse('/account', status_code=303, headers={'Cache-Control': 'no-store'})


async def owner_account(request: Request):
    owner = owner_auth.owner(request)
    if not owner:
        return page('Ваши устройства', '<p>Войдите, чтобы получить ссылки подключений и управлять доступом.</p>'
                    f'<a class="button" href="{owner_auth.login_link()}">Войти через Google</a>')
    content = f'<p>{escape(owner["email"])}</p><p>После подтверждения дождитесь сообщения об успешной проверке в установщике. Затем создайте в ChatGPT отдельный MCP-коннектор для устройства: вставьте Server URL и выберите <strong>No authentication</strong>.</p>'
    devices = [(user, entry) for user, entry in load_registry().items() if entry.get('owner_subject') == owner['subject']]
    if not devices:
        content += '<p class="notice">Устройств пока нет. Запустите установщик на нужном устройстве и откройте его ссылку.</p>'
    for user, entry in devices:
        link = PUBLIC_BASE_URL + '/mcp?' + urlencode({'user': user, 'token': entry['link_key']})
        content += f'<article class="device"><h2>{escape(entry.get("hostname") or user)}</h2><p class="muted">{escape(user)} · {escape(entry.get("os", ""))}</p>'
        content += f'<label for="url-{escape(user)}">Приватный Server URL</label><textarea id="url-{escape(user)}" rows="3" readonly spellcheck="false">{escape(link)}</textarea>'
        content += f'<div class="actions"><button class="secondary copy" data-target="url-{escape(user)}">Скопировать ссылку</button><a class="button" href="https://chatgpt.com/plugins?directoryTab=personal" target="_blank" rel="noopener noreferrer">Вставить в ChatGPT</a></div>'
        content += '<figure class="connect-guide"><img src="/assets/chatgpt-add-mcp.png" width="1019" height="216" loading="lazy" alt="В ChatGPT откройте Плагины → Личные, нажмите «Добавить», затем «Добавить пользовательский сервер MCP»."><figcaption>Сначала скопируйте ссылку. Затем в ChatGPT нажмите «Добавить» → «Добавить пользовательский сервер MCP» и вставьте её в Server URL. <span class="auth-hint">В настройках вместо <strong>oAuth</strong> - выберите <strong>No authentication</strong></span></figcaption></figure><div class="actions">'
        for action, label in [('check', 'Проверить связь'), ('rotate', 'Заменить ссылку'), ('revoke', 'Отозвать доступ')]:
            content += f'<form method="post" action="/account/devices/{escape(user)}/{action}">{form_fields(owner["csrf"])}<button class="secondary">{label}</button></form>'
        content += '</div></article>'
    content += f'<div class="actions"><form method="post" action="/auth/logout">{form_fields(owner["csrf"])}<button class="secondary">Выйти</button></form></div><p id="status" role="status" aria-live="polite"></p>'
    script = """document.querySelectorAll('.copy').forEach(button=>button.addEventListener('click',async()=>{
const field=document.getElementById(button.dataset.target);const status=document.getElementById('status');
try{await navigator.clipboard.writeText(field.value);status.textContent='Ссылка скопирована. Вставьте её в Server URL в ChatGPT.';}
catch(error){field.focus();field.select();status.textContent='Скопируйте выделенную ссылку вручную.';}}));"""
    return page('Ваши устройства', content, script=script)


async def owner_device_action(request: Request):
    owner = owner_auth.owner(request)
    data = await fields(request)
    if not owner_auth.csrf_ok(request, owner, data):
        return page('Действие отклонено', '<p>Сеанс истёк или страница устарела. Войдите и повторите действие.</p>', status=403)
    registry = load_registry()
    user = request.path_params['user']
    entry = registry.get(user)
    if not entry or entry.get('owner_subject') != owner['subject']:
        return page('Устройство не найдено', '<p>Нет доступа к этому устройству.</p>', status=404)
    action = request.path_params['action']
    back = '<p><a href="/account">К устройствам</a></p>'
    if action == 'check':
        result = await call_remote_tool(user, 'mesh_status', {})
        error = remote_tool_error(result)
        if error or not result.get('agent_pid'):
            return page('Устройство пока не отвечает', '<p>Дождитесь окончания установки. Если агент уже установлен, запустите диагностику status на устройстве и проверьте его службу.</p>' + back, status=503)
        return page('Связь подтверждена', f'<p>Получен реальный ответ агента на {escape(result.get("hostname", user))}.</p>' + back)
    if action not in {'rotate', 'revoke'}:
        return page('Неизвестное действие', back, status=404)
    if data.get('confirm') != 'yes':
        explanation = 'Все коннекторы с прежней ссылкой перестанут работать. Агент останется подключён.' if action == 'rotate' else 'Агент и все коннекторы потеряют доступ. Для восстановления потребуется новая привязка устройства.'
        return page('Заменить ссылку?' if action == 'rotate' else 'Отозвать доступ?',
            f'<p>{escape(entry.get("hostname") or user)}</p><p>{explanation}</p><form method="post">{form_fields(owner["csrf"])}<input type="hidden" name="confirm" value="yes"><button>Подтвердить</button></form>' + back)
    if action == 'rotate':
        entry['link_key'] = new_link_key()
        save_registry(registry)
    else:
        await revoke_node(user, registry)
    return RedirectResponse('/account', status_code=303, headers={'Cache-Control': 'no-store'})


async def chatgpt_guide_image(request: Request):
    from pathlib import Path
    image_path = Path(__file__).resolve().parent / 'gateway_assets' / 'chatgpt-add-mcp.png'
    return Response(image_path.read_bytes(), media_type='image/png', headers={
        'Cache-Control': 'public, max-age=3600', 'X-Content-Type-Options': 'nosniff'})


async def installer_asset(request: Request):
    """Publish code only, never a directory, configuration, registry or token."""
    from pathlib import Path
    root = Path(__file__).resolve().parent
    relative = request.url.path.lstrip('/')
    allowed = {'install.sh', 'install.ps1', 'skills/orchestrator.md',
               'ops/windows/agent-watchdog.ps1', 'ops/windows/run-hidden.vbs',
               'ops/update.ps1', 'ops/doctor.ps1'}
    allowed.update('core/' + p.name for p in (root / 'core').glob('*.py') if not p.is_symlink())
    if relative not in allowed:
        return Response('Not found', status_code=404)
    target = root / relative
    if not target.is_file() or target.is_symlink():
        return Response('Not found', status_code=404)
    payload = target.read_bytes()
    if relative == 'install.sh':
        text = payload.decode('utf-8')
        text = re.sub(r'^PUBLISHED_DOMAIN=.*$', lambda m: 'PUBLISHED_DOMAIN=' + json.dumps(PUBLIC_HOST), text, flags=re.MULTILINE)
        text = text.replace('PAIR=false', 'PAIR=true', 1)
        payload = text.encode('utf-8')
    elif relative == 'install.ps1':
        text = payload.decode('utf-8-sig')
        text = re.sub(r'(?m)^(\s*)\$PublishedDomain = .*$', lambda m: m[1] + '$PublishedDomain = ' + json.dumps(PUBLIC_HOST), text)
        text = text.replace('[switch]$Pair,', '[switch]$Pair = $true,', 1)
        payload = ('\ufeff' + text).encode('utf-8')
    return Response(payload if request.method != 'HEAD' else b'', media_type='text/plain',
                    headers={'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff',
                             'Content-Length': str(len(payload))})


async def oauth_discovery(request: Request):
    """RFC 9728 protected-resource metadata.

    Returning 404 here made every MCP client log a connection error while
    connecting (Gemini Spark probes this path twice). The mesh authenticates with
    a bearer token in the URL, so the honest answer is metadata that advertises
    no authorization server and no bearer requirement for discovery.
    """
    host = request.headers.get("host", PUBLIC_HOST)
    base = f"https://{host}"
    return JSONResponse(
        {
            "resource": base,
            "authorization_servers": [],
            "bearer_methods_supported": ["query", "header"],
            "scopes_supported": [],
            "resource_documentation": "https://github.com/LevRa7/remote-harness-mcp",
            "resource_name": "Remote Harness MCP",
        },
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS, HEAD",
            "Access-Control-Allow-Headers": "*",
            "Cache-Control": "public, max-age=300",
        },
    )

async def sse_endpoint(request: Request):
    ok, msg, user = verify_token(request)
    if not ok:
        return JSONResponse(
            {"error": "unauthorized", "message": msg},
            status_code=401,
            headers={"Access-Control-Allow-Origin": "*"}
        )

    if request.method == "HEAD":
        return Response(
            status_code=200,
            media_type="text/event-stream",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
                "mcp-protocol-version": protocol_version_for(user),
            }
        )

    # The credential a client authenticated WITH is what the classic transport may
    # echo back in its endpoint frame. When the client proved itself with a header
    # there is nothing to echo: the secret stays in its own headers, out of the
    # URL and out of the proxy log. When it used a link key, that (rotatable,
    # URL-safe) key is the only thing that may appear here.
    credential, credential_source = request_credential(request)
    endpoint_credential = credential if credential_source == "query" else ""
    # Two different things are needed here, and conflating them broke delivery:
    #   * the id the CLIENT sees must match the one POST responses return, or the
    #     client believes it holds two sessions and aborts one;
    #   * the key this queue is stored under must be UNIQUE per connection,
    #     because Google's frontends load-balance requests across many IPs and
    #     any one of them may send DELETE. With a shared key that DELETE destroyed
    #     the stream another in-flight request was still reading, so its result
    #     never reached the model.
    session_id = resolve_session_id(request, user)
    stream_key = str(uuid.uuid4())
    # The classic SSE transport announces where to POST. A Streamable HTTP client
    # (GET /mcp) must receive JSON-RPC frames only: an `endpoint` frame pointing
    # at a different path is not part of that protocol, and a strict client drops
    # the stream over it - the stream it was reading tool results from.
    announce_endpoint = announce_endpoint_for_path(request.url.path)
    queue = asyncio.Queue()
    queues = active_sse_subscribers.setdefault(user, [])
    queues.append(queue)
    # Cap the streams one user may hold. Nothing in the protocol closes a stream
    # a client forgot, so a client whose session id was lost (or that reconnects
    # on every turn) accumulates queues until its own connection limit is reached
    # and it can no longer open one at all. Evicting the OLDEST is safe: the
    # newest is the stream the client just opened and is actually reading.
    evicted = 0
    while len(queues) > MAX_SSE_PER_USER:
        oldest = queues.pop(0)
        evicted += 1
        try:
            oldest.put_nowait(None)      # sentinel: the stream loop closes on None
        except Exception:
            pass
        # Also drop it from the session index now, so /health and a later
        # re-initialize never see a stream that was already closed.
        forget_sse_queue(user, oldest)
    # stored under the per-connection key, not the shared client-visible id
    active_sse_sessions[stream_key] = queue
    # ...and indexed by the client-visible session id, so a re-initialize can
    # close exactly the streams of the session it replaces.
    active_sse_session_queues.setdefault(session_id, []).append(queue)
    logger.info(
        f"SSE stream opened for '{user}' (stream_key={stream_key}, "
        f"session={session_id[:12]}…, path={request.url.path}, "
        f"open_now={len(queues)}"
        + (f", evicted={evicted} over the cap of {MAX_SSE_PER_USER}" if evicted else "")
        + ")"
    )

    async def event_generator():
        # Keepalive deadline: intermediaries (nginx, Google's frontend) close an
        # idle stream, and a closed stream is what makes a client believe the
        # server went away. One frame - a comment counts - goes out at least every
        # 15 s. Framing is untouched: `event: endpoint` once, then one
        # `event: message` per queued frame.
        keepalive_interval = 15.0
        last_write = time.monotonic()
        try:
            if announce_endpoint:
                endpoint = f"/messages?user={user}"
                if endpoint_credential:
                    endpoint += f"&token={endpoint_credential}"
                yield f"event: endpoint\ndata: {endpoint}&stream_id={stream_key}\n\n"
            while True:
                try:
                    remaining = keepalive_interval - (time.monotonic() - last_write)
                    msg = await asyncio.wait_for(queue.get(), timeout=max(0.25, remaining))
                except asyncio.TimeoutError:
                    try:
                        yield ": keepalive\n\n"
                    except Exception as exc:
                        # The write raised: nobody is reading this stream any
                        # more, so drop it instead of keeping the queue alive.
                        logger.warning(f"SSE keepalive write failed for '{user}' "
                                       f"(stream_key={stream_key}): {exc}")
                        break
                    last_write = time.monotonic()
                    continue
                except Exception as exc:
                    logger.warning(f"SSE queue unreadable for '{user}' "
                                   f"(stream_key={stream_key}): {exc}")
                    break
                if msg is None:
                    break
                try:
                    yield f"event: message\ndata: {msg}\n\n"
                except Exception as exc:
                    logger.warning(f"SSE write failed for '{user}' "
                                   f"(stream_key={stream_key}): {exc}")
                    break
                last_write = time.monotonic()
        except asyncio.CancelledError:
            logger.info(f"SSE stream cancelled for '{user}' (stream_key={stream_key})")
            raise
        finally:
            active_sse_sessions.pop(stream_key, None)
            forget_sse_queue(user, queue)
            logger.info(f"SSE stream closed for '{user}' (stream_key={stream_key}, "
                        f"open_now={len(active_sse_subscribers.get(user, ()))})")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
            "mcp-session-id": session_id,
            "mcp-protocol-version": protocol_version_for(user),
        }
    )

async def messages_endpoint(request: Request):
    ok, err_msg, user = verify_token(request)
    if not ok:
        return JSONResponse(
            {"error": "unauthorized", "message": err_msg},
            status_code=401,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
                "mcp-protocol-version": protocol_version_for(user),
            }
        )

    classic_stream = None
    stream_id = request.query_params.get("stream_id")
    if request.url.path == "/messages" and stream_id:
        classic_stream = active_sse_sessions.get(stream_id)
        if classic_stream is None or classic_stream not in active_sse_subscribers.get(user, []):
            return JSONResponse({"error": "SSE stream expired; reconnect"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}

    method = body.get("method")
    params = body.get("params", {})
    req_id = body.get("id")

    if req_id is None and method:
        session_id = resolve_session_id(request, user)
        return Response(
            status_code=204,
            headers={
                "mcp-session-id": session_id,
                "mcp-protocol-version": protocol_version_for(user),
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
            }
        )

    client_ip = request.client.host if request.client else "unknown"
    call_started = time.monotonic()
    # What the client accepts decides how the answer must be framed: a strict
    # streamable-HTTP client that asks only for text/event-stream rejects a plain
    # application/json body even when the payload is correct.
    accept_hdr = (request.headers.get("accept") or "").lower()
    incoming_session = request.headers.get("mcp-session-id") or ""
    proto_hdr = request.headers.get("mcp-protocol-version") or ""
    # Arguments can contain file contents, credentials and private messages.
    tool_note = f", tool={params.get('name')}" if method == "tools/call" else ""
    logger.info(f"Incoming MCP RPC from {client_ip} [{user}]: method={method}, id={req_id}{tool_note}"
                + (f", accept={accept_hdr[:60]!r}" if accept_hdr else "")
                + (f", session={incoming_session[:40]!r}" if incoming_session else ", session=<none>")
                + (f", proto-hdr={proto_hdr[:20]!r}" if proto_hdr else ""))

    resp = {"jsonrpc": "2.0", "id": req_id}
    issued_session_id = ""  # set by initialize; echoed in the response header below

    if method == "initialize":
        # Protocol version negotiation. The spec says a client MUST disconnect
        # when the server answers with a version it does not support, and we used
        # to hard-code 2024-11-05 whatever the client asked for. Echo the client's
        # version when we support it, otherwise fall back to our baseline.
        client_version = str((params or {}).get("protocolVersion") or "")
        supported = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
        if client_version in supported:
            negotiated = client_version
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", client_version):
            # The tool surface (initialize / tools list+call / ping) is stable
            # across revisions. Answering an older version than the client asked
            # for makes a compliant client DISCONNECT immediately - that was the
            # real cause of the session churn - so echo any dated version rather
            # than lose the session.
            negotiated = client_version
        else:
            negotiated = "2024-11-05"
        active_protocol_versions[user] = negotiated
        # Session hygiene: this initialize replaces whatever session this user
        # held before, and the client will never use that id again. Leaving its
        # streams open is how one user ended up with 9 live streams and no
        # tunnel - the client lost the session id, re-initialized every turn, and
        # every abandoned stream stayed registered (and counted in /health).
        # The response-header contract is untouched: the NEW id is what this
        # response returns.
        previous_session_id = latest_session_by_user.get(user)
        if previous_session_id and classic_stream is None:
            closed = close_sse_streams_for_session(previous_session_id)
            active_sessions.pop(previous_session_id, None)
            if closed:
                logger.info(f"initialize: closed {closed} superseded SSE stream(s) of session "
                            f"{previous_session_id[:12]}… for user '{user}'")
        session_id = issue_session_id(user)
        issued_session_id = session_id
        logger.info(f"initialize: client requested protocolVersion={client_version!r} "
                    f"clientInfo={(params or {}).get('clientInfo')} -> answering {negotiated}; "
                    f"issued session {session_id[:12]}… for user '{user}'")
        resp["result"] = {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
                "prompts": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False}
            },
            "serverInfo": {
                "name": "remote_harness_mcp",
                "version": "1.0.0"
            },
            "instructions": (
                (
                (
                f"Remote Harness MCP node '{user}' - direct access to the real host over a reverse tunnel.\n"
                f"GROUNDING (mandatory): NEVER state a path, file, directory or process you have not seen "
                f"in a tool result in THIS conversation. Tool output marked [VERIFIED BY TOOL] is real; "
                f"anything marked [TOOL ERROR] means the path does NOT exist - never repeat it as real. "
                f"If you have not inspected something, say so and call list_dir / glob_find / grep_search first.\n"
                f"USE: list_dir/glob_find/grep_search to locate, read_file to read, write_file/edit_file to "
                f"modify, bash_exec for short commands, system_vitals for resources. Only touch paths you "
                f"have confirmed. For slow work (builds, installs, tests, downloads) use run_job, then "
                f"job_output / job_kill / job_list. Long output is paged: pass cursor=<next_cursor>.\n"
                f"REACHABILITY: every tool result you receive in this conversation was produced ON the "
                f"node, so the node IS online. After any successful tool result, never tell the user the "
                f"host is unreachable or that the agent did not answer. If a call fails, report its exact "
                f"error text instead of guessing that the host is down.\n"
                f"DEVICE: if system_info reports a 'device' block whose class is phone or tablet, prefer "
                f"device_info / device_control for device-level work instead of hand-writing termux-* "
                f"commands through bash_exec, and use device_capture / device_messages for the camera, "
                f"microphone, location, SMS or contacts. Client approval and "
                f"the node's own MESH_DEVICE / MESH_DEVICE_ACTIONS / MESH_DEVICE_PIM switches are what "
                f"decides whether the call is allowed."
            )
            )
            )
        }
    elif method == "notifications/initialized":
        session_id = resolve_session_id(request, user)
        return Response(
            status_code=204,
            headers={
                "mcp-session-id": session_id,
                "mcp-protocol-version": protocol_version_for(user),
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
            }
        )
    elif method == "ping":
        resp["result"] = {}
    elif method == "tools/list":
        # annotate_tools attaches the confirmation policy (read-only / local
        # write / publish / destructive) declared at the bottom of this file.
        resp["result"] = {
            "tools": annotate_tools([
                {
                    "name": "list_dir",
                    "description": (
                        "List a directory on the remote host (default: workspace)."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Optional directory path to inspect. If omitted or empty, lists the agent's current working directory."
                            }
                        },
                        "required": []
                    }
                },
                {
                    "name": "read_file",
                    "description": (
                        "Read a file, line-numbered. Page with max_chars/cursor."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path to read"},
                            "start_line": {"type": "integer", "description": "First line (1-based)"},
                            "end_line": {"type": "integer", "description": "Last line (1-based)"}
                        },
                        "required": ["path"]
                    }
                },
                {
                    "name": "bash_exec",
                    "description": (
                        f"Execute a shell command directly on {user}'s host machine. "
                        "The shell follows the host: bash on Linux/macOS, PowerShell or cmd.exe "
                        "on Windows - call system_info first and match command_shell. "
                        "Use it to inspect git status, run builds, check processes or run tasks, "
                        "and NEVER guess command outputs. Commands that install or remove "
                        "software, or that delete data, are refused here and must go through "
                        "system_change. Client permissions still apply to all commands."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Shell command to execute on the node"
                            }
                        },
                        "required": ["command"]
                    }
                },
                {
                    "name": "system_change",
                    "description": (
                        "Install or remove software, or delete data, on the host - the only tool "
                        "allowed to run those commands, and the one the user is asked to confirm "
                        "before it runs. Same shell as bash_exec (check command_shell from "
                        "system_info first). Use it for package managers (apt/dnf/pacman/apk/brew/"
                        "pip/npm/winget/msiexec/...), uninstallers, rm / Remove-Item / del, disk "
                        "wipes and anything else that installs, removes or destroys. Set "
                        "background=true for long installs and poll with job_output. Ordinary "
                        "inspection, builds and tests belong in bash_exec / run_job instead."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string",
                                        "description": "The install / remove / delete command"},
                            "timeout_sec": {"type": "integer",
                                            "description": "Foreground only: 1-120 seconds (default 25)"},
                            "background": {"type": "boolean",
                                           "description": "Run it as a background job and return a job_id"},
                            "cwd": {"type": "string",
                                    "description": "Working directory; background=true only"},
                            "max_chars": {"type": "integer",
                                          "description": "Maximum characters per response"},
                            "cursor": {"type": "integer",
                                       "description": "Character offset to continue from"}
                        },
                        "required": ["command"]
                    }
                },
                {
                    "name": "mesh_status",
                    "description": ("Confirm this node is reachable. Call this FIRST if you believe the "
                                    "host is offline: it returns live evidence from the host."),
                    "inputSchema": {"type": "object", "properties": {}, "required": []}
                },
                {
                    "name": "mesh_update",
                    "description": ("Report, check for, or install a newer release of the node's own "
                                    "code. action=status reads local state; action=check asks GitHub; "
                                    "action=apply installs it and restarts the agent (the tunnel "
                                    "drops for a few seconds). Default action is status."),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["status", "check", "apply"],
                                       "description": "What to do (default: status)"},
                            "force": {"type": "boolean",
                                      "description": "Ignore the check interval and the failed-apply backoff"},
                            "offline": {"type": "boolean",
                                        "description": "check: cached answer only, no network"},
                            "restart": {"type": "boolean",
                                        "description": "apply: restart the node afterwards (default true)"}
                        },
                        "required": []
                    }
                },
                # The node's own device (a phone/tablet under Termux, or the local
                # laptop/desktop). core/mcp_tools.TOOLS is the SOURCE OF TRUTH for
                # these four entries: their descriptions and schemas below are copied
                # from it value for value (only the layout is the gateway's own - it
                # deploys as one file and may not import node code). The node is what
                # actually executes, and the client reads this copy, so any wording
                # difference here would be a lie the model acts on. Keep them in step;
                # tests/test_gateway_share_route.py pins the copy for these four.
                {
                    "name": "device_info",
                    "description": (
                        "One-call report for the device this node runs on: battery and charging, "
                        "network interfaces with Wi-Fi and cellular signal strength, language, time and "
                        "timezone, CPU, RAM, storage, cameras, microphones, sensors and what the platform "
                        "can actually reach. On an Android/Termux node it reads the phone through the "
                        "termux-api commands; on a laptop or desktop it reads the local system. Every "
                        "section carries available/source, so 'not exposed here' is never reported as a "
                        "zero - and on a non-phone the phone-only sections explain themselves instead of "
                        "failing. Use device_control, device_capture or device_messages to act."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "section": {
                                "type": "string",
                                "enum": ["summary", "all", "device", "battery", "network", "locale",
                                         "time", "hardware", "storage", "cameras", "microphones",
                                         "sensors", "capabilities"],
                                "description": "Which blocks to return (default: all)."
                            },
                            "sensor": {
                                "type": "string",
                                "description": ("Name one sensor to take a single live sample from "
                                                "(Android only; needs a name from the sensors block).")
                            },
                            "fresh": {
                                "type": "boolean",
                                "description": "Bypass the short-lived cache (default false)."
                            },
                            "quick": {
                                "type": "boolean",
                                "description": ("Skip the slow sections: network, cameras, microphones, "
                                                "sensors (default false).")
                            }
                        },
                        "additionalProperties": False
                    }
                },
                {
                    "name": "device_control",
                    "description": (
                        "Reversible actions on the node's own device: torch, vibrate, volume, screen "
                        "brightness, text-to-speech, toast, notifications, clipboard, media playback, "
                        "media scan, wake lock, download, opening or sharing a file, a text dialog and "
                        "the wallpaper. Each action is a fixed termux-api command on Android and an "
                        "explained no-op elsewhere; the result names the command and its argv so nothing "
                        "is guessed. Nothing here is destructive, and all of it is refused when the node "
                        "runs with MESH_READ_ONLY=1."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["torch", "vibrate", "volume", "volume_get", "brightness",
                                         "tts_speak", "toast", "notify", "notify_list",
                                         "notify_remove", "clipboard_get", "clipboard_set", "media",
                                         "media_scan", "wakelock", "download", "open", "share",
                                         "dialog", "wallpaper"],
                                "description": "Action to perform."
                            },
                            "on": {
                                "type": "boolean",
                                "description": "torch: true lights it, false puts it out."
                            },
                            "value": {
                                "type": "integer",
                                "description": ("vibrate: milliseconds; volume: level 0-15; "
                                                "brightness: 0-255.")
                            },
                            "stream": {
                                "type": "string",
                                "enum": ["music", "call", "alarm", "notification", "ring", "system"],
                                "description": "volume: which audio stream (default music)."
                            },
                            "text": {
                                "type": "string",
                                "description": ("tts_speak/toast/notify/clipboard_set/dialog body; media: "
                                                "play|pause|stop|info.")
                            },
                            "title": {
                                "type": "string",
                                "description": "notify/dialog: heading text."
                            },
                            "id": {
                                "type": "string",
                                "description": "notify_remove: the id the notification was posted with."
                            },
                            "url": {
                                "type": "string",
                                "description": "download/open/wallpaper: URL to use."
                            },
                            "path": {
                                "type": "string",
                                "description": "open/share/media/download/media_scan/wallpaper: file on the node."
                            },
                            "state": {
                                "type": "string",
                                "enum": ["acquire", "release", "status"],
                                "description": "wakelock: what to do (default status)."
                            },
                            "timeout_sec": {
                                "type": "integer",
                                "description": "dialog: seconds to wait for the person (5-120, default 30)."
                            }
                        },
                        "required": ["action"],
                        "additionalProperties": False
                    }
                },
                {
                    "name": "device_capture",
                    "description": (
                        "Camera, microphone, location, fingerprint, USB and infrared actions. These "
                        "observe the physical world or authenticate the person holding the phone, but "
                        "client approval settings apply in addition to device permissions; the "
                        "guard is the node's own switch (MESH_DEVICE, MESH_DEVICE_ACTIONS, "
                        "MESH_READ_ONLY), which refuses a call before any command runs. A photo or a "
                        "recording is written to the node's capture directory and is never uploaded "
                        "anywhere by itself; "
                        "fingerprint returns only the verdict, never biometric data; USB access requests "
                        "permission and returns the descriptor but deliberately refuses to run a program "
                        "for you; and on Android 11+ the Termux:API app has to be in the foreground for a "
                        "recording."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["camera_list", "camera_photo", "mic_record_start",
                                         "mic_record_stop", "mic_record_status", "location",
                                         "fingerprint", "usb_list", "usb_access",
                                         "infrared_frequencies", "infrared_transmit"],
                                "description": "Capture action to perform."
                            },
                            "camera_id": {
                                "type": "integer",
                                "description": "camera_photo: id from camera_list (default 0)."
                            },
                            "path": {
                                "type": "string",
                                "description": ("camera_photo/mic_record_start: output file (default: the "
                                                "node's capture directory); usb_access: device path from "
                                                "usb_list.")
                            },
                            "seconds": {
                                "type": "integer",
                                "description": "mic_record_start: recording length limit in seconds."
                            },
                            "provider": {
                                "type": "string",
                                "enum": ["gps", "network", "passive"],
                                "description": "location: which Android provider to use (default gps)."
                            },
                            "frequency": {
                                "type": "integer",
                                "description": "infrared_transmit: carrier frequency in Hz."
                            },
                            "pattern": {
                                "type": "string",
                                "description": ("infrared_transmit: durations in microseconds, e.g. "
                                                "1000,2000,1000.")
                            }
                        },
                        "required": ["action"],
                        "additionalProperties": False
                    }
                },
                {
                    "name": "device_messages",
                    "description": (
                        "SMS, call log, contacts and placing a call. This is private data about the person "
                        "who owns the phone, so the node refuses "
                        "until its operator enables it with MESH_DEVICE_PIM=1. The refusal names the flag "
                        "and the Android permissions to grant, so the answer is an instruction rather than "
                        "a dead end. Sending an SMS and placing a call are also refused under "
                        "MESH_READ_ONLY=1."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["sms_list", "sms_send", "call_log", "contacts", "call"],
                                "description": "Message or PIM action to perform."
                            },
                            "number": {
                                "type": "string",
                                "description": "sms_send/call: recipient number."
                            },
                            "text": {
                                "type": "string",
                                "description": "sms_send: message body."
                            },
                            "limit": {
                                "type": "integer",
                                "description": "How many records (1-50, default 10)."
                            },
                            "offset": {
                                "type": "integer",
                                "description": "Record offset, for paging."
                            },
                            "type": {
                                "type": "string",
                                "description": ("sms_list/call_log: filter, e.g. inbox/sent or "
                                                "incoming/outgoing/missed.")
                            },
                            "query": {
                                "type": "string",
                                "description": "contacts: filter by name or number."
                            }
                        },
                        "required": ["action"],
                        "additionalProperties": False
                    }
                },
                {
                    "name": "system_info",
                    "description": ("One-call host summary: OS, desktop, user, home, disks, memory, load, "
                                    "top processes and the current wallpaper. Prefer this over several "
                                    "exploratory commands - the client allows only a few calls per turn."),
                    "inputSchema": {"type": "object", "properties": {}, "required": []}
                },
                {
                    "name": "system_vitals",
                    "description": f"Retrieve real-time CPU, RAM, and Disk metrics on {user}'s host machine",
                    "inputSchema": {"type": "object", "properties": {}, "required": []}
                },
                {
                    "name": "get_orchestration_skill",
                    "description": "Load the node orchestration rules.",
                    "inputSchema": {"type": "object", "properties": {}, "required": []}
                },
                {
                    "name": "write_file",
                    "description": (
                        "Write a file atomically (creates parent dirs). A path inside the "
                        "system - /etc, /usr, "
                        "/boot, /var/lib, C:\\Windows, C:\\Program Files, ProgramData, the "
                        "registry - is refused and has to go through system_write, which "
                        "the user is asked to confirm."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Absolute path, or relative to the agent's workspace"},
                            "content": {"type": "string", "description": "Exact file content to write"},
                            "create_dirs": {"type": "boolean", "description": "Create parent directories"},
                            "mode": {"type": "string", "description": "File mode, e.g. 0644"}
                        },
                        "required": ["path", "content"]
                    }
                },
                {
                    "name": "edit_file",
                    "description": (
                        "Replace an exact string in a file; old_string must be unique. A "
                        "system path is refused here - "
                        "read the file and write the whole new content with system_write "
                        "instead, which the user confirms."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File to edit"},
                            "old_string": {"type": "string", "description": "Exact text to replace (not a regex); must be unique unless replace_all is true"},
                            "new_string": {"type": "string", "description": "Replacement text"},
                            "expected_sha256": {"type": "string", "description": "Fail if file changed since read"},
                            "replace_all": {"type": "boolean", "description": "Replace every occurrence"}
                        },
                        "required": ["path", "old_string", "new_string"]
                    }
                },
                {
                    "name": "system_write",
                    "description": (
                        "Write a file inside a system path - /etc, /usr, /boot, /var/lib, "
                        "C:\\Windows, C:\\Program Files, ProgramData, the registry - the only "
                        "way to write there, and the one the user is asked to confirm before "
                        "it runs. Takes the exact content, like write_file (no shell "
                        "quoting). To change an existing system file, read_file it first and "
                        "send the full new content here."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "System path to write"},
                            "content": {"type": "string", "description": "Exact file content to write"},
                            "create_dirs": {"type": "boolean", "description": "Create parent directories"},
                            "mode": {"type": "string", "description": "File mode, e.g. 0644 (POSIX only)"}
                        },
                        "required": ["path", "content"]
                    }
                },
                {
                    "name": "grep_search",
                    "description": (
                        "Recursively search file contents; returns path:line: text."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Text or regular expression to search for"},
                            "path": {"type": "string", "description": "Directory to search recursively (default \".\")"},
                            "glob": {"type": "string", "description": "Filename filter"},
                            "limit": {"type": "integer", "description": "Max results"},
                            "ignore_case": {"type": "boolean", "description": "Case-insensitive"},
                            "fixed": {"type": "boolean", "description": "Literal text, not regex"},
                            "context": {"type": "integer", "description": "Context lines"}
                        },
                        "required": ["pattern"]
                    }
                },
                {
                    "name": "glob_find",
                    "description": (
                        "Find files by glob pattern (* and **)."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Glob pattern, e.g. \"**/*.py\" or \"src/*.json\""},
                            "path": {"type": "string", "description": "Base directory to search from (default \".\")"}
                        },
                        "required": ["pattern"]
                    }
                },
                {
                    "name": "run_job",
                    "description": (
                        "Start a long command in the background; returns job_id. Installs, "
                        "removals and deletions are refused here; run those with "
                        "system_change(background=true)."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Shell command to run in the background"},
                            "cwd": {"type": "string", "description": "Optional working directory"}
                        },
                        "required": ["command"]
                    }
                },
                {
                    "name": "job_output",
                    "description": (
                        "Get a job status/output. wait_ms<=20000; page via next_cursor."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string", "description": "Job id returned by run_job"},
                            "wait_ms": {"type": "integer", "description": "Block up to this long (max 20000)"},
                            "max_chars": {"type": "integer", "description": "Chunk size"},
                            "cursor": {"type": "integer", "description": "Offset to continue from"}
                        },
                        "required": ["job_id"]
                    }
                },
                {
                    "name": "job_kill",
                    "description": "Stop a running job.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string", "description": "Job id to stop"},
                            "signal": {"type": "string", "description": "Signal to send, e.g. TERM or KILL (default TERM)"}
                        },
                        "required": ["job_id"]
                    }
                },
                {
                    "name": "job_list",
                    "description": "List recent jobs, newest first (20 by default).",
                    "inputSchema": {"type": "object", "properties": {
                        "limit": {"type": "integer", "description": "How many jobs to return (1-50, default 20)"}
                    }, "required": []}
                }
            ] + SHARE_TOOL_SPECS)
        }
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        is_error = False
        if name == "get_orchestration_skill":
            content_text = get_skill(user, request.headers.get("host", PUBLIC_HOST))
        elif name == "mesh_status":
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                parts = ["[NODE REACHABLE] This answer was produced on the host itself - the mesh "
                         "agent and the tunnel are working. Do not claim the node is offline."]
                for key in ("hostname", "agent_pid", "checked_at", "host_uptime_human",
                            "workspace", "node_version"):
                    if res.get(key) is not None:
                        parts.append(f"{key}: {res.get(key)}")
                content_text = "\n".join(parts)
        elif name == "mesh_update":
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                parts = []
                if res.get("current"):
                    parts.append(f"node version: {res['current']}")
                if res.get("latest"):
                    parts.append(f"newest release: {res['latest']}")
                if res.get("update_available") is not None:
                    parts.append("update available: %s"
                                 % ("yes" if res.get("update_available") else "no"))
                if res.get("applied"):
                    parts.append("applied: %s -> %s"
                                 % (res.get("current"), res.get("latest")))
                    parts.append("node is restarting; the tunnel drops for a few seconds")
                elif res.get("reason"):
                    parts.append(f"detail: {res['reason']}")
                content_text = "\n".join(parts) if parts else json.dumps(res, ensure_ascii=False, indent=2)
        elif name == "system_info":
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                parts = []
                # command_shell tells the model which shell bash_exec really uses
                # (PowerShell or cmd.exe on Windows, bash on Linux), so it writes
                # commands for the right one instead of assuming bash.
                for key in ("hostname", "os", "kernel", "user", "home", "desktop",
                            "session_type", "command_shell", "scenario", "device_class",
                            "wallpaper", "wallpaper_exists"):
                    value = res.get(key)
                    if value not in (None, ""):
                        parts.append("%s: %s" % (key, value))
                for key in ("memory", "load", "disks", "top_processes"):
                    if res.get(key):
                        parts.append("\n[%s]\n%s" % (key, res.get(key)))
                parts.extend(_device_report_lines(res))
                content_text = "\n".join(parts) if parts else json.dumps(res, ensure_ascii=False, indent=2)
        elif name == "system_vitals":
            res = await call_remote_tool(user, name, args)
            if "error" in res:
                is_error = True
                content_text = f"Error: {res.get('error')}"
            else:
                content_text = json.dumps(res, ensure_ascii=False, indent=2)
        elif name in ("bash_exec", "list_dir", "read_file"):
            refusal = ""
            if name == "bash_exec":
                kind = classify_command(args.get("command"))
                if kind:
                    # Installs and deletions only run through system_change, which the
                    # client confirms; this tool is advertised as read-only.
                    refusal = command_confirmation_message(args.get("command", ""), kind, "bash_exec")
                else:
                    # Gateway-level guard: call_remote_tool gives up after 28s, so never
                    # forward a bash timeout longer than 25s (the node defaults to 25).
                    timeout_sec = args.get("timeout_sec")
                    if isinstance(timeout_sec, (int, float)) and timeout_sec > 25:
                        args = dict(args)
                        args["timeout_sec"] = 25
            if refusal:
                is_error = True
                content_text = refusal
            else:
                res = await call_remote_tool(user, name, args)
                content_text, is_error = format_command_result(res, name)
        elif name in ("write_file", "edit_file"):
            if is_system_path(args.get("path")):
                # The unconfirmed file tools never touch the system: that is what
                # system_write exists for, and the client confirms it.
                is_error = True
                content_text = path_confirmation_message(args.get("path", ""), name)
                res = None
            else:
                res = await call_remote_tool(user, name, args)
            if res is not None:
                err = remote_tool_error(res)
                if err:
                    is_error = True
                    content_text = f"[Error] {err}"
                else:
                    path = res.get("path", args.get("path", ""))
                    sha = res.get("sha256", "")
                    if name == "write_file":
                        content_text = f"OK: wrote {res.get('bytes', 0)} bytes to {path} (sha256={sha})"
                    else:
                        content_text = f"OK: edited {path} ({res.get('replacements', 0)} replacement(s), sha256={sha})"
        elif name == "system_write":
            # Confirmed twin of write_file for paths inside the system. The node only
            # ever sees write_file, so this works with every deployed node version.
            res = await call_remote_tool(user, "write_file", args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                path = res.get("path", args.get("path", ""))
                sha = res.get("sha256", "")
                content_text = (
                    f"OK: wrote {res.get('bytes', 0)} bytes to {path} (sha256={sha}) "
                    f"(confirmed system write)"
                )
        elif name == "grep_search":
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                matches = res.get("matches") or []
                if not matches:
                    content_text = "No matches."
                else:
                    lines = []
                    for m in matches:
                        if not isinstance(m, dict):
                            continue
                        lines.append(f"{m.get('path')}:{m.get('line')}: {m.get('text')}")
                    content_text = "\n".join(lines) if lines else "No matches."
                    if res.get("truncated"):
                        content_text += f"\n\n(truncated: showing first {len(lines)} matches; narrow the pattern or raise limit)"
        elif name == "glob_find":
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                files = res.get("files") or []
                content_text = "\n".join(str(f) for f in files) if files else "No files found."
                if res.get("truncated"):
                    content_text += f"\n\n(truncated: showing first {len(files)} paths)"
        elif name == "run_job":
            kind = classify_command(args.get("command"))
            if kind:
                # Same rule as bash_exec: the unconfirmed tools never install or
                # delete anything, not even in the background.
                is_error = True
                content_text = command_confirmation_message(args.get("command", ""), kind, "run_job")
            else:
                res = await call_remote_tool(user, name, args)
                err = remote_tool_error(res)
                if err:
                    is_error = True
                    content_text = f"[Error] {err}"
                else:
                    content_text = format_job_started(res, args)
        elif name == "system_change":
            # The confirmed twin of bash_exec/run_job. The client asks the user
            # before this call because the tool is advertised destructiveHint=true;
            # the node itself only ever sees bash_exec or run_job, so this works with
            # every node version, including payloads older than this gateway.
            background = _wants_background(args)
            forwarded = dict(args)
            forwarded.pop("background", None)
            if background:
                forwarded.pop("timeout_sec", None)
                forwarded.pop("max_chars", None)
                forwarded.pop("cursor", None)
                remote_name = "run_job"
            else:
                # bash_exec has no cwd of its own; the command runs in the node's
                # workspace unless the caller asked for a background job.
                forwarded.pop("cwd", None)
                timeout_sec = forwarded.get("timeout_sec")
                if isinstance(timeout_sec, (int, float)) and timeout_sec > 25:
                    forwarded["timeout_sec"] = 25
                remote_name = "bash_exec"
            res = await call_remote_tool(user, remote_name, forwarded)
            if remote_name == "run_job":
                err = remote_tool_error(res)
                if err:
                    is_error = True
                    content_text = f"[Error] {err}"
                else:
                    content_text = format_job_started(res, forwarded)
            else:
                content_text, is_error = format_command_result(res, remote_name)
        elif name == "job_output":
            # Gateway-level guard: call_remote_tool gives up after 28s, keep wait below it.
            wait_ms = args.get("wait_ms")
            if isinstance(wait_ms, (int, float)) and wait_ms > 20000:
                args = dict(args)
                args["wait_ms"] = 20000
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                parts = [f"[status={res.get('status')} exit_code={res.get('exit_code')} duration={res.get('duration')}]"]
                stdout = res.get("stdout") or ""
                stderr = res.get("stderr") or ""
                if stdout:
                    parts.append(stdout)
                if stderr:
                    parts.append(f"[STDERR]\n{stderr}")
                if res.get("next_cursor") is not None:
                    parts.append(f"... output truncated; continue with cursor={res.get('next_cursor')}")
                content_text = "\n\n".join(parts)
        elif name == "job_kill":
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                content_text = f"Job {args.get('job_id', '')}: status={res.get('status')} (ok={res.get('ok')})"
        elif name == "job_list":
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                jobs = res.get("jobs") or []
                if not jobs:
                    content_text = "No jobs."
                else:
                    lines = ["job_id | status | exit_code | duration | started_at | command"]
                    for j in jobs:
                        if not isinstance(j, dict):
                            continue
                        lines.append(
                            "{job_id} | {status} | {exit_code} | {duration} | {started_at} | {command}".format(
                                job_id=j.get("job_id", ""),
                                status=j.get("status", ""),
                                exit_code=j.get("exit_code"),
                                duration=j.get("duration"),
                                started_at=j.get("started_at", ""),
                                command=j.get("command", ""),
                            )
                        )
                    content_text = "\n".join(lines)
        elif name in ("share_file", "serve_dir"):
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                content_text = (
                    "Published. Public link: {url}\n"
                    "Anyone with this link can read it - the random part of the path is the credential. "
                    "Revoke it with unshare(name=\"{name}\") when it is no longer needed.\n"
                    "Local URL on the node: {local}"
                ).format(url=res.get("url", ""), name=res.get("name", ""),
                         local=res.get("local_url", ""))
        elif name == "share_list":
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                shares = res.get("shares") or []
                if not shares:
                    content_text = "No active shares."
                else:
                    lines = ["kind | name | url | running"]
                    for item in shares:
                        if isinstance(item, dict):
                            lines.append("{kind} | {name} | {url} | {running}".format(
                                kind=item.get("kind", ""), name=item.get("name", ""),
                                url=item.get("url", ""), running=item.get("running")))
                    content_text = "\n".join(lines)
        elif name == "unshare":
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                content_text = (
                    "Revoked share '{name}' ({slug}). Server stopped={stopped}, files removed={removed}."
                ).format(name=res.get("name", ""), slug=res.get("slug", ""),
                         stopped=res.get("server_stopped"), removed=res.get("files_removed"))
        elif name in DEVICE_TOOLS:
            res = await call_remote_tool(user, name, args)
            err = remote_tool_error(res)
            if err:
                is_error = True
                content_text = f"[Error] {err}"
            else:
                content_text = _format_device_result(name, res)
        else:
            resp["error"] = {"code": -32601, "message": f"Unknown tool: {name}"}
            broadcast_sse(user, resp)
            session_id = resolve_session_id(request, user)
            return JSONResponse(
                resp,
                headers={
                    "mcp-session-id": session_id,
                    "mcp-protocol-version": protocol_version_for(user),
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
                }
            )

        # Size guard for the Gemini Web turn budget. The node already paginates
        # via next_cursor, so the gateway must NOT cut the middle out: that would
        # break byte-exact continuation. Keep the head and say how to get the
        # rest, so the model can ask for the next page instead of losing data.
        MAX_GATEWAY_TEXT = 45000
        if len(content_text) > MAX_GATEWAY_TEXT:
            kept = content_text[:MAX_GATEWAY_TEXT]
            omitted = len(content_text) - MAX_GATEWAY_TEXT
            content_text = (
                f"{kept}\n\n... [Output truncated by the gateway: {omitted} more characters. "
                f"Do not re-run the command; the node can page the rest: call the same tool again "
                f"with cursor=<next_cursor> and a larger max_chars, or read the saved file if the "
                f"result reported one.] ..."
            )

        resp["result"] = {
            "content": [{"type": "text", "text": content_text}],
            "isError": is_error
        }
    elif method == "prompts/list":
        resp["result"] = {
            "prompts": [{"name": "rhm-orchestrator", "description": f"Orchestrator role for {user}"}]
        }
    elif method == "prompts/get":
        skill_text = get_skill(user, request.headers.get("host", PUBLIC_HOST))
        resp["result"] = {
            "description": "Remote Harness Orchestrator Persona",
            "messages": [{"role": "user", "content": {"type": "text", "text": skill_text}}]
        }
    elif method == "resources/list":
        resp["result"] = {
            "resources": [{
                "uri": "resource://skills/orchestrator.md",
                "name": "orchestrator.md",
                "mimeType": "text/markdown"
            }]
        }
    elif method == "resources/read":
        uri = params.get("uri")
        if uri == "resource://skills/orchestrator.md":
            text = get_skill(user, request.headers.get("host", PUBLIC_HOST))
            resp["result"] = {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}
        else:
            resp["error"] = {"code": -32602, "message": f"Resource not found: {uri}"}
            broadcast_sse(user, resp)
            session_id = resolve_session_id(request, user)
            return JSONResponse(
                resp,
                headers={
                    "mcp-session-id": session_id,
                    "mcp-protocol-version": protocol_version_for(user),
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
                }
            )
    else:
        resp["error"] = {"code": -32601, "message": f"Method not found: {method}"}

    # Delivery channel. The answer always goes in the HTTP body; duplicating it on
    # the caller's stream is what makes a strict client see one response id twice
    # and drop the session. Only the legacy transports (POST /messages, or
    # LEGACY_SSE which answers 202 with no body) are delivered on the stream.
    path = request.url.path
    if classic_stream is not None:
        # Classic SSE receives each response on exactly its own GET stream.
        classic_stream.put_nowait(json.dumps(resp, ensure_ascii=False))
        return Response(status_code=202)
    if should_broadcast_response(path):
        broadcast_sse(user, resp)
    else:
        logger.info(f"response [{method} id={req_id}] answered in the POST body only "
                    f"(no stream duplicate)")

    if method == "tools/call":
        # Duration matters: Gemini's frontend gives a tool call roughly 30 s, so a
        # slow command shows up here before it shows up as a client-side timeout.
        logger.info(f"tools/call {params.get('name')} took {time.monotonic() - call_started:.2f}s")

    # Return response in HTTP body (for Streamable HTTP clients) with Mcp-Session-Id header.
    # A client that asks ONLY for text/event-stream must get an SSE-framed body:
    # a strict streamable-HTTP client rejects application/json even when the
    # payload itself is correct.
    # The id we issued at initialize must be the one the client sees here, or the
    # client believes it holds a different session than the one it opened.
    session_id = issued_session_id or resolve_session_id(request, user)
    headers = {
        "mcp-session-id": session_id,
        "mcp-protocol-version": protocol_version_for(user),
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
    }
    wants_sse_only = ("text/event-stream" in accept_hdr) and ("application/json" not in accept_hdr)
    if wants_sse_only:
        payload = "event: message\ndata: %s\n\n" % json.dumps(resp, ensure_ascii=False)
        headers["Cache-Control"] = "no-cache"
        return Response(payload, media_type="text/event-stream", headers=headers)

    if (LEGACY_SSE and method != "initialize"
            and "text/event-stream" in accept_hdr and active_sse_subscribers.get(user)):
        # The client is holding a stream and reads the answer there, so a body
        # would be a second copy of the same response. 202 Accepted with no body
        # is what the legacy SSE transport prescribes; the broadcast above has
        # already queued the message on the stream.
        #
        # `initialize` is deliberately excluded: with it answered 202 the client
        # never learns the session id or the negotiated version, and the observed
        # result was a pure initialise/DELETE loop with no tool call at all.
        logger.info(f"legacy-SSE mode: answering {method} with 202, result goes to the stream")
        return Response(status_code=202, headers=headers)

    return JSONResponse(resp, headers=headers)

async def mcp_unified_endpoint(request: Request):
    if request.method == "OPTIONS":
        user = get_target_user(request)
        return Response(
            status_code=204,
            headers={
                "Allow": "GET, POST, DELETE, OPTIONS, HEAD",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS, HEAD",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
                "mcp-protocol-version": protocol_version_for(user),
            }
        )
    if request.method == "DELETE":
        ok, msg, user = verify_token(request)
        if not ok:
            return JSONResponse(
                {"error": "unauthorized", "message": msg},
                status_code=401,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
                    "mcp-protocol-version": protocol_version_for(user),
                }
            )
        client_ip = request.client.host if request.client else "unknown"
        session_id = resolve_session_id(request, user)
        logger.info(f"Session teardown (DELETE) from {client_ip} [{user}] session_id={session_id[:12]}…")

        # Forget the session we issued for this connection. Streams live under
        # their own per-connection key, so a teardown can never close a stream
        # that another in-flight request is still reading.
        active_sessions.pop(session_id, None)

        # Clean up session queue if active. The lookup stays keyed per connection
        # (see above): a DELETE closes the stream whose key it names, never
        # another connection's. The queue is unregistered from the per-user list
        # and the session index here as well, so /health stays truthful even if
        # the stream generator never resumes to run its own cleanup.
        if session_id in active_sse_sessions:
            q = active_sse_sessions.pop(session_id, None)
            if q is not None:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
                forget_sse_queue(user, q)

        return Response(
            status_code=204,
            headers={
                "mcp-session-id": session_id,
                "mcp-protocol-version": protocol_version_for(user),
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS, HEAD",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
            }
        )
    if request.method in ("GET", "HEAD"):
        path = request.url.path
        if path == "/messages":
            ok, msg, user = verify_token(request)
            if not ok:
                return JSONResponse(
                    {"error": "unauthorized", "message": msg},
                    status_code=401,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
                        "mcp-protocol-version": protocol_version_for(user),
                    }
                )
            accept = (request.headers.get("accept") or "").lower()
            client_ip = request.client.host if request.client else "unknown"
            logger.info(
                f"GET /messages from {client_ip} [{user}] accept={accept[:60]!r} "
                f"-> {'sse-stream' if 'text/event-stream' in accept else 'json-ready'}"
            )
            # Some MCP clients (Gemini Spark among them) open the message endpoint
            # itself as the server->client stream instead of /sse. Answering that
            # with a small JSON body closes the stream immediately, which makes the
            # client drop the session and re-initialise in a loop. Serve a real SSE
            # stream when the caller asks for one, and keep the JSON readiness probe
            # for plain health checks.
            if "text/event-stream" in accept:
                return await sse_endpoint(request)
            return JSONResponse({
                "status": "ready",
                "endpoint": "messages",
                "user": user
            }, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
                "mcp-protocol-version": protocol_version_for(user)
            })
        return await sse_endpoint(request)
    if request.method == "POST":
        return await messages_endpoint(request)
    return Response(
        status_code=405,
        headers={
            "Allow": "GET, POST, DELETE, OPTIONS, HEAD",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version",
            "mcp-protocol-version": protocol_version_for(user),
        }
    )

async def ws_tunnel_endpoint(websocket: WebSocket):
    query = websocket.query_params
    user = query.get("user", "").lower()
    registry = load_registry()
    entry = registry.get(user) if user else None

    # The tunnel is the machine's own identity: it accepts the DEVICE TOKEN only,
    # never a link key (a link that leaks must not let someone attach a fake
    # agent to that node name). The header is preferred; the query form is kept
    # for agents released before 0.4.6, which have the wss URL in their service
    # config and would otherwise need a reinstall to come back online.
    header = (websocket.headers.get("x-mesh-token") or "").strip()
    if not header:
        auth = websocket.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            header = auth[7:].strip()
    token = header or query.get("token", "")

    if entry is None or not (token_matches_device(entry, token) or token_matches_next(entry, token)):
        await websocket.close(code=4001, reason="Unauthorized")
        return
    # A tunnel that presents the pending token is the node saying "my config holds
    # the new secret now": that is the moment the rotation completes and the old
    # token dies. Doing it here, rather than when the rotation was requested,
    # means a node that fails to restart keeps a working tunnel instead of
    # becoming unreachable.
    if token_matches_next(entry, token) and not token_matches_device(entry, token):
        promote_device_token(user, registry, entry)
    if not header:
        logger.warning(
            "Agent '%s' opened its tunnel with ?token= in the URL; update the node "
            "to send 'Authorization: Bearer' instead.", user,
        )

    # Cleanly terminate any prior websocket and fail pending futures for this user
    old_tunnel = active_tunnels.get(user)
    if old_tunnel:
        for r_id, fut in list(old_tunnel.get("pending", {}).items()):
            if not fut.done():
                fut.set_exception(ConnectionResetError("Tunnel reconnected"))
        if old_tunnel.get("ws"):
            try:
                await old_tunnel["ws"].close(code=1000, reason="Superceded by new connection")
            except Exception:
                pass

    await websocket.accept()
    logger.info(f"Agent tunnel connected for user '{user}'")
    tunnel_data = {"ws": websocket, "pending": {}}
    active_tunnels[user] = tunnel_data
    # The agent is connected: this is the timestamp /health reports as
    # last_tunnel_seen, and the event that tells a reboot apart from a flap.
    record_tunnel_event("connect", user, "agent connected")

    disconnect_reason = "tunnel closed"
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            req_id = msg.get("id")
            if req_id and req_id in tunnel_data["pending"]:
                fut = tunnel_data["pending"].pop(req_id)
                if not fut.done():
                    fut.set_result(msg)
    except WebSocketDisconnect:
        disconnect_reason = "websocket disconnected"
        logger.info(f"Agent tunnel disconnected for user '{user}'")
    except Exception as exc:
        disconnect_reason = f"tunnel error: {type(exc).__name__}"
        logger.warning(f"Agent tunnel error for user '{user}': {exc}")
        raise
    finally:
        if active_tunnels.get(user, {}).get("ws") == websocket:
            active_tunnels.pop(user, None)
            # Only the tunnel that is still the active one is reported here: if
            # the watchdog already removed it as stale, or a newer connection
            # replaced it, that path has its own event and this one is noise.
            record_tunnel_event("disconnect", user, disconnect_reason)


# ---------------------------------------------------------------------------
# Public file shares: GET/HEAD <shared-domain>/<node>/<name>-<secret>/...
# ---------------------------------------------------------------------------
# The node runs a loopback-only static server (core/web_share.py) and this route
# relays the request over the existing tunnel; no port is ever opened on the
# node. Only read methods are proxied, the 16-hex suffix is the share secret and
# a node that is not connected fails fast with 503 instead of blocking on the
# tunnel retry loop. The slug shape is duplicated from core/web_share.py on
# purpose: the gateway deploys as a single file and must not import node code.
SHARE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}-[0-9a-f]{16}$")
SHARE_METHODS = ("GET", "HEAD")
# The node is trusted, but a corrupted or buggy reply must not be able to inject
# response headers. An allow-list (not a deny-list) also keeps a compromised node
# from setting cookies on the shared domain or adding headers this route never
# means to expose; control characters are stripped and values are forced through
# latin-1, which is what Starlette encodes headers with.
_RELAY_HEADERS = (
    "content-type", "content-disposition", "last-modified", "etag",
    "cache-control", "location", "accept-ranges", "content-length",
)
_HEADER_VALUE_RE = re.compile(r"[\x00-\x1f\x7f]")
_CONTENT_LENGTH_RE = re.compile(r"^\d{1,12}$")


def _relay_headers(raw) -> dict:
    headers = {}
    if not isinstance(raw, dict):
        return headers
    for key, value in raw.items():
        name = str(key).strip().lower()
        if name not in _RELAY_HEADERS:
            continue
        text = _HEADER_VALUE_RE.sub("", str(value))[:4096]
        text = text.encode("latin-1", "replace").decode("latin-1")
        if name == "location" and not text.startswith("/"):
            continue  # a redirect must stay inside this domain's share namespace
        headers[name] = text
    return headers


# ---------------------------------------------------------------------------
# Command routing policy. This is a convenience classifier, not a sandbox.
# ---------------------------------------------------------------------------
# All commands are advertised as writes. The client controls approvals and the
# agent's OS user/sandbox enforces permissions. The classifier routes common
# system operations to their explicit tools:
#
#   install / delete   software is installed or removed, data is deleted
#                      -> system_change
#   sudo               the command escalates privileges (sudo/doas/pkexec/runas)
#                      -> system_change
#   system             a system path or the registry is modified
#                      -> system_change (shell) or system_write (file content)
#
# The shell check is deliberately conservative: a verb has to sit at the start of a
# command segment, after stripping sudo/env/timeout/`powershell -Command`
# prefixes, so `echo "rm -rf /"`, `grep rm notes.txt` and `git log --grep install`
# all still run. It is a UX gate, not a sandbox - MESH_READ_ONLY on the node is the
# hard boundary.
_SEGMENT_RE = re.compile(r"[;&|\n]+")
_PREFIX_RE = re.compile(
    r"^\s*(?:(?:sudo|doas|command|nohup|env|time|nice|"
    r"timeout\s+\d+(?:\.\d+)?|"
    r"(?:powershell|pwsh)(?:\.exe)?(?:\s+-[\w:]+)*|"
    r"cmd(?:\.exe)?\s+/[cCkK]|"
    r"&\s+|start\s+)\s+|\w+=\S+\s+)*"
)
INSTALL_COMMAND_PATTERNS = (
    r"apt(?:-get)?\s+(?:install|remove|purge|autoremove|dist-upgrade|full-upgrade)\b",
    r"aptitude\s+(?:install|remove|purge)\b",
    r"dnf\s+(?:install|remove|erase|update|upgrade|autoremove|groupinstall)\b",
    r"yum\s+(?:install|remove|erase|update|upgrade|groupinstall)\b",
    r"zypper\s+(?:install|remove|rm|update|up|dup|patch)\b",
    r"pacman\s+-[A-Za-z]*[SR][A-Za-z]*\b",
    r"apk\s+(?:add|del|delete)\b",
    r"brew\s+(?:install|uninstall|remove|upgrade|reinstall)\b",
    r"(?:pip|pip3|pipx)\s+(?:install|uninstall)\b",
    r"(?:python|python3|py)\s+-m\s+pip\s+(?:install|uninstall)\b",
    r"(?:npm|pnpm|yarn)\s+(?:install|i|ci|add|uninstall|remove|rm|up|upgrade)\b",
    r"winget\s+(?:install|uninstall|upgrade)\b",
    r"(?:choco|scoop)\s+(?:install|uninstall|upgrade|remove)\b",
    r"(?:snap|flatpak)\s+(?:install|remove|uninstall|refresh|update)\b",
    r"(?:gem|cargo|dotnet)\s+(?:install|uninstall|tool\s+(?:install|uninstall)|"
    r"add\s+package|remove\s+package)\b",
    r"go\s+install\b",
    r"msiexec(?:\.exe)?\s+/[ix]\b",
    r"dpkg\s+-[A-Za-z]*[irP][A-Za-z]*\b",
    r"rpm\s+-[A-Za-z]*[ieU][A-Za-z]*\b",
    r"(?:install|uninstall)-(?:module|package|script|psresource|windowsfeature)\b",
    r"(?:add|remove)-(?:appxpackage|windowsoptionalfeature|windowsfeature|"
    r"provisionedappxpackage)\b",
    r"\b(?:iex|invoke-expression)\b",
)
DELETE_COMMAND_PATTERNS = (
    r"rm\s+(?:-\S+\s+)*\S",
    r"(?:rmdir|rd|unlink|shred|srm)\s+\S",
    r"(?:del|erase)\s+\S",
    r"deltree\b",
    r"remove-item\b",
    r"ri\s+(?:-\S+\s+)*\S",
    r"clear-content\b",
    r"remove-itemproperty\b",
    r"(?:format-volume|clear-disk|remove-partition)\b",
    r"(?:mkfs(?:\.\w+)?|wipefs|blkdiscard|fdisk|parted)\b",
    r"dd\s+",
    r"truncate\s+-s\s*0\b",
    r"git\s+clean\s+-\S*[fdx]",
    r"git\s+rm\b",
    r"docker\s+(?:rm|rmi)\b",
    r"docker\s+(?:system|volume|image|container|builder)\s+prune\b",
    r"kubectl\s+delete\b",
    r"vssadmin\s+delete\b",
    r"bcdedit\s+/delete\b",
    r"(?:userdel|groupdel|deluser|delgroup)\b",
)
_INSTALL_RE = re.compile("|".join(INSTALL_COMMAND_PATTERNS), re.IGNORECASE)
_DELETE_RE = re.compile("|".join(DELETE_COMMAND_PATTERNS), re.IGNORECASE)
# `curl … | sh` is the installer idiom and never looks like a package manager.
_PIPE_TO_SHELL_RE = re.compile(
    r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b", re.IGNORECASE
)
# Privilege escalation: the operator asked to be asked whenever the agent reaches
# for root, whatever the command does with it.
_PRIVILEGE_RE = re.compile(
    r"(?:^|[;&|\n])\s*(?:sudo|doas|gsudo|pkexec|runas)\b"
    r"|\bsu\s+-\S"
    r"|\bStart-Process\b[^\n]*?-Verb\s+RunAs",
    re.IGNORECASE,
)
# Paths that belong to the system, not to the user's work: the operator asked to be
# asked before these are modified. Reads (`cat /etc/os-release`, `ls /usr/bin`) are
# not gated - only a mutation of them is.
SYSTEM_PATH_RE = re.compile(
    r"(?:"
    r"/(?:etc|usr|bin|sbin|lib|lib64|boot|dev|proc|sys|opt|root)(?:/|\b)"
    r"|/var/(?:lib|log|spool|cache|www)(?:/|\b)"
    r"|[A-Za-z]:\\+(?:Windows|ProgramData)(?:\\|\b)"
    r"|[A-Za-z]:\\+Program Files(?:\s*\(x86\))?(?:\\|\b)"
    r"|%(?:SystemRoot|ProgramFiles|ProgramData|windir)%"
    r"|HKEY_LOCAL_MACHINE|HKLM\\+"
    r")",
    re.IGNORECASE,
)
# What "modifies" means for the test above.
SYSTEM_MUTATION_PATTERNS = (
    r"rm|rmdir|unlink|shred|mv|cp|install|chmod|chown|chgrp|mkdir|rmdir|touch|ln|"
    r"truncate|dd|mount|umount|tee|sed\s+-i|patch|dpkg|rpm|apt(?:-get)?|"
    r"(?:systemctl|service)\s+(?:start|stop|restart|reload|enable|disable|mask|unmask|"
    r"daemon-reload|set-property)|"
    r"crontab|visudo|passwd|user(?:add|mod|del)|group(?:add|mod|del)|"
    r"reg\s+(?:add|delete|import|copy|restore|save|load|unload)|"
    r"sc\s+(?:config|create|delete|start|stop)|netsh|schtasks|icacls|takeown|attrib|"
    r"new-item|set-content|add-content|out-file|remove-item|move-item|copy-item|"
    r"rename-item|new-itemproperty|set-itemproperty|clear-content|set-acl|"
    r"(?:start|stop|restart|new|set|remove)-service"
)
_SYSTEM_MUTATION_RE = re.compile(
    r"(?<![\w.-])(?:%s)(?![\w.-])" % SYSTEM_MUTATION_PATTERNS, re.IGNORECASE
)
# A redirection that writes straight into a system path.
_REDIRECT_TO_SYSTEM_RE = re.compile(
    r">>?\s*[\"']?(?:"
    r"/(?:etc|usr|bin|sbin|lib|lib64|boot|dev|proc|sys|opt|root)(?:/|\b)"
    r"|/var/(?:lib|log|spool|cache|www)(?:/|\b)"
    r"|[A-Za-z]:\\+(?:Windows|ProgramData)(?:\\|\b)"
    r"|[A-Za-z]:\\+Program Files(?:\s*\(x86\))?(?:\\|\b)"
    r")",
    re.IGNORECASE,
)
_TRUE_VALUES = ("1", "true", "yes", "on")


def _wants_background(args) -> bool:
    """True when the caller asked for system_change in the background."""
    if not isinstance(args, dict):
        return False
    value = args.get("background")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE_VALUES


def classify_command(command) -> str:
    """Return "install", "delete", "sudo", "system" or "" for a shell command.

    "" means the command may run in the unconfirmed tools; anything else has to go
    through system_change, where the client asks the user first. The most specific
    reason wins, so `sudo apt install x` reports the install, not the sudo.
    """
    if not isinstance(command, str) or not command.strip():
        return ""
    if _PIPE_TO_SHELL_RE.search(command):
        return "install"
    for segment in _SEGMENT_RE.split(command):
        stripped = _PREFIX_RE.sub("", segment).strip().lstrip("\"'`").lstrip()
        if not stripped:
            continue
        if _INSTALL_RE.match(stripped):
            return "install"
        if _DELETE_RE.match(stripped):
            return "delete"
    if _PRIVILEGE_RE.search(command):
        return "sudo"
    if SYSTEM_PATH_RE.search(command) and (
            _SYSTEM_MUTATION_RE.search(command) or _REDIRECT_TO_SYSTEM_RE.search(command)):
        return "system"
    return ""


def is_system_path(path) -> bool:
    """True when *path* points into the system rather than the user's own files."""
    if not isinstance(path, str) or not path.strip():
        return False
    return bool(SYSTEM_PATH_RE.search(path.strip()))


def command_confirmation_message(command: str, kind: str, tool: str) -> str:
    """The refusal a model gets instead of running a gated command unconfirmed."""
    what = {
        "install": "installs or removes software",
        "delete": "deletes data",
        "sudo": "runs with elevated privileges",
        "system": "changes a system path",
    }.get(kind, "is a system change")
    return (
        f"[Confirmation required] This command {what}, so it was not run.\n"
        f"Gateway policy routes this operation through system_change. "
        f"Call system_change with the same command and respect client approval settings.\n"
        f"command: {command.strip() if isinstance(command, str) else command}\n"
        f"Do not rephrase, split, encode or otherwise obfuscate the command to get around "
        f"this; route it through system_change."
    )


def path_confirmation_message(path: str, tool: str) -> str:
    """The refusal for write_file/edit_file aimed at a system path."""
    return (
        f"[Confirmation required] {path} is a system path, so it was not written.\n"
        f"Gateway policy requires system_write(path=..., content=...) for this path. "
        f"Respect client approval settings.\n"
        f"path: {path}"
    )


def format_command_result(res, name: str):
    """(text, is_error) for a bash_exec / system_change / list_dir / read_file reply."""
    if "error" in res:
        return (f"[Execution Error]: {res.get('error')}", True)
    is_error = False
    exit_code = res.get("exit_code", 0)
    # Never strip: a paginated chunk must survive byte-exact so the
    # client can rebuild the output by following next_cursor.
    stdout = res.get("stdout", "")
    stderr = res.get("stderr", "")
    if exit_code != 0:
        is_error = True
        # A non-zero exit code is the command's own result, not a
        # transport failure: the command RAN on the host. Without this
        # wording a model reads isError=true as "the node is
        # unreachable" and stops looking for the real output.
        parts = [
            f"[Exit code: {exit_code}] The command ran on the host and the node "
            f"is reachable; {exit_code} is the command's own exit status."
        ]
        if stderr.strip():
            parts.append(stderr)
        if stdout.strip():
            parts.append(stdout)
        content_text = "\n".join(parts) if parts else f"[Exit code: {exit_code}]"
    else:
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[STDERR]\n{stderr}")
        content_text = "\n\n".join(parts) if parts else "(Command executed successfully with no output)"
    # Surface node-side pagination/spooling so the model knows there is more.
    if isinstance(res, dict):
        if res.get("next_cursor") is not None:
            content_text += (
                f"\n\n[output continues on the node: call {name} again with "
                f"cursor={res.get('next_cursor')} (optionally a larger max_chars)]"
            )
        if res.get("saved_to"):
            content_text += f"\n\n[full output saved on the node at {res['saved_to']}]"
    return (content_text, is_error)


def format_job_started(res, args) -> str:
    """The reply for run_job and for system_change(background=true)."""
    job_id = res.get("job_id", "")
    return (
        f"Started job {job_id} (pid {res.get('pid')})\n"
        f"command: {res.get('command', (args or {}).get('command', ''))}\n"
        f"Use job_output(job_id=\"{job_id}\") to poll progress; "
        f"use job_kill(job_id=\"{job_id}\") to stop it."
    )


# ---------------------------------------------------------------------------
# MCP annotations describe effects truthfully; they are not a permission bypass.
# ---------------------------------------------------------------------------
_ANNOT_READ_ONLY = {
    "readOnlyHint": True, "destructiveHint": False,
    "idempotentHint": True, "openWorldHint": False,
}
_ANNOT_UNCONFIRMED_WRITE = {
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": False, "openWorldHint": False,
}
_ANNOT_PUBLISH = dict(_ANNOT_UNCONFIRMED_WRITE, openWorldHint=True)
_ANNOT_DESTRUCTIVE = dict(_ANNOT_UNCONFIRMED_WRITE, destructiveHint=True)

TOOL_ANNOTATIONS = {
    # read-only: looking around never needs a prompt
    "mesh_status": _ANNOT_READ_ONLY,
    "system_info": _ANNOT_READ_ONLY,
    "system_vitals": _ANNOT_READ_ONLY,
    "get_orchestration_skill": _ANNOT_READ_ONLY,
    "list_dir": _ANNOT_READ_ONLY,
    "read_file": _ANNOT_READ_ONLY,
    "grep_search": _ANNOT_READ_ONLY,
    "glob_find": _ANNOT_READ_ONLY,
    "job_output": _ANNOT_READ_ONLY,
    "job_list": _ANNOT_READ_ONLY,
    "share_list": _ANNOT_READ_ONLY,
    "device_info": _ANNOT_READ_ONLY,
    "bash_exec": dict(_ANNOT_DESTRUCTIVE, openWorldHint=True),
    "run_job": dict(_ANNOT_DESTRUCTIVE, openWorldHint=True),
    "write_file": _ANNOT_UNCONFIRMED_WRITE,
    "edit_file": _ANNOT_UNCONFIRMED_WRITE,
    "job_kill": _ANNOT_UNCONFIRMED_WRITE,
    "device_control": _ANNOT_UNCONFIRMED_WRITE,
    "device_capture": _ANNOT_UNCONFIRMED_WRITE,
    "device_messages": dict(_ANNOT_UNCONFIRMED_WRITE, openWorldHint=True),
    "share_file": _ANNOT_PUBLISH,
    "serve_dir": _ANNOT_PUBLISH,
    "unshare": _ANNOT_UNCONFIRMED_WRITE,
    "system_change": _ANNOT_DESTRUCTIVE,
    "system_write": _ANNOT_DESTRUCTIVE,
    "mesh_update": _ANNOT_DESTRUCTIVE,
}


def annotate_tools(tools):
    """Return *tools* with the confirmation policy attached to each entry."""
    annotated = []
    for tool in tools:
        profile = TOOL_ANNOTATIONS.get(tool.get("name"))
        if profile is None:
            annotated.append(tool)
            continue
        tool = dict(tool)
        tool["annotations"] = dict(profile)
        annotated.append(tool)
    return annotated


# Tool surface advertised for public shares. The gateway keeps its own static
# tools/list (it must not import node code), so this list has to stay in step
# with the node's core/mcp_tools.TOOLS; tests/test_gateway_share_route.py
# compares the two surfaces to catch drift.
SHARE_TOOL_SPECS = [
    {
        "name": "share_file",
        "description": (
            "Publish ONE local file on the internet through the gateway and return a public "
            "HTTPS link (https://<domain>/<node>/<name>-<random>/<file>). Anyone with the link "
            "can read it; revoke it with unshare when it is no longer needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the local file to publish."},
                "name": {"type": "string", "description": "Human name in the URL (default: file name)."},
                "overwrite": {"type": "boolean", "description": "Replace an existing share with the same name."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "serve_dir",
        "description": (
            "Start a static web server for a local directory and return its public HTTPS link "
            "(https://<domain>/<node>/<name>-<random>/). Served in place, read-only (GET/HEAD); "
            "unshare stops it and the directory itself is never deleted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Local directory to serve."},
                "name": {"type": "string", "description": "Human name in the URL (default: directory name)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "share_list",
        "description": "List active public shares with their URLs and whether the local server is running.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "unshare",
        "description": "Stop a public share and revoke its link (accepts the name, slug or full URL).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Share name, slug or public URL from share_list."},
            },
            "required": ["name"],
        },
    },
]


# The node's own device: one relay branch in tools/call, one formatter. The four
# names must match core/mcp_tools.TOOLS exactly (the same parity test as the
# share tools above), and DEVICE_TOOLS is deliberately a tuple so the branch is a
# single membership test instead of four elifs.
#
# core/mcp_tools.TOOLS is the source of truth for the device_* specs in tools/list
# as well. Their descriptions and schemas above are copied from it verbatim; an
# earlier draft of this file carried different prose, which the node's landed
# version superseded. The node executes these calls and the client reads this
# copy, so a wording difference here would be a lie the model acts on.
DEVICE_TOOLS = ("device_info", "device_control", "device_capture", "device_messages")

#: A device report is JSON by contract and ``device_info(section="all")`` can run
#: to megabytes, so the gateway shows the head and names the way to ask for one
#: block by itself - rather than cutting a table in the middle with no way back.
DEVICE_REPORT_LIMIT = 8000


def _device_report_lines(res):
    """Render the device blocks of a ``system_info`` answer as readable lines.

    The gateway used to print a fixed list of the original fields, which silently
    dropped everything the device branch added - the client asked for the battery
    and got a summary without it. Every block is rendered as either its facts or,
    when the platform has no source for it, ``available: false`` with the reason and
    the fix, because "not exposed here" and "0 %" must not look the same to a model.
    """
    if not isinstance(res, dict):
        return []
    lines = []
    hint = res.get("device_hint")
    if hint:
        lines.append("\n[device] %s" % hint)

    def block(name):
        value = res.get(name)
        return value if isinstance(value, dict) else None

    def facts(name, fields, prefix=""):
        value = block(name)
        if value is None:
            return
        if value.get("available") is False:
            line = "%s%s: not available - %s" % (prefix, name, value.get("reason") or "no reason given")
            if value.get("fix"):
                line += " (fix: %s)" % value["fix"]
            lines.append(line)
            return
        shown = ["%s=%s" % (key, value[key]) for key in fields if value.get(key) not in (None, "")]
        if shown:
            lines.append("%s%s: %s" % (prefix, name, ", ".join(shown)))

    device = block("device")
    if device and device.get("available") is not False:
        shown = [device.get(key) for key in ("class", "manufacturer", "model", "android_release")]
        shown = [str(item) for item in shown if item]
        if shown:
            lines.append("\n[device] " + " ".join(shown))
    facts("battery", ("percent", "status", "plugged", "temperature_c", "health",
                      "time_remaining_s", "ac_online"))
    network = block("network")
    if network:
        if network.get("available") is False:
            facts("network", ())
        else:
            if network.get("signal"):
                lines.append("signal: %s" % network["signal"])
            shown = [network.get(key) for key in ("connected_kind", "interface_count",
                                                  "default_route")]
            shown = [str(item) for item in shown if item not in (None, "")]
            if shown:
                lines.append("network: " + ", ".join(shown))
            wifi = network.get("wifi") or {}
            if wifi:
                detail = ["%s=%s" % (key, wifi[key]) for key in ("ssid", "rssi_dbm",
                                                                 "link_speed_mbps", "frequency_mhz")
                          if wifi.get(key) not in (None, "")]
                if detail:
                    lines.append("wifi: " + ", ".join(detail))
            cellular = network.get("cellular") or {}
            if cellular:
                detail = ["%s=%s" % (key, cellular[key])
                          for key in ("operator", "network_type", "signal_dbm", "level",
                                      "roaming", "data_state")
                          if cellular.get(key) not in (None, "")]
                if detail:
                    lines.append("cellular: " + ", ".join(detail))
                if cellular.get("cellinfo_reason"):
                    lines.append("cellular: %s" % cellular["cellinfo_reason"])
    facts("locale", ("language", "region", "encoding"))
    facts("time", ("iso_local", "utc_offset", "timezone", "abbreviation"))
    hardware = block("hardware")
    if hardware and hardware.get("available") is not False:
        cpu = hardware.get("cpu") or {}
        if cpu:
            shown = ["%s=%s" % (key, cpu[key])
                     for key in ("model", "cores", "freq_mhz_max", "temp_c", "load")
                     if cpu.get(key) not in (None, "")]
            if shown:
                lines.append("cpu: " + ", ".join(shown))
        memory = hardware.get("memory") or {}
        if memory:
            shown = ["%s=%s" % (key, memory[key])
                     for key in ("total_mb", "used_mb", "free_mb", "used_pct", "swap_total_mb")
                     if memory.get(key) not in (None, "")]
            if shown:
                lines.append("memory: " + ", ".join(shown))
        thermal = hardware.get("thermal") or []
        if thermal:
            lines.append("thermal: " + ", ".join(
                "%s=%s%s" % (item.get("name"), item.get("value"),
                             item.get("unit") or "") for item in thermal[:6]
                if isinstance(item, dict)))
    storage = block("storage")
    if storage:
        if storage.get("available") is False:
            facts("storage", ())
        else:
            shown = ["%s=%s" % (key, storage[key])
                     for key in ("root", "total_gb", "free_gb", "used_pct", "shared",
                                 "capture_dir")
                     if storage.get(key) not in (None, "")]
            if shown:
                lines.append("storage: " + ", ".join(shown))
            if storage.get("shared_fix"):
                lines.append("storage: %s" % storage["shared_fix"])
    for name, fields in (("cameras", ("count",)), ("microphones", ("count",)),
                         ("sensors", ("count", "sampling"))):
        value = block(name)
        if value is None:
            continue
        if value.get("available") is False:
            facts(name, ())
            continue
        shown = ["%s=%s" % (key, value[key]) for key in fields if value.get(key) not in (None, "")]
        items = value.get(name) or value.get("sensors") or []
        names = []
        for item in items[:4] if isinstance(items, list) else []:
            if isinstance(item, dict):
                label = item.get("name") or item.get("facing") or item.get("id")
                if label not in (None, ""):
                    names.append(str(label))
            elif isinstance(item, str):
                names.append(item)
        text = ", ".join(shown)
        if names:
            text = (text + " (" + ", ".join(names) + ")") if text else ", ".join(names)
        if text:
            lines.append("%s: %s" % (name, text))
    capabilities = block("capabilities")
    if capabilities:
        api = capabilities.get("termux_api") or capabilities.get("api") or {}
        if api:
            lines.append("termux-api: installed=%s" % api.get("installed"))
            if api.get("missing"):
                lines.append("termux-api missing: %s" % ", ".join(api["missing"][:8]))
        if capabilities.get("app_reachable") is False:
            lines.append("termux-api app: not answering - %s"
                         % (capabilities.get("app_fix") or "open the Termux:API app once"))
        permissions = capabilities.get("permissions") or {}
        denied = sorted(name for name, state in permissions.items() if state == "denied")
        if denied:
            lines.append("permissions denied: %s" % ", ".join(denied))
    return lines


def _format_device_result(name, res):
    """Render one ``device_*`` answer for the model.

    "Not available on this node" is deliberately NOT an error: on a Windows or
    Linux laptop a phone-only action is the normal answer, the node reports it as
    ``available: false`` with a reason (and often a fix), and reading that as a
    crash would tell the model the host is broken when it is merely not a phone.
    """
    if not isinstance(res, dict):
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    if "error" in res:
        # The relay already turns a real failure into isError; this keeps the text
        # consistent with the other "[Error] ..." answers if this path is reached.
        return "[Error] " + str(res.get("error"))
    if res.get("available") is False:
        lines = ["%s: not available on this node - %s"
                 % (name, res.get("reason") or "this device does not expose it")]
        if res.get("fix"):
            lines.append("Fix: %s" % res.get("fix"))
        return "\n".join(lines)
    text = json.dumps(res, ensure_ascii=False, indent=2, default=str)
    if len(text) > DEVICE_REPORT_LIMIT:
        text = text[:DEVICE_REPORT_LIMIT] + (
            "\n... [device report truncated; call the tool again with section=<name> for one block]")
    return text


def _share_response(status: int, message: str, extra=None) -> Response:
    headers = {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"}
    if extra:
        headers.update(extra)
    return Response(message + "\n", status_code=status, headers=headers)

async def public_share_endpoint(request: Request) -> Response:
    """Relay one public share request to the node that owns it."""
    user = (request.path_params.get("user") or "").strip().lower()
    rest = (request.path_params.get("rest") or "").strip("/")
    if not user or not rest:
        return _share_response(404, "not found")
    if user not in load_registry():
        return _share_response(404, "not found")

    slug, _, sub_path = rest.partition("/")
    if not SHARE_SLUG_RE.match(slug):
        return _share_response(404, "not found")

    method = request.method.upper()
    if method not in SHARE_METHODS:
        return _share_response(405, "method not allowed", {"allow": "GET, HEAD"})
    if user not in active_tunnels:
        return _share_response(503, "node offline: the mesh agent on '%s' is not connected" % user)

    result = await call_remote_tool(user, "_http_share", {
        "slug": slug,
        "path": "/" + sub_path,
        "method": method,
        "query": request.url.query or "",
    })
    if not isinstance(result, dict) or "status" not in result:
        logger.warning("share relay for '%s' returned an invalid response: %r", user, result)
        return _share_response(502, "bad gateway")
    try:
        status = int(result["status"])
        body = base64.b64decode(result.get("body_b64") or "")
    except Exception:
        return _share_response(502, "bad gateway")
    if not 200 <= status <= 599:
        # 1xx is not a final response: h11 rejects it and the client gets no
        # response at all, so a buggy node reply becomes a clean 502 instead.
        return _share_response(502, "bad gateway")
    headers = _relay_headers(result.get("headers"))
    if status in (204, 304):
        # These statuses must not carry a body or a Content-Length.
        body = b""
        headers.pop("content-length", None)
    elif method == "HEAD":
        # Keep the node's Content-Length - it describes the resource while the
        # body is empty - but only a sane numeric value.
        if not _CONTENT_LENGTH_RE.match(headers.get("content-length", "")):
            headers.pop("content-length", None)
    else:
        # Never trust a declared length: a body that disagrees with
        # Content-Length is a protocol error (h11 aborts the response).
        headers["content-length"] = str(len(body))
    return Response(content=body, status_code=status, headers=headers, media_type=None)


routes = [
    Route("/", health, methods=["GET", "HEAD", "OPTIONS"]),
    Route("/health", health, methods=["GET", "HEAD", "OPTIONS"]),
    Route("/health-mesh", health, methods=["GET", "HEAD", "OPTIONS"]),
    Route("/pair", pairing_page, methods=["GET"]),
    Route("/auth/google/login", owner_login, methods=["GET"]),
    Route("/auth/google/callback", owner_auth.callback, methods=["GET"]),
    Route("/auth/logout", owner_auth.logout, methods=["POST"]),
    Route("/account", owner_account, methods=["GET"]),
    Route("/assets/chatgpt-add-mcp.png", chatgpt_guide_image, methods=["GET"]),
    Route("/account/pair/{code}/approve", owner_pair_approve, methods=["POST"]),
    Route("/account/devices/{user}/{action}", owner_device_action, methods=["POST"]),
    Route("/install.sh", installer_asset, methods=["GET", "HEAD"]),
    Route("/install.ps1", installer_asset, methods=["GET", "HEAD"]),
    Route("/core/{filename}", installer_asset, methods=["GET", "HEAD"]),
    Route("/skills/orchestrator.md", installer_asset, methods=["GET", "HEAD"]),
    Route("/ops/windows/agent-watchdog.ps1", installer_asset, methods=["GET", "HEAD"]),
    Route("/ops/windows/run-hidden.vbs", installer_asset, methods=["GET", "HEAD"]),
    Route("/ops/update.ps1", installer_asset, methods=["GET", "HEAD"]),
    Route("/ops/doctor.ps1", installer_asset, methods=["GET", "HEAD"]),
    Route("/api/register", api_register, methods=["POST", "OPTIONS"]),
    # Device pairing: the join flow that does not hand a token to an anonymous
    # caller. Credentials are issued only after owner/admin confirmation.
    Route("/v1/devices", device_pair_start, methods=["POST", "OPTIONS"]),
    Route("/v1/devices/pair", device_pair_start, methods=["POST", "OPTIONS"]),
    Route("/v1/devices/pair/{code}", device_pair_status, methods=["GET", "OPTIONS"]),
    Route("/v1/devices/pair/{code}/approve", device_pair_approve, methods=["POST", "OPTIONS"]),
    Route("/v1/devices/{user}/rotate", device_rotate, methods=["POST", "OPTIONS"]),
    Route("/v1/devices/{user}/rotate-token", device_rotate_token, methods=["POST", "OPTIONS"]),
    Route("/v1/devices/{user}/rotate-token/finish", device_rotate_token_finish, methods=["POST", "OPTIONS"]),
    Route("/v1/devices/{user}/revoke", device_revoke, methods=["POST", "OPTIONS"]),
    Route("/.well-known/oauth-protected-resource", oauth_discovery, methods=["GET", "HEAD", "OPTIONS"]),
    Route("/.well-known/oauth-protected-resource/{path:path}", oauth_discovery, methods=["GET", "HEAD", "OPTIONS"]),
    Route("/sse", mcp_unified_endpoint, methods=["GET", "POST", "DELETE", "OPTIONS", "HEAD"]),
    Route("/mcp", mcp_unified_endpoint, methods=["GET", "POST", "DELETE", "OPTIONS", "HEAD"]),
    Route("/messages", mcp_unified_endpoint, methods=["GET", "POST", "DELETE", "OPTIONS", "HEAD"]),
    WebSocketRoute("/ws/tunnel", ws_tunnel_endpoint),
    # Public file shares. MUST stay last: it is a catch-all for two-segment
    # paths and must never shadow /sse, /mcp, /health, /api/register or the
    # WebSocket tunnel above.
    Route("/{user}/{rest:path}", public_share_endpoint, methods=["GET", "HEAD", "OPTIONS"]),
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id", "mcp-protocol-version"],
    )
]

async def tunnel_watchdog():
    """Background self-healing loop.

    A websocket can die without a clean close (NAT timeout, host sleep). The entry
    then stays in active_tunnels, so /health reports the node online while every
    call hangs until the 28 s timeout. This loop removes such entries, prunes SSE
    queues nobody consumes and keeps the state truthful.
    """
    logger.info("Self-heal watchdog started (30s interval)")
    while True:
        try:
            await asyncio.sleep(30)
            for user, tunnel in list(active_tunnels.items()):
                ws = tunnel.get("ws")
                state = getattr(getattr(ws, "client_state", None), "name", "UNKNOWN")
                if ws is None or state not in ("CONNECTED",):
                    logger.warning(f"Watchdog: removing stale tunnel for '{user}' (state={state})")
                    for fut in list(tunnel.get("pending", {}).values()):
                        if not fut.done():
                            fut.set_exception(ConnectionResetError("Tunnel was stale"))
                    active_tunnels.pop(user, None)
                    # The stale path is a real disconnect: without this event the
                    # history would show a connect with no matching disconnect
                    # after a host reboot or a NAT timeout.
                    record_tunnel_event("disconnect", user, f"stale tunnel (state={state})")
            # drop subscriber queues that no stream is reading any more. The
            # per-user container is an ordered list, so the surviving queues keep
            # their order (oldest first) for the next cap eviction.
            for user, queues in list(active_sse_subscribers.items()):
                alive = [q for q in queues if not q.empty() or q in active_sse_sessions.values()]
                if alive != queues:
                    active_sse_subscribers[user] = alive
                if not alive:
                    active_sse_subscribers.pop(user, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                 # never let the watchdog die
            logger.warning(f"Watchdog error: {exc}")


@asynccontextmanager
async def lifespan(_app):
    """Start the self-healing watchdog with the app (modern Starlette API)."""
    # All tunnel state belongs to ONE process. Fail closed under --workers N.
    owner_auth.validate_config()
    lock = None
    if os.name == "posix":
        import fcntl
        lock_path = os.environ.get("MESH_GATEWAY_LOCK", REGISTRY_PATH + ".lock")
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
        lock = open(lock_path, "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("Gateway requires exactly one worker per registry: tunnel state is process-local")
    task = asyncio.create_task(tunnel_watchdog())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if lock is not None:
            lock.close()

app = Starlette(debug=False, routes=routes, middleware=middleware, lifespan=lifespan)




if __name__ == "__main__":
    # ws_max_size matters for public file shares: the node sends the base64
    # payload through the tunnel as ONE websocket message, so uvicorn's 16 MiB
    # default silently drops anything above ~12 MiB. 128 MiB carries the node's
    # maximum share size (64 MiB -> ~85 MiB on the wire) with headroom.
    uvicorn.run(app, host="127.0.0.1", port=8096, log_level="info",
                timeout_graceful_shutdown=2, ws_max_size=128 * 1024 * 1024)
