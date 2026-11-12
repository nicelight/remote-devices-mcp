#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/domain.py - the ONE place that knows the project's public domain.

Every component that has to name the public host asks this module instead of
building its own chain of fallbacks:

* :mod:`gateway` (URLs and texts handed to clients),
* :mod:`core.agent` (the host a node dials for its tunnel),
* :mod:`core.web_share` (links published for shared files),
* :mod:`core.mcp_tools` (the ``public_url`` it passes on),
* :mod:`rhm_sync`, :mod:`rhm_watcher` (links printed into generated facts/logs).

Resolution order, highest priority first:

1. ``configure(public_base_url=...)`` - explicit injection, for tests;
2. ``MESH_PUBLIC_URL`` in the environment;
3. ``AGY_PUBLIC_BASE_URL`` in the environment (legacy alias, same meaning);
4. ``MESH_PUBLIC_URL`` (or ``AGY_PUBLIC_BASE_URL``) in the *domain file* - the one
   host-level place to configure this:

   ==========  ==========================================================
   Linux       ``/etc/remote-harness-mcp/domain.env``
   Windows     ``%USERPROFILE%\\.config\\remote-harness-mcp\\domain.env``
   Termux      ``~/.config/remote-harness-mcp/domain.env``
   ==========  ==========================================================

   ``domain.env`` is plain ``KEY=VALUE`` and may carry a UTF-8 BOM, because
   Windows PowerShell writes one. Point ``MESH_DOMAIN_FILE`` at another path to
   override the location (used by the tests);
5. ``MESH_GATEWAY`` in the environment - the legacy node-side setting. It is
   honoured for the *tunnel host* only, so a node installed before ``domain.env``
   existed keeps working until its operator moves the value into the domain file;
6. :data:`DEFAULT_PUBLIC_BASE_URL` - the single default, declared once, here.

The only project module imported here is :mod:`core.paths` (itself standard
library only), so any component may import this module - including
``gateway.py`` when it is deployed on its own.
"""

import os
import threading
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from core import paths

#: The project default. Declared exactly once; every other module asks for it.
DEFAULT_PUBLIC_BASE_URL = "https://mcp.natureonzoom.win"

#: Neutral placeholder for *display* in an unconfigured deployment. It is not a
#: usable address and is never used to build a URL that a caller is expected to
#: reach - only tools that generate human-readable facts fall back to it, exactly
#: as they did before the domain moved here.
PLACEHOLDER_PUBLIC_BASE_URL = "https://<your-domain>"

#: ``KEY=VALUE`` keys understood in the environment and in the domain file.
_ENV_KEYS = ("MESH_PUBLIC_URL", "AGY_PUBLIC_BASE_URL")

#: Environment variables: the domain file location, and the legacy node host.
_DOMAIN_FILE_ENV = "MESH_DOMAIN_FILE"
_LEGACY_HOST_ENV = "MESH_GATEWAY"

#: Directory names live in :mod:`core.paths`, which also keeps the pre-rename
#: directory readable so an upgraded node keeps its ``domain.env``.
_CONFIG_DIR_NAME = paths.APP_NAME

_LOCK = threading.RLock()
_INJECTED: Optional[str] = None
_FILE_OVERRIDE: Optional[str] = None
#: (path, mtime) -> parsed values, so a per-request caller does not re-read the
#: file while an edit is still picked up (the mtime changes).
_FILE_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "values": {}}


# ---------------------------------------------------------------------------
# The KEY=VALUE parser
# ---------------------------------------------------------------------------

def read_env_file(path: str) -> Dict[str, str]:
    """Parse a KEY=VALUE file; tolerates a UTF-8 BOM (Windows PowerShell 5 writes one).

    This is the only parser in the project: :mod:`core.agent` imports it from here
    (and re-exports it, so callers of ``core.agent.read_env_file`` keep working).
    """
    values: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


# ---------------------------------------------------------------------------
# The domain file
# ---------------------------------------------------------------------------

def is_termux() -> bool:
    """True when this process runs inside Termux on Android.

    Termux is not FHS-compliant: a phone app is never root, there is no ``/etc``
    at the usual place and no host-level configuration directory to put a domain
    file in. The question is answered *here* rather than in a new module because
    this one must stay importable on its own - ``gateway.py`` is deployed without
    the rest of the package - and :mod:`core.agent` asks it too instead of
    repeating the check.

    Neither signal is trusted alone: Termux exports ``TERMUX_VERSION``, its
    ``$PREFIX`` points inside ``com.termux``, and its application data directory
    has a fixed path. A Linux host with an unrelated ``$PREFIX`` is not a phone.
    """
    if os.environ.get("TERMUX_VERSION"):
        return True
    if "com.termux" in os.environ.get("PREFIX", ""):
        return True
    return os.path.isdir("/data/data/com.termux/files/usr/bin")


def default_domain_file() -> str:
    """Where ``domain.env`` lives when the operator did not override the path.

    Linux keeps host-level configuration in ``/etc``. Windows and Termux have no
    such place - Windows because the concept does not exist there, Termux because
    a phone has no root and no ``/etc`` - so both use the per-user directory that
    also holds ``agent.env``. ``install.sh`` resolves the same path for a Termux
    install, so the installer and the node never disagree about where the domain
    lives.
    """
    if os.name == "nt" or is_termux():
        return os.path.join(paths.config_dir(), "domain.env")
    return os.path.join(paths.etc_dir(), "domain.env")


def domain_file() -> str:
    """The domain file in effect: injected path, then ``MESH_DOMAIN_FILE``, then default."""
    with _LOCK:
        override = _FILE_OVERRIDE
    if override:
        return override
    return os.environ.get(_DOMAIN_FILE_ENV, "").strip() or default_domain_file()


def _domain_file_values() -> Dict[str, str]:
    """Parsed domain file, cached by modification time."""
    path = domain_file()
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        with _LOCK:
            _FILE_CACHE.update({"path": path, "mtime": None, "values": {}})
        return {}
    with _LOCK:
        if _FILE_CACHE["path"] == path and _FILE_CACHE["mtime"] == mtime:
            return dict(_FILE_CACHE["values"])
    values = read_env_file(path)
    with _LOCK:
        _FILE_CACHE.update({"path": path, "mtime": mtime, "values": values})
    return dict(values)


def _invalidate_file_cache() -> None:
    with _LOCK:
        _FILE_CACHE.update({"path": None, "mtime": None, "values": {}})


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def normalise_public_base_url(value: Any) -> str:
    """``mesh.example.com/`` and ``https://mesh.example.com/`` -> ``https://mesh.example.com``.

    A bare host gains the ``https://`` scheme; a trailing slash is dropped so that
    joining a path never produces a double slash. An explicit ``http://`` is left
    alone - the operator may have a good reason. Public because callers that accept
    a domain of their own (``core.mcp_tools.configure(public_url=...)``) normalise it
    through the same rules instead of inventing their own.
    """
    text = str(value or "").strip().strip('"').strip("'").rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    return text.rstrip("/")


def _resolve() -> Dict[str, str]:
    """``{"base": ..., "source": "injected|env|file|default"}`` for the current state."""
    with _LOCK:
        injected = _INJECTED
    if injected:
        return {"base": injected, "source": "injected"}

    for key in _ENV_KEYS:
        value = normalise_public_base_url(os.environ.get(key))
        if value:
            return {"base": value, "source": "env"}

    file_values = _domain_file_values()
    for key in _ENV_KEYS:
        value = normalise_public_base_url(file_values.get(key))
        if value:
            return {"base": value, "source": "file"}

    return {"base": DEFAULT_PUBLIC_BASE_URL, "source": "default"}


def public_base_url() -> str:
    """Public base URL, e.g. ``https://mesh.example.com`` - never with a trailing slash."""
    return _resolve()["base"]


