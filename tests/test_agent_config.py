"""The agent's MESH_* configuration comes from the environment or from the file.

``install.sh`` / ``install.ps1`` write gateway, node name and token into
``agent.env``. Extra keys in the same file (MESH_SHELL on Windows,
MESH_WORKSPACE, MESH_READ_ONLY, ...) must reach the process too, otherwise a
Windows node cannot switch its command shell without editing the launcher.
"""

import os

import pytest

from core import agent, domain

#: Every key ``_load_settings`` may export, plus the config-file pointer itself.
_MESH_KEYS = (
    "MESH_GATEWAY",
    "MESH_USER",
    "MESH_TOKEN",
    "MESH_SHELL",
    "MESH_READ_ONLY",
    "MESH_CONFIG_FILE",
    # ``_load_settings`` exports any MESH_* key it finds in agent.env, and the
    # domain keys decide the tunnel host before MESH_GATEWAY is consulted.
    "MESH_PUBLIC_URL",
    "AGY_PUBLIC_BASE_URL",
    "MESH_DOMAIN_FILE",
    "UNRELATED",
)


@pytest.fixture(autouse=True)
def restore_process_environment(tmp_path, monkeypatch):
    """Undo the MESH_* variables ``_load_settings`` exports into ``os.environ``.

    ``monkeypatch.delenv`` cannot cover this: it only restores keys that already
    existed, so a key the test *deletes* and the code *creates* survives the
    test. A leaked ``MESH_SHELL=cmd`` then changes the shell used by every later
    ``bash_exec``/``run_job`` test in the session, which made unrelated failures
    appear only in a full-suite run.

    The domain is isolated the same way :mod:`tests.test_domain` isolates it: the
    real ``domain.env`` of the machine running the tests (which the installer
    writes) must not decide what ``MESH_GATEWAY`` means here, or these tests
    would pass or fail depending on whose laptop they run on.
    """
    for key in ("MESH_PUBLIC_URL", "AGY_PUBLIC_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MESH_DOMAIN_FILE", str(tmp_path / "absent-domain.env"))
    domain.reset()

    saved = {key: os.environ.get(key) for key in _MESH_KEYS}
    yield
    domain.reset()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_env_file_values_are_exported_for_every_mesh_key(tmp_path, monkeypatch):
    config = tmp_path / "agent.env"
    config.write_text(
        "MESH_GATEWAY=example.test\n"
        "MESH_USER=node-one\n"
        "MESH_TOKEN=tok\n"
        "MESH_SHELL=cmd\n"
        "MESH_READ_ONLY=1\n"
        "UNRELATED=ignored\n"
    )
    monkeypatch.setenv("MESH_CONFIG_FILE", str(config))
    for key in ("MESH_GATEWAY", "MESH_USER", "MESH_TOKEN", "MESH_SHELL",
                "MESH_READ_ONLY", "UNRELATED"):
        monkeypatch.delenv(key, raising=False)

    settings = agent._load_settings()

    assert settings == {"gateway": "example.test", "user": "node-one", "token": "tok"}
    assert os.environ["MESH_SHELL"] == "cmd"
    assert os.environ["MESH_READ_ONLY"] == "1"
    assert "UNRELATED" not in os.environ, "only MESH_* keys may be exported"


def test_environment_wins_over_the_file(tmp_path, monkeypatch):
    config = tmp_path / "agent.env"
    config.write_text("MESH_GATEWAY=from-file\nMESH_SHELL=cmd\n")
    monkeypatch.setenv("MESH_CONFIG_FILE", str(config))
    monkeypatch.setenv("MESH_GATEWAY", "from-env")
    monkeypatch.setenv("MESH_SHELL", "git-bash")

    settings = agent._load_settings()

    assert settings["gateway"] == "from-env"
    assert os.environ["MESH_SHELL"] == "git-bash"


