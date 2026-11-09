"""core/agent.py - Remote Harness MCP tunnel node (thin transport).

All tool logic lives in :mod:`core.mcp_tools`; this module only maintains the
websocket tunnel to the gateway and forwards ``tools/call`` requests.  The
response shape is unchanged (``stdout``/``stderr``/``exit_code`` and friends),
so an unmodified gateway keeps working.

Configuration (read once at import/startup)::

    MESH_GATEWAY      shared gateway domain
    MESH_USER         node name
    MESH_TOKEN        node token
    MESH_CONFIG_FILE  KEY=VALUE file used for any of the three above that is not
                      set in the environment (default:
                      ~/.config/remote-harness-mcp/agent.env).  This keeps the token
                      out of launchers such as the Windows Startup script.  Any
                      other MESH_* key in that file is exported as well.
    MESH_SHELL        Windows command shell: "cmd" or "git-bash" (default:
                      PowerShell, falling back to cmd.exe when PowerShell is absent)
    MESH_WORKSPACE    base directory for relative paths (default: cwd)
    MESH_READ_ONLY    "1"/"true" -> mutating tools refuse to run
    MESH_WRITE_ROOTS  pathsep-separated list of roots allowed for writes

    MESH_UPDATE_*      self-update: see core/updater.py. A running node checks for a
                       newer GitHub release in the background and installs it
 
Resilience contract:

* every link failure (DNS, TLS, reset, half-open socket after sleep) is retried
  forever with capped exponential backoff plus jitter;
* tool calls run concurrently, so a slow command never delays another call or
  the keepalive pings;
* only one agent per node name runs on a machine.  The gateway keeps exactly one
  tunnel per node and a new tunnel supersedes the old one, so two agents would
  knock each other off in an endless loop and fail every in-flight call.  A
  second instance therefore waits for the lock instead of connecting.
"""

import asyncio
import atexit
import faulthandler
import json
import logging
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Optional

try:
    import websockets
except ImportError as _websockets_error:      # a launcher pinned a Python without the dep
    websockets = None                         # type: ignore[assignment]
    _WEBSOCKETS_IMPORT_ERROR = _websockets_error
else:
    _WEBSOCKETS_IMPORT_ERROR = None

from core import domain, mcp_tools, paths, updater
from core import version as version_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rhm-agent")

#: Resolved through :mod:`core.paths`: a fresh install uses the new name, an
#: upgraded node keeps the directory its token already lives in.
CONFIG_DIR = paths.config_dir()
DEFAULT_CONFIG_FILE = os.path.join(CONFIG_DIR, "agent.env")

#: Diagnostics that answer "why is this node offline?" without the tunnel. The
#: watchdog (ops/windows/agent-watchdog.ps1) and ops/doctor.ps1 read these files,
#: and the incident that motivated them - a launcher pinned to the Microsoft
#: Store Python alias that left a single torn line in agent.log and kept the node
#: offline for a day - is exactly what they prevent.
HEARTBEAT_FILE = os.path.join(CONFIG_DIR, "agent.heartbeat")
FAULT_FILE = os.path.join(CONFIG_DIR, "agent.fault.log")
STOP_MARKER_FILE = os.path.join(CONFIG_DIR, "agent.stopped")
HEARTBEAT_INTERVAL = 15.0

#: Backoff cap for ordinary link failures.
MAX_BACKOFF = 15.0
#: Wait after the gateway rejects the token: retrying fast cannot help, but the
#: node must recover by itself once it is re-registered.
UNAUTHORIZED_BACKOFF = 60.0


def read_env_file(path: str) -> dict:
    """Parse a KEY=VALUE file; tolerates a UTF-8 BOM (Windows PowerShell 5 writes one).

    The implementation lives in :mod:`core.domain`, which reads ``domain.env`` with
    the same rules; it is re-exported here so existing importers of
    ``core.agent.read_env_file`` keep working and the project keeps exactly one
    parser.
    """
    return domain.read_env_file(path)


