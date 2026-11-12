"""The public domain is configured in exactly one place: :mod:`core.domain`.

These tests pin the resolution order (injection, ``MESH_PUBLIC_URL``, the
``AGY_PUBLIC_BASE_URL`` alias, ``domain.env``, the legacy ``MESH_GATEWAY`` for the
tunnel host, and the single default), the URL forms that are accepted, and that the
gateway, the node and the tools all end up with the same host.
"""

import importlib
import os
from pathlib import Path

import pytest

from core import domain, paths

#: Variables the resolver reads, plus the ones :func:`core.agent._load_settings`
#: exports while it runs. Every test starts with all of them cleared.
_ISOLATED_KEYS = (
    "MESH_PUBLIC_URL",
    "AGY_PUBLIC_BASE_URL",
    "MESH_GATEWAY",
    "MESH_DOMAIN_FILE",
    "MESH_CONFIG_FILE",
    "MESH_USER",
    "MESH_TOKEN",
)


@pytest.fixture(autouse=True)
def isolated_domain(tmp_path, monkeypatch):
    """No environment and no configuration file: each test starts from zero.

    ``MESH_DOMAIN_FILE`` is pointed at a path that does not exist so a real
    ``/etc/remote-harness-mcp/domain.env`` on the host running the tests cannot change
    the outcome. The keys are restored by hand as well as by monkeypatch, because
    the code under test also *creates* environment entries (an absent key has no
    monkeypatch record to undo).
    """
    saved = {key: os.environ.get(key) for key in _ISOLATED_KEYS}
    for key in _ISOLATED_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MESH_DOMAIN_FILE", str(tmp_path / "absent-domain.env"))
    domain.reset()
    yield
    domain.reset()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _write_domain_file(path: Path, text: str, bom: bool = False) -> str:
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return str(path)


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------

def test_default_when_nothing_is_configured():
    assert domain.public_base_url() == domain.DEFAULT_PUBLIC_BASE_URL
    assert domain.public_base_url() == "https://mcp.natureonzoom.win"
    assert domain.public_host() == "mcp.natureonzoom.win"
    assert domain.gateway_host() == "mcp.natureonzoom.win"
    assert domain.source() == "default"
    assert domain.is_explicit() is False


def test_environment_wins_and_is_normalised(monkeypatch):
    monkeypatch.setenv("MESH_PUBLIC_URL", "https://mesh.example.com/")
    assert domain.public_base_url() == "https://mesh.example.com"
    assert domain.public_host() == "mesh.example.com"
    assert domain.gateway_host() == "mesh.example.com"
    assert domain.source() == "env"
    assert domain.is_explicit() is True


def test_bare_host_gains_the_scheme(monkeypatch):
    monkeypatch.setenv("MESH_PUBLIC_URL", "mesh.example.com")
    assert domain.public_base_url() == "https://mesh.example.com"


def test_explicit_http_is_not_upgraded(monkeypatch):
    monkeypatch.setenv("MESH_PUBLIC_URL", "http://mesh.example.com/")
    assert domain.public_base_url() == "http://mesh.example.com"


def test_rhm_public_base_url_is_an_alias(monkeypatch):
    monkeypatch.setenv("AGY_PUBLIC_BASE_URL", "https://alias.example.com")
    assert domain.public_base_url() == "https://alias.example.com"

    # MESH_PUBLIC_URL is the canonical name and outranks the alias.
    monkeypatch.setenv("MESH_PUBLIC_URL", "https://canonical.example.com")
    assert domain.public_base_url() == "https://canonical.example.com"


def test_domain_file_is_used_and_environment_beats_it(tmp_path, monkeypatch):
    path = _write_domain_file(tmp_path / "domain.env", "MESH_PUBLIC_URL=https://file.example.com\n")
    monkeypatch.setenv("MESH_DOMAIN_FILE", path)

    assert domain.public_base_url() == "https://file.example.com"
    assert domain.source() == "file"
    assert domain.is_explicit() is True

    monkeypatch.setenv("MESH_PUBLIC_URL", "https://env.example.com")
    assert domain.public_base_url() == "https://env.example.com"
    assert domain.source() == "env"