def test_mesh_shell_from_the_file_selects_the_shell(tmp_path, monkeypatch):
    """The value from agent.env reaches command_shell(), not just os.environ."""
    from core import mcp_tools

    config = tmp_path / "agent.env"
    config.write_text("MESH_SHELL=cmd\nMESH_USER=node-one\nMESH_TOKEN=tok\n")
    monkeypatch.setenv("MESH_CONFIG_FILE", str(config))
    monkeypatch.delenv("MESH_SHELL", raising=False)
    monkeypatch.setattr(mcp_tools, "_IS_WINDOWS", True)
    monkeypatch.setattr(mcp_tools, "_SHELL_CACHE", None)
    monkeypatch.setattr(mcp_tools, "powershell_argv", lambda: ["powershell.exe", "-Command"])
    try:
        agent._load_settings()
        assert mcp_tools.command_shell()[0] == "cmd"
    finally:
        mcp_tools._SHELL_CACHE = None


# ---------------------------------------------------------------------------
# Node name default
# ---------------------------------------------------------------------------

def test_default_node_name_is_derived_from_the_machine(monkeypatch):
    monkeypatch.setattr(agent.socket, "gethostname", lambda: "MateBook16")
    assert agent.default_node_name() == "matebook16"

    monkeypatch.setattr(agent.socket, "gethostname", lambda: "My Laptop!! #2")
    assert agent.default_node_name() == "mylaptop2"

    monkeypatch.setattr(agent.socket, "gethostname", lambda: "###")
    assert agent.default_node_name() == "node"


def test_unset_mesh_user_falls_back_to_the_hostname_not_a_nickname(tmp_path, monkeypatch):
    """Every node without MESH_USER used to claim one hardcoded nickname.

    The gateway keeps exactly one tunnel per node name and a new tunnel supersedes
    the old one, so unrelated machines would knock each other offline in a loop.
    """
    monkeypatch.setenv("MESH_CONFIG_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("MESH_USER", raising=False)
    monkeypatch.setattr(agent.socket, "gethostname", lambda: "Studio-PC")

    settings = agent._load_settings()

    assert settings["user"] == "studio-pc"
    assert settings["user"] != "levra7"


def test_mesh_user_from_the_file_still_wins(tmp_path, monkeypatch):
    config = tmp_path / "agent.env"
    config.write_text("MESH_USER=chosen-node\nMESH_TOKEN=tok\n")
    monkeypatch.setenv("MESH_CONFIG_FILE", str(config))
    monkeypatch.delenv("MESH_USER", raising=False)
    monkeypatch.delenv("MESH_TOKEN", raising=False)

    settings = agent._load_settings()

    assert settings["user"] == "chosen-node"


# ---------------------------------------------------------------------------
# Misconfiguration and unreachable gateway
# ---------------------------------------------------------------------------

def test_empty_token_is_reported_instead_of_retrying_forever(monkeypatch):
    monkeypatch.setattr(agent, "TOKEN", "")
    problem = agent.configuration_error()
    assert problem and "MESH_TOKEN" in problem and "installer" in problem

    monkeypatch.setattr(agent, "TOKEN", "secret-token")
    assert agent.configuration_error() is None


def test_dns_failure_is_recognised(monkeypatch):
    import socket as _socket

    assert agent._looks_like_dns_failure(_socket.gaierror(-2, "Name or service not known"))
    for text in ("getaddrinfo failed",             # Windows
                 "Temporary failure in name resolution",
                 "nodename nor servname provided"):
        assert agent._looks_like_dns_failure(OSError(text)), text
    assert not agent._looks_like_dns_failure(OSError("Connection refused"))
    assert not agent._looks_like_dns_failure(TimeoutError("timed out"))


def test_tunnel_uri_uses_the_shared_domain_contract(monkeypatch):
    monkeypatch.setattr(agent, "GATEWAY_HOST", "example.test")
    monkeypatch.setattr(agent, "USER", "node-one")
    monkeypatch.setattr(agent, "TOKEN", "tok")
    assert agent.tunnel_uri() == "wss://example.test/ws/tunnel?user=node-one"