#: Hostnames that identify no machine. Android answers "localhost" to
#: ``gethostname(2)`` for every device, and Termux has no hostname of its own, so
#: a node that trusted it would collide with every other phone: the gateway keeps
#: exactly one tunnel per name and the newer one evicts the older.
_USELESS_HOSTNAMES = frozenset({"", "localhost", "localhost.localdomain", "android"})


def android_device_name() -> str:
    """The phone's model as a node name (``Pixel 7 Pro`` -> ``pixel7pro``).

    ``getprop`` ships with Android itself, needs no root and is on PATH inside
    Termux. A device that does not answer yields "" and the caller falls back to
    the generic name - this helper never raises.
    """
    for prop in ("ro.product.model", "ro.product.device"):
        try:
            completed = subprocess.run(["getprop", prop], capture_output=True, timeout=5)
        except Exception:
            continue
        value = (completed.stdout or b"").decode("utf-8", "replace")
        safe = "".join(c for c in value.strip().lower() if c.isalnum() or c in "-_")
        if safe:
            return safe
    return ""


def default_node_name() -> str:
    """Node name used when ``MESH_USER`` is not configured: the machine's name.

    This used to be a personal nickname, so every node that did not set
    ``MESH_USER`` claimed that *same* name on the shared gateway - and the gateway
    keeps exactly one tunnel per name, evicting the previous one. Two unrelated
    users would therefore knock each other offline in an endless loop, which is
    precisely the flapping the instance lock exists to prevent (and that lock is
    per machine, so it cannot help across machines). The installers already
    register the sanitised hostname, so the agent now agrees with them.

    On a phone the hostname is worse than useless - it is the same "localhost" for
    every device - so Termux uses the device model instead, exactly as the
    installer registers it.
    """
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    safe = "".join(c for c in hostname.strip().lower() if c.isalnum() or c in "-_")
    if safe in _USELESS_HOSTNAMES and domain.is_termux():
        safe = android_device_name()
    return safe or "node"


def configuration_error() -> Optional[str]:
    """Human-readable reason this node cannot connect, or None when it is usable.

    An empty token is not a transient link failure: the gateway rejects the node
    on every attempt, so retrying forever only fills agent.log and leaves a
    broken autostart behind. The caller reports this and stops.
    """
    if not TOKEN:
        return ("MESH_TOKEN is empty (environment and %s). The gateway rejects a node "
                "without a token, so the tunnel cannot work - re-run the installer."
                % (os.environ.get("MESH_CONFIG_FILE") or DEFAULT_CONFIG_FILE))
    return None


def _load_settings() -> dict:
    file_values = read_env_file(os.environ.get("MESH_CONFIG_FILE") or DEFAULT_CONFIG_FILE)

    def pick(key: str, default: str) -> str:
        return os.environ.get(key) or file_values.get(key) or default

    # Every other MESH_* key the operator put in the same file is exported too, so
    # one agent.env (or one Windows Startup launcher) configures the whole node:
    # MESH_SHELL for the Windows command shell, MESH_WORKSPACE, MESH_READ_ONLY,
    # MESH_WRITE_ROOTS, MESH_JOBS_DIR, MESH_MAX_OUTPUT_CHARS. The real environment
    # always wins over the file.
    for key, value in file_values.items():
        if key.startswith("MESH_") and not os.environ.get(key):
            os.environ[key] = value

    return {
        # The host the node dials. core.domain owns the order: the configured
        # public domain (MESH_PUBLIC_URL, or domain.env) wins, and the legacy
        # MESH_GATEWAY - which the loop above has just exported from agent.env - is
        # only consulted while nothing else names a domain.
        "gateway": domain.gateway_host(),
        "user": pick("MESH_USER", default_node_name()),
        "token": pick("MESH_TOKEN", ""),
    }