def public_host() -> str:
    """Host part of :func:`public_base_url`, e.g. ``mesh.example.com``."""
    base = public_base_url()
    return urlparse(base).netloc or base


def public_url(path: str = "") -> str:
    """``public_base_url()`` joined with *path* (this is not gateway.public_url()).

    ``gateway.public_url(path, user, token)`` additionally appends the node
    credentials; this one is the plain join that share links and log links need.
    """
    base = public_base_url()
    if not path:
        return base
    return base + (path if path.startswith("/") else "/" + path)


def gateway_host() -> str:
    """Authority a node dials for its tunnel (no scheme, unless legacy carries one).

    The configured public base wins, so moving the domain into ``domain.env`` moves
    the tunnel with it. Only when nothing is configured does the legacy
    ``MESH_GATEWAY`` decide, and a legacy value that already carries a scheme (for
    example ``ws://10.0.0.1``) is passed through untouched, because the node's
    tunnel builder honours such a scheme.
    """
    resolved = _resolve()
    if resolved["source"] != "default":
        parsed = urlparse(resolved["base"])
        authority = parsed.netloc or parsed.path
        if parsed.netloc and parsed.path:
            authority = parsed.netloc + parsed.path
        return authority

    legacy = str(os.environ.get(_LEGACY_HOST_ENV, "") or "").strip().rstrip("/")
    if legacy:
        return legacy
    return urlparse(DEFAULT_PUBLIC_BASE_URL).netloc or DEFAULT_PUBLIC_BASE_URL


def source() -> str:
    """Where the current value comes from: ``injected``, ``env``, ``file`` or ``default``."""
    return _resolve()["source"]


def is_explicit() -> bool:
    """True when the domain was configured (by injection, environment or file).

    Tools that must not assume a deployment - the ones that print neutral facts -
    use this to decide between the configured domain and
    :data:`PLACEHOLDER_PUBLIC_BASE_URL`.
    """
    return source() != "default"


# ---------------------------------------------------------------------------
# Injection (tests, and embedders that resolve the domain themselves)
# ---------------------------------------------------------------------------

def configure(**kwargs: Any) -> None:
    """Inject the domain instead of reading the environment and the file.

    Recognised keys:

    ``public_base_url``
        The value to use. ``None`` or ``""`` drops a previous injection, so the
        environment and the domain file decide again.
    ``domain_file``
        Alternative ``domain.env`` path. ``None`` or ``""`` restores the
        environment/default location.
    """
    global _INJECTED, _FILE_OVERRIDE
    with _LOCK:
        if "public_base_url" in kwargs:
            _INJECTED = normalise_public_base_url(kwargs.get("public_base_url")) or None
        if "domain_file" in kwargs:
            value = str(kwargs.get("domain_file") or "").strip()
            _FILE_OVERRIDE = value or None
        _invalidate_file_cache()


def reset() -> None:
    """Drop injection and cached file contents (call between tests)."""
    global _INJECTED, _FILE_OVERRIDE
    with _LOCK:
        _INJECTED = None
        _FILE_OVERRIDE = None
        _invalidate_file_cache()