def test_domain_file_accepts_a_utf8_bom(tmp_path, monkeypatch):
    """Windows PowerShell writes a BOM; the reader must not choke on it."""
    path = _write_domain_file(
        tmp_path / "domain.env",
        "# comment\nMESH_PUBLIC_URL=https://bom.example.com\n\n",
        bom=True,
    )
    monkeypatch.setenv("MESH_DOMAIN_FILE", path)
    assert domain.public_base_url() == "https://bom.example.com"


def test_domain_file_accepts_the_alias_key(tmp_path, monkeypatch):
    path = _write_domain_file(tmp_path / "domain.env", "AGY_PUBLIC_BASE_URL=https://alias.example.com\n")
    monkeypatch.setenv("MESH_DOMAIN_FILE", path)
    assert domain.public_base_url() == "https://alias.example.com"


def test_domain_file_location_is_platform_specific_and_overridable(tmp_path, monkeypatch):
    """The default is the current directory name, or the pre-rename one.

    ``core.paths`` resolves the legacy name only when it is the one that exists on
    the host, so an upgraded node keeps reading the ``domain.env`` the previous
    version wrote. Either way the answer must be a real ``<dir>/domain.env``.
    """
    location = domain.default_domain_file()
    if os.name == "nt" or domain.is_termux():
        assert location == os.path.join(paths.config_dir(), "domain.env")
    else:
        assert location == os.path.join(paths.etc_dir(), "domain.env")
    assert location.endswith(os.path.join(paths.APP_NAME, "domain.env")) or \
        location.endswith(os.path.join(paths.LEGACY_APP_NAME, "domain.env"))

    override = str(tmp_path / "custom.env")
    monkeypatch.setenv("MESH_DOMAIN_FILE", override)
    assert domain.domain_file() == override


def test_injection_wins_and_reset_clears_it(monkeypatch):
    monkeypatch.setenv("MESH_PUBLIC_URL", "https://env.example.com")
    domain.configure(public_base_url="https://injected.example.com/")
    assert domain.public_base_url() == "https://injected.example.com"
    assert domain.source() == "injected"

    domain.configure(public_base_url=None)
    assert domain.public_base_url() == "https://env.example.com"

    domain.configure(domain_file="/nonexistent/domain.env")
    assert domain.domain_file() == "/nonexistent/domain.env"
    domain.reset()
    assert domain.source() == "env"


# ---------------------------------------------------------------------------
# Legacy MESH_GATEWAY: tunnel host only
# ---------------------------------------------------------------------------