_SETTINGS = _load_settings()
GATEWAY_HOST = _SETTINGS["gateway"]
USER = _SETTINGS["user"]
TOKEN = _SETTINGS["token"]


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def configure_from_env() -> None:
    """Apply MESH_* environment configuration to the shared tool module."""
    write_roots = os.environ.get("MESH_WRITE_ROOTS") or None
    jobs_dir = os.environ.get("MESH_JOBS_DIR") or None
    max_chars = os.environ.get("MESH_MAX_OUTPUT_CHARS")
    mcp_tools.configure(
        workspace=os.environ.get("MESH_WORKSPACE") or os.getcwd(),
        read_only=_env_flag("MESH_READ_ONLY"),
        write_roots=write_roots,
        jobs_dir=jobs_dir,
        max_output_chars=max_chars,
        # Public shares: the node name in a share link must be the name this
        # tunnel registers with the gateway (the gateway routes
        # /<node>/<slug>/... back to that tunnel), so it is passed explicitly
        # rather than left to web_share's MESH_USER fallback - which would say
        # "anonymous" whenever the operator relied on the hostname default.
        # ``public_url`` is not passed here: core/domain.py owns the domain.
        web_dir=os.environ.get("MESH_WEB_DIR") or None,
        mesh_user=USER,
        max_share_bytes=os.environ.get("MESH_WEB_MAX_BYTES"),
        max_shares=os.environ.get("MESH_WEB_MAX_SHARES"),
        web_listing=os.environ.get("MESH_WEB_LISTING"),
        # Device branch (core/device.py, core/termux.py). "auto" is the default and
        # keeps the tools advertised on every platform - the phone-only ones then
        # answer with a reason instead of vanishing, because the gateway's static
        # tool surface must match this node's exactly.
        device=os.environ.get("MESH_DEVICE") or "auto",
        device_actions=os.environ.get("MESH_DEVICE_ACTIONS") or None,
        capture_dir=os.environ.get("MESH_DEVICE_CAPTURE_DIR") or None,
        device_pim=_env_flag("MESH_DEVICE_PIM"),
        device_quick=_env_flag("MESH_DEVICE_QUICK"),
    )


# Configure as soon as the node module is imported so external callers (tests,
# harnesses) observe the environment-driven settings.
configure_from_env()


# ---------------------------------------------------------------------------
# Runtime diagnostics: banner, fault log, heartbeat, clean-stop marker
#
# A node that never connects leaves the client with an opaque frontend error, so
# every start records what it is: which interpreter, which websockets, from which
# directory. The heartbeat makes "the agent runs but is not connected" visible to
# the watchdog, and the absence of a clean-stop marker tells it that the process
# was killed instead of shutting down.
# ---------------------------------------------------------------------------

_RUNTIME_FACTS = None
_FAULT_HANDLE = None

#: Live state reported through the heartbeat. Written by the event loop and read
#: by the heartbeat thread; plain dict operations are atomic enough under the GIL
#: and a lost update only costs one cycle of freshness.
_HEARTBEAT_STATE = {
    "connected": False,
    "connects": 0,
    "last_tool": "",
    "last_tool_at": 0.0,
    "last_error": "",
    "started_at": 0.0,
}


def runtime_facts() -> dict:
    """Interpreter, dependency and location facts of this agent process."""
    global _RUNTIME_FACTS
    if _RUNTIME_FACTS is None:
        version = getattr(websockets, "__version__", "MISSING") if websockets else "MISSING"
        _RUNTIME_FACTS = {
            "python": "%d.%d.%d" % sys.version_info[:3],
            "interpreter": sys.executable or "",
            "websockets": version,
            "node_version": version_module.__version__,
        }
    facts = dict(_RUNTIME_FACTS)
    # Read fresh: tests (and a re-configured process) may change these.
    facts["gateway"] = GATEWAY_HOST
    facts["user"] = USER
    return facts


def log_runtime_banner() -> dict:
    """Log one line naming the interpreter a launcher actually started.

    The Windows incident produced no such line: the launcher ran the Microsoft
    Store alias, which printed ``Python `` and exited, so nothing in the log said
    which interpreter was expected.
    """
    facts = runtime_facts()
    logger.info("Runtime: node=%s interpreter=%s python=%s websockets=%s cwd=%s pid=%s",
                facts.get("node_version") or version_module.__version__,
                facts["interpreter"] or "<unknown>", facts["python"],
                facts["websockets"], os.getcwd(), os.getpid())
    if _WEBSOCKETS_IMPORT_ERROR is not None:
        logger.error("The 'websockets' package is not importable by this interpreter (%s): %s. "
                     "The tunnel cannot start; run: %s -m pip install websockets",
                     facts["interpreter"] or "<unknown>", _WEBSOCKETS_IMPORT_ERROR,
                     facts["interpreter"] or "python")
    if "\\WindowsApps\\" in facts["interpreter"] or "/WindowsApps/" in facts["interpreter"]:
        logger.error("This agent runs through the Microsoft Store 'python.exe' alias (%s). That alias "
                     "stops working after a Store repair or an update; the installer must pin a real "
                     "interpreter instead.", facts["interpreter"])
    return facts


def write_heartbeat(path: Optional[str] = None, **extra) -> dict:
    """Atomically refresh the heartbeat file read by the watchdog and the doctor."""
    target = path or HEARTBEAT_FILE
    payload = {"ts": time.time(), "pid": os.getpid()}
    payload.update(runtime_facts())
    payload.update(_HEARTBEAT_STATE)
    payload.update(extra)
    try:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = target + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, target)
    except Exception as exc:                   # diagnostics never break the tunnel
        logger.debug("Could not write the heartbeat %s: %s", target, exc)
    return payload


def read_heartbeat(path: Optional[str] = None) -> dict:
    """The last heartbeat, or an empty dict when there is none or it is unreadable."""
    target = path or HEARTBEAT_FILE
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def heartbeat_age(path: Optional[str] = None) -> float:
    """Seconds since the last heartbeat; ``inf`` when there is none."""
    try:
        return max(0.0, time.time() - float(read_heartbeat(path).get("ts")))
    except Exception:
        return float("inf")


def mark_connected(connected: bool, reason: str = "") -> None:
    """Record the tunnel state for the heartbeat."""
    _HEARTBEAT_STATE["connected"] = bool(connected)
    if connected:
        _HEARTBEAT_STATE["connects"] = int(_HEARTBEAT_STATE.get("connects", 0)) + 1
        _HEARTBEAT_STATE["last_error"] = ""
    elif reason:
        _HEARTBEAT_STATE["last_error"] = str(reason)[:300]


def start_heartbeat(path: Optional[str] = None, interval: float = HEARTBEAT_INTERVAL):
    """Start the daemon heartbeat thread (it must never keep the process alive)."""
    _HEARTBEAT_STATE["started_at"] = time.time()
    write_heartbeat(path)

    def _beat():
        while True:
            time.sleep(interval)
            write_heartbeat(path)

    thread = threading.Thread(target=_beat, name="mesh-heartbeat", daemon=True)
    thread.start()
    return thread


def enable_fault_log(path: Optional[str] = None) -> Optional[str]:
    """Send fatal-error tracebacks (native crash, deadlock) to their own file.

    Without this the only trace of an abnormal end is a torn line in agent.log.
    """
    global _FAULT_HANDLE
    target = path or FAULT_FILE
    try:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _FAULT_HANDLE = open(target, "a", encoding="utf-8")
        faulthandler.enable(file=_FAULT_HANDLE, all_threads=True)
    except Exception as exc:
        logger.debug("Could not enable the fault log %s: %s", target, exc)
        return None
    return target


def write_stop_marker(reason: str = "stop") -> None:
    """Record a clean stop: a missing marker means the process was killed."""
    try:
        directory = os.path.dirname(STOP_MARKER_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(STOP_MARKER_FILE, "w", encoding="utf-8") as handle:
            handle.write("%s pid=%s reason=%s\n"
                         % (time.strftime("%Y-%m-%dT%H:%M:%S"), os.getpid(), reason))
    except Exception:
        pass


def install_exit_markers() -> None:
    """Write the clean-stop marker on a normal exit or on a received signal."""
    atexit.register(write_stop_marker, "atexit")

    def _on_signal(signum, _frame):
        write_stop_marker("signal=%s" % signum)
        raise SystemExit(0)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, _on_signal)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Self-update
#
# The updater itself lives in core/updater.py and is deliberately unaware of this
# module: it can check, download, verify and swap a payload with no agent around
# (ops/update.ps1 and the CLI drive it that way). What only the agent can do is
# stop cleanly so the swapped code is what runs next - that is the hook below.
# ---------------------------------------------------------------------------