def test_legacy_mesh_gateway_only_decides_the_tunnel_host(monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY", "legacy.example")
    # With nothing else configured it is the host a node dials...
    assert domain.gateway_host() == "legacy.example"
    # ...and it never becomes the public domain those URLs are built on.
    assert domain.public_base_url() == domain.DEFAULT_PUBLIC_BASE_URL
    assert domain.source() == "default"


def test_configured_domain_outranks_the_legacy_host(monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY", "legacy.example")
    monkeypatch.setenv("MESH_PUBLIC_URL", "https://mesh.example.com")
    assert domain.gateway_host() == "mesh.example.com"


def test_legacy_host_keeps_its_scheme(monkeypatch):
    """The node's tunnel builder honours an explicit ws:// target."""
    monkeypatch.setenv("MESH_GATEWAY", "ws://10.0.0.1:8096")
    assert domain.gateway_host() == "ws://10.0.0.1:8096"


# ---------------------------------------------------------------------------
# URL forms
# ---------------------------------------------------------------------------

def test_public_host_and_base_for_a_url_with_a_path(monkeypatch):
    monkeypatch.setenv("MESH_PUBLIC_URL", "https://mesh.example.com/gateway/")
    assert domain.public_base_url() == "https://mesh.example.com/gateway"
    assert domain.public_host() == "mesh.example.com"
    # The tunnel target keeps the path, so the node still reaches the right mount.
    assert domain.gateway_host() == "mesh.example.com/gateway"


def test_public_url_joins_without_double_slash(monkeypatch):
    monkeypatch.setenv("MESH_PUBLIC_URL", "https://mesh.example.com/")
    assert domain.public_url() == "https://mesh.example.com"
    assert domain.public_url("/sse") == "https://mesh.example.com/sse"
    assert domain.public_url("sse") == "https://mesh.example.com/sse"


# ---------------------------------------------------------------------------
# One domain, every component
# ---------------------------------------------------------------------------

def test_gateway_takes_the_domain_from_the_shared_module(monkeypatch):
    import gateway

    monkeypatch.setenv("MESH_PUBLIC_URL", "https://mesh.example.com/")
    try:
        reloaded = importlib.reload(gateway)
        assert reloaded.PUBLIC_BASE_URL == domain.public_base_url() == "https://mesh.example.com"
        assert reloaded.PUBLIC_HOST == domain.public_host() == "mesh.example.com"
        assert reloaded.public_url("/mcp", "node-one", "tok") == (
            "https://mesh.example.com/mcp?user=node-one&token=tok"
        )
    finally:
        monkeypatch.delenv("MESH_PUBLIC_URL", raising=False)
        importlib.reload(gateway)


def test_gateway_follows_the_domain_file_too(tmp_path, monkeypatch):
    import gateway

    path = _write_domain_file(tmp_path / "domain.env", "MESH_PUBLIC_URL=https://from-file.example.com\n")
    monkeypatch.setenv("MESH_DOMAIN_FILE", path)
    try:
        reloaded = importlib.reload(gateway)
        assert reloaded.PUBLIC_HOST == "from-file.example.com"
        assert reloaded.PUBLIC_HOST == domain.public_host()
    finally:
        monkeypatch.delenv("MESH_DOMAIN_FILE", raising=False)
        importlib.reload(gateway)


def test_node_tools_and_gateway_agree_on_the_host(tmp_path, monkeypatch):
    from core import agent, mcp_tools

    config = tmp_path / "agent.env"
    config.write_text("MESH_USER=node-one\nMESH_TOKEN=tok\n")
    monkeypatch.setenv("MESH_CONFIG_FILE", str(config))
    monkeypatch.setenv("MESH_PUBLIC_URL", "https://mesh.example.com")

    # The node dials the configured host...
    assert agent._load_settings()["gateway"] == "mesh.example.com"
    # ...the tools publish links on the same base (web_share reads this value)...
    assert mcp_tools.public_base_url() == domain.public_base_url() == "https://mesh.example.com"
    # ...and so does the gateway.
    import gateway

    try:
        reloaded = importlib.reload(gateway)
        assert reloaded.PUBLIC_HOST == "mesh.example.com"
    finally:
        monkeypatch.delenv("MESH_PUBLIC_URL", raising=False)
        importlib.reload(gateway)


def test_node_falls_back_to_the_legacy_host_from_agent_env(tmp_path, monkeypatch):
    """A node installed before domain.env existed keeps working unchanged."""
    from core import agent

    config = tmp_path / "agent.env"
    config.write_text("MESH_USER=node-one\nMESH_TOKEN=tok\nMESH_GATEWAY=legacy.example\n")
    monkeypatch.setenv("MESH_CONFIG_FILE", str(config))

    assert agent._load_settings()["gateway"] == "legacy.example"


def test_mcp_tools_public_url_can_be_overridden_and_released():
    from core import mcp_tools

    try:
        mcp_tools.configure(public_url="https://override.example/")
        assert mcp_tools.public_base_url() == "https://override.example"
    finally:
        mcp_tools.configure(public_url="")
    assert mcp_tools.public_base_url() == domain.public_base_url()


def test_agent_read_env_file_delegates_to_the_shared_parser(tmp_path):
    from core import agent

    path = tmp_path / "env"
    path.write_bytes(b"\xef\xbb\xbfMESH_PUBLIC_URL=https://bom.example.com\n# comment\nnot a pair\n")
    expected = {"MESH_PUBLIC_URL": "https://bom.example.com"}
    assert domain.read_env_file(str(path)) == expected
    assert agent.read_env_file(str(path)) == expected
    assert domain.read_env_file(str(tmp_path / "missing")) == {}


# ---------------------------------------------------------------------------
# The point of the exercise: the domain is written down once
# ---------------------------------------------------------------------------

def test_the_domain_literal_lives_in_exactly_one_module():
    """Every component must read the domain, not spell it out again."""
    root = Path(__file__).resolve().parent.parent
    components = (
        "gateway.py",
        "core/agent.py",
        "core/mcp_tools.py",
        "core/server.py",
        "rhm_sync.py",
        "rhm_watcher.py",
    )
    offenders = [
        name
        for name in components
        if "mcp.natureonzoom.win" in (root / name).read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "the public domain must only be declared in core/domain.py, found in: %s" % offenders
    )
    assert "mcp.natureonzoom.win" in (root / "core" / "domain.py").read_text(encoding="utf-8")