#: The updater's last answer, carried in every heartbeat so ops/doctor.ps1 can
#: show "a newer release exists" (or "the last check failed") without a network.
_UPDATE_STATE: dict = {}
_RESTART_EVENT = threading.Event()


def note_update_state(result: dict) -> None:
    """Record one updater answer in the heartbeat."""
    if not isinstance(result, dict):
        return
    check = result.get("check") if isinstance(result.get("check"), dict) else result
    payload = {
        "current": check.get("current") or version_module.__version__,
        "latest": check.get("latest") or "",
        "available": bool(check.get("update_available")),
        "checked_at": check.get("checked_at") or "",
        "reason": str(check.get("reason") or "")[:200],
    }
    applied = result.get("apply")
    if isinstance(applied, dict) and applied.get("applied"):
        payload["applied"] = applied.get("to") or ""
    _UPDATE_STATE.clear()
    _UPDATE_STATE.update(payload)
    _HEARTBEAT_STATE["update"] = dict(_UPDATE_STATE)
    write_heartbeat()


def request_restart(delay: float = 5.0) -> bool:
    """Stop this agent (once) so the restart helper can start the updated copy.

    The delay is what lets an in-flight tool result reach the caller: a
    ``mesh_update`` call answers "applied and restarting" and only then does the
    process leave, which is also what releases the tunnel and the instance lock
    the new agent needs.
    """
    if _RESTART_EVENT.is_set():
        return False
    _RESTART_EVENT.set()
    name = "SIGBREAK" if os.name == "nt" else "SIGTERM"
    number = getattr(signal, name, None)
    if number is None:
        number = getattr(signal, "SIGTERM", None)

    def _fire():
        time.sleep(max(0.0, delay))
        logger.warning("Stopping this agent so the updated version can start (signal %s).", name)
        try:
            signal.raise_signal(number)
        except Exception:
            # A platform without raise_signal still has to end up updated; the
            # restart helper is already waiting for this pid either way.
            os._exit(0)

    threading.Thread(target=_fire, name="mesh-restart", daemon=True).start()
    return True


def start_update_thread():
    """Check for a newer release in the background and install it.

    The first check is delayed (``MESH_UPDATE_INITIAL_DELAY``, 90 s by default)
    so a node that was just started - including one that was just updated - gets
    its tunnel up and serves a few calls before anything restarts.
    """
    config = updater.config_from_env()
    if not config["enabled"]:
        logger.info("Self-update is disabled (MESH_UPDATE_CHECK=0).")
        return None
    # Registered even when automatic updating is off: an operator (or the model
    # through mesh_update) can still ask for an update, and the node then has to
    # stop itself for the new version to run.
    updater.set_restart_hook(request_restart)
    try:
        initial = float(os.environ.get("MESH_UPDATE_INITIAL_DELAY", "") or 90.0)
    except ValueError:
        initial = 90.0

    def _loop():
        time.sleep(max(0.0, initial))
        while True:
            try:
                result = updater.background_tick(config=config)
                note_update_state(result)
                check = result.get("check") or {}
                applied = result.get("apply") or {}
                if applied.get("applied"):
                    logger.warning("Self-update: %s -> %s applied from %s; restarting.",
                                   applied.get("from"), applied.get("to"), applied.get("asset"))
                elif check.get("update_available"):
                    logger.info("Self-update: release %s is available (%s).",
                                check.get("latest"), check.get("reason"))
                elif check.get("ok"):
                    logger.debug("Self-update: up to date (%s).", check.get("current"))
                else:
                    logger.warning("Self-update check failed: %s", check.get("reason"))
            except Exception as exc:      # the updater must never take the node down
                logger.warning("Self-update iteration failed: %s", exc)
            time.sleep(max(60, int(config["interval"])))

    thread = threading.Thread(target=_loop, name="mesh-updater", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Single instance per node name
# ---------------------------------------------------------------------------

class InstanceLock:
    """Non-blocking, OS-released file lock (fcntl on POSIX, msvcrt on Windows).

    The OS drops the lock when the process dies, so a crash never leaves a stale
    lock behind.
    """

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def try_acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True


def wait_for_instance_lock(user: str) -> InstanceLock:
    """Block until this process is the only agent for ``user`` on the machine."""
    safe = "".join(c for c in user if c.isalnum() or c in "-_") or "node"
    lock = InstanceLock(os.path.join(CONFIG_DIR, "agent-%s.lock" % safe))
    warned = False
    while not lock.try_acquire():
        if not warned:
            logger.warning("Another agent for node '%s' is already running on this machine; "
                           "waiting for it to exit instead of fighting over the tunnel.", user)
            warned = True
        time.sleep(10)
    return lock


# ---------------------------------------------------------------------------
# Tunnel
# ---------------------------------------------------------------------------

async def handle_tool_call(name: str, args: dict) -> dict:
    """Forward one tool call to the shared implementation in a worker thread."""
    logger.info("Executing %s", name)
    _HEARTBEAT_STATE["last_tool"] = name or ""
    _HEARTBEAT_STATE["last_tool_at"] = time.time()
    try:
        return await asyncio.to_thread(mcp_tools.call_tool, name, args or {})
    finally:
        # A heartbeat right after every call makes "the agent is alive and served
        # this tool" visible to the watchdog without reading the tunnel state.
        write_heartbeat()


async def _serve_call(ws, req_id, params: dict) -> None:
    tool_name = params.get("name")
    tool_args = params.get("arguments", {})
    try:
        res = await handle_tool_call(tool_name, tool_args)
    except Exception as tool_exc:
        # A tool must never take the tunnel down with it.
        logger.warning(f"Tool {tool_name} failed: {tool_exc}")
        _HEARTBEAT_STATE["last_error"] = "%s: %s" % (type(tool_exc).__name__, tool_exc)
        res = {"error": f"{type(tool_exc).__name__}: {tool_exc}"}
    try:
        await ws.send(json.dumps({"id": req_id, "result": res}, ensure_ascii=False))
    except Exception as send_exc:
        # The link dropped while the tool ran; the gateway already failed the
        # call, and the reconnect loop restores the tunnel.
        logger.warning(f"Could not deliver result of {tool_name}: {send_exc}")
        _HEARTBEAT_STATE["last_error"] = "send failed: %s" % send_exc
        write_heartbeat()


def _is_unauthorized(exc: BaseException) -> bool:
    """True when the gateway rejected the node's credentials.

    The gateway closes with 4001 before accepting the websocket; Starlette turns
    that into an HTTP 403 handshake response, so depending on timing the client
    sees either the close code or the HTTP status.  Handles websockets' legacy
    and new exception shapes.
    """
    for attr in ("rcvd", "received"):
        frame = getattr(exc, attr, None)
        if frame is not None and getattr(frame, "code", None) == 4001:
            return True
    if getattr(exc, "code", None) == 4001:
        return True
    status = getattr(exc, "status_code", None)                       # legacy InvalidStatusCode
    response = getattr(exc, "response", None)                        # websockets >= 14 InvalidStatus
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    return status in (401, 403)


def _looks_like_dns_failure(exc: BaseException) -> bool:
    """True when the connection failed because the gateway name did not resolve.

    A wrong or dead gateway domain produced an endless stream of generic
    "Connection lost" lines, which reads like a network outage. Naming the cause
    once is the difference between a five-minute fix and a long hunt.
    """
    if isinstance(exc, socket.gaierror):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in (
        "name or service not known",       # Linux getaddrinfo
        "nodename nor servname",           # BSD / macOS
        "getaddrinfo failed",              # Windows
        "temporary failure in name resolution",
        "no address associated with hostname",
    ))


def tunnel_uri() -> str:
    """Canonical tunnel URL: one shared domain, node selected by ?user=."""
    base = GATEWAY_HOST.rstrip("/")
    if not base.startswith(("ws://", "wss://")):
        base = "wss://" + base
    return f"{base}/ws/tunnel?user={USER}"


async def run_agent():
    """Stay connected to the gateway, healing every kind of link failure.

    A dropped websocket is retried with exponential backoff plus jitter: a short
    first retry restores a flapping link quickly, while the growing cap stops a
    hammering loop when the network is genuinely down. The backoff resets after a
    healthy connection, so a long-lived session is not penalised.
    """
    if not TOKEN:
        logger.error("MESH_TOKEN is empty (env and %s): the gateway will reject this node. "
                     "Re-run the installer.", os.environ.get("MESH_CONFIG_FILE") or DEFAULT_CONFIG_FILE)
    uri = tunnel_uri()
    logger.info(f"Connecting to Gateway {uri.split('&token=')[0]} ...")
    backoff = 1.0
    in_flight = set()
    while True:
        try:
            # websockets >=14 renamed extra_headers to additional_headers.
            import inspect
            header_arg = ("additional_headers" if "additional_headers" in
                          inspect.signature(websockets.connect).parameters else "extra_headers")
            async with websockets.connect(uri, **{header_arg: {"Authorization": f"Bearer {TOKEN}"}},
                                            ping_interval=10, ping_timeout=10,
                                            close_timeout=5, open_timeout=15,
                                            max_size=None) as ws:
                logger.info(f"Connected to Mesh Gateway as '{USER}'!")
                backoff = 1.0                      # healthy again
                mark_connected(True)
                write_heartbeat()
                async for raw_msg in ws:
                    try:
                        data = json.loads(raw_msg)
                    except Exception:
                        continue
                    if data.get("method") == "tools/call":
                        # Each call runs on its own: Gemini issues parallel calls,
                        # and serialising them pushed later ones past the
                        # gateway's 28 s limit.
                        task = asyncio.create_task(_serve_call(ws, data.get("id"), data.get("params", {})))
                        in_flight.add(task)
                        task.add_done_callback(in_flight.discard)
            # A clean close (e.g. gateway restart) still needs a reconnect.
            raise ConnectionError("tunnel closed by gateway")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # The heartbeat is how a watchdog - and an operator reading the files
            # later - can tell "running but disconnected" from "not running".
            mark_connected(False, "%s: %s" % (type(e).__name__, e))
            write_heartbeat()
            if _is_unauthorized(e):
                delay = UNAUTHORIZED_BACKOFF
                logger.error("Gateway rejected node '%s' (4001 Unauthorized): the token is wrong or the "
                             "node was removed. Re-run the installer. Retrying in %.0fs.", USER, delay)
            elif _looks_like_dns_failure(e):
                delay = min(backoff, MAX_BACKOFF) + random.uniform(0, 0.5)
                logger.error("Gateway '%s' does not resolve (%s). Check the gateway name (MESH_GATEWAY) "
                             "and this machine's DNS. Retrying in %.0fs.", GATEWAY_HOST, e, delay)
                backoff = min(backoff * 2, MAX_BACKOFF)
            else:
                delay = min(backoff, MAX_BACKOFF) + random.uniform(0, 0.5)
                logger.warning(f"Connection lost: {e}. Reconnecting in {delay:.1f}s...")
                backoff = min(backoff * 2, MAX_BACKOFF)
            await asyncio.sleep(delay)


def main() -> None:
    # Fail fast on a configuration that can never work, instead of retrying the
    # gateway every 60 seconds forever from a Windows Startup launcher.
    problem = configuration_error()
    if problem:
        logger.error("%s Refusing to start.", problem)
        sys.exit(2)
    install_exit_markers()
    enable_fault_log()
    log_runtime_banner()
    # An update that was interrupted mid-swap (a kill, a power cut) is settled
    # here, before the tunnel starts: either finished or rolled back.
    recovery = updater.recover_pending()
    if recovery:
        logger.warning("Self-update recovery: %s", recovery)
    start_heartbeat()
    if _WEBSOCKETS_IMPORT_ERROR is not None:
        # The banner above already named the interpreter and the missing module.
        logger.error("Refusing to start: this interpreter cannot import 'websockets'.")
        sys.exit(3)
    _lock = wait_for_instance_lock(USER)  # held for the life of the process
    write_heartbeat()
    logger.info("Instance lock acquired: this is the only agent for node '%s'.", USER)
    # Only the agent that owns the node updates it: a second instance waiting for
    # the lock must not swap files under the process that is actually running.
    start_update_thread()
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        write_heartbeat(stopping=True)


if __name__ == "__main__":
    main()
