"""Termux (Android) support: what a phone changes, and what it must not change.

A phone is the one target that is neither "just Linux" nor served by the Windows
installer:

* Termux is not FHS-compliant - there is no ``/etc``, no root and no sudo, and its
  binaries link against Android's bionic libc - so the domain file has to move to
  the per-user directory and ``uv``'s glibc archives are unusable there;
* Android answers ``localhost`` to ``gethostname(2)`` on *every* device, so the
  node name has to come from the device model: the gateway keeps one tunnel per
  name, and two phones claiming one name would evict each other forever;
* there is no systemd, so ``install.sh`` supervises the node with runit
  (``termux-services``) and autostarts it with the ``Termux:Boot`` app.

The installer half of this is exercised against a *simulated* Termux environment
in dry-run mode, which is the only way to reach that branch from a non-Android
machine.
"""

import os
import shutil
import subprocess
import sys

import pytest

from core import agent, domain, paths

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(REPO, "install.sh")

#: The Termux root, as Termux itself sets $PREFIX.
TERMUX_PREFIX = "/data/data/com.termux/files/usr"
#: The application data directory Termux detection falls back to.
TERMUX_DATA_DIR = "/data/data/com.termux/files/usr/bin"

#: A machine that really is a phone detects Termux no matter what the environment
#: says, so the negative cases can only be asserted elsewhere.
ON_ANDROID = os.path.isdir(TERMUX_DATA_DIR)


@pytest.fixture(autouse=True)
def no_leftover_platform_environment(monkeypatch):
    """No test here may leave a fake TERMUX_VERSION/PREFIX behind for the suite."""
    for key in ("TERMUX_VERSION", "PREFIX"):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@pytest.mark.skipif(ON_ANDROID, reason="a real Termux is always detected")
def test_an_unrelated_prefix_is_not_a_phone(monkeypatch):
    """Many build systems export PREFIX; only com.termux means Termux."""
    monkeypatch.setenv("PREFIX", "/usr/local")
    assert domain.is_termux() is False


@pytest.mark.skipif(ON_ANDROID, reason="a real Termux is always detected")
@pytest.mark.parametrize("signal", ("TERMUX_VERSION", "PREFIX"))
def test_either_platform_signal_detects_termux(monkeypatch, signal):
    monkeypatch.setenv(signal, "0.118.0" if signal == "TERMUX_VERSION" else TERMUX_PREFIX)
    assert domain.is_termux() is True


# ---------------------------------------------------------------------------
# The domain file
# ---------------------------------------------------------------------------

@pytest.mark.skipif(ON_ANDROID, reason="a real Termux already uses the HOME path")
def test_the_domain_file_follows_the_platform(monkeypatch, tmp_path):
    """Linux keeps host-level configuration in /etc; a phone cannot have one."""
    home = str(tmp_path)
    monkeypatch.setattr(domain.os.path, "expanduser", lambda _: home)
    per_user = os.path.join(home, ".config", "remote-harness-mcp", "domain.env")

    if os.name == "nt":
        assert domain.default_domain_file() == per_user
    else:
        # On Linux the host-level directory is the answer. It is the current name,
        # or the pre-rename one when that is the only one present on the host.
        expected_dir = paths.etc_dir()
        assert expected_dir in ("/etc/%s" % paths.APP_NAME, "/etc/%s" % paths.LEGACY_APP_NAME)
        assert domain.default_domain_file() == os.path.join(expected_dir, "domain.env")

    # On the phone the per-user path is the answer on every host, which is what
    # install.sh resolves as well: the installer and the node must not disagree
    # about where a custom domain is configured.
    monkeypatch.setenv("TERMUX_VERSION", "0.118.0")
    assert domain.default_domain_file() == per_user


# ---------------------------------------------------------------------------
# The node name
# ---------------------------------------------------------------------------

def test_a_phone_name_comes_from_the_device_model(monkeypatch):
    """localhost is not a name: it is what every Android device answers."""
    monkeypatch.setattr(agent.socket, "gethostname", lambda: "localhost")
    monkeypatch.setenv("TERMUX_VERSION", "0.118.0")
    monkeypatch.setattr(agent, "android_device_name", lambda: "pixel7pro")
    assert agent.default_node_name() == "pixel7pro"

    # A device that does not answer getprop still must not claim "localhost".
    monkeypatch.setattr(agent, "android_device_name", lambda: "")
    assert agent.default_node_name() == "node"


def test_android_naming_does_not_rename_a_real_host(monkeypatch):
    """The Android rule is a fallback, not a rewrite of a machine's own name."""
    monkeypatch.setenv("TERMUX_VERSION", "0.118.0")
    monkeypatch.setattr(agent.socket, "gethostname", lambda: "Studio-PC")
    monkeypatch.setattr(agent, "android_device_name", lambda: "pixel7pro")
    assert agent.default_node_name() == "studio-pc"


def test_the_device_model_is_sanitised(monkeypatch):
    completed = subprocess.CompletedProcess(["getprop"], 0, b"Pixel 7 Pro\n", b"")
    monkeypatch.setattr(agent.subprocess, "run", lambda *args, **kwargs: completed)
    assert agent.android_device_name() == "pixel7pro"


def test_a_missing_getprop_yields_no_name(monkeypatch):
    def explode(*args, **kwargs):
        raise FileNotFoundError("getprop")

    monkeypatch.setattr(agent.subprocess, "run", explode)
    assert agent.android_device_name() == ""


# ---------------------------------------------------------------------------
# install.sh: the Termux branch
# ---------------------------------------------------------------------------

def _bash() -> str:
    """A usable bash: PATH on POSIX, Git for Windows' bash on Windows.

    Never ``C:\\WINDOWS\\system32\\bash.exe``, which is the WSL launcher and would
    read the wrong files (core.mcp_tools makes the same distinction).
    """
    if os.name != "nt":
        return shutil.which("bash") or ""
    try:
        from core import mcp_tools

        return mcp_tools._git_bash_windows() or ""
    except Exception:
        return ""


BASH = _bash()
needs_bash = pytest.mark.skipif(not BASH, reason="bash is required for the installer checks")


def _dry_run(extra_env: dict) -> str:
    """Run install.sh --dry-run with a deterministic, network-free environment."""
    environment = dict(os.environ)
    for key in ("TERMUX_VERSION", "PREFIX", "MESH_PUBLIC_URL", "AGY_PUBLIC_BASE_URL"):
        environment.pop(key, None)
    # A domain that is always resolvable and never read from this machine's own
    # configuration, so the assertion cannot depend on whose laptop runs it.
    environment["MESH_PUBLIC_URL"] = "https://mesh.example.com"
    environment["MESH_DOMAIN_FILE"] = os.path.join(
        environment.get("TEMP") or environment.get("TMPDIR") or "/tmp", "absent-domain.env")
    environment.update(extra_env)

    result = subprocess.run(
        [BASH, INSTALLER, "--dry-run", "--mode=tunnel", "--user=pixel7pro", "--lang=en"],
        capture_output=True, text=True, env=environment, cwd=REPO, timeout=180)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


@needs_bash
def test_a_simulated_phone_takes_the_termux_branch(tmp_path):
    # An empty MESH_DOMAIN_FILE means "use the platform default", which is the
    # path asserted below ($HOME/.config/... on Android, never /etc).
    output = _dry_run({"TERMUX_VERSION": "0.118.0", "PREFIX": TERMUX_PREFIX,
                       "HOME": str(tmp_path), "MESH_DOMAIN_FILE": ""})

    assert "[DRY-RUN] Platform: Termux (Android)" in output
    assert "Android phone/tablet (Termux)" in output
    # The per-user domain file: /etc is not writable (and not present) on a phone.
    # Git Bash translates the Windows $HOME into its own /tmp mount, so the path is
    # compared by shape (and by the home directory it came from), not verbatim.
    domain_line = next(line for line in output.splitlines()
                       if line.startswith("[DRY-RUN] Domain file:"))
    domain_path = domain_line.split(":", 1)[1].strip().replace("\\", "/")
    assert domain_path.endswith(".config/remote-harness-mcp/domain.env")
    assert not domain_path.startswith("/etc")
    assert os.path.basename(str(tmp_path)) in domain_path
    # No systemd unit is installed on a phone, so the installer must not report one.
    assert "Systemd service and dependencies check" not in output
    assert "Unit file" not in output


@needs_bash
def test_without_termux_the_unchanged_linux_path_is_still_taken(tmp_path):
    output = _dry_run({"HOME": str(tmp_path)})

    assert "Termux" not in output
    assert "Systemd service and dependencies check: OK" in output


def test_the_termux_packages_never_go_through_sudo():
    """`run_privileged` is a no-op on a phone: no root, no sudo.

    Wrapping the Termux packages in it would install nothing and still let the
    installer continue, which is exactly the silent failure this project refuses.
    """
    with open(INSTALLER, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    assert "pkg install -y python python-pip python-ensurepip-wheels curl ca-certificates" in text
    assert "run_privileged pkg" not in text
    # The Android autostart is runit plus the Termux:Boot app, not a unit file.
    assert 'configure_termux_node "rhm-agent" "core.agent"' in text
    assert 'configure_termux_node "rhm-standalone" "core.server"' in text
    assert '"$HOME/.termux/boot/${name}.sh"' in text
    assert "termux-wake-lock" in text


# ---------------------------------------------------------------------------
# A full install against a simulated phone
# ---------------------------------------------------------------------------
#
# This is the only test that runs the Termux branch to completion: registration,
# agent.env, the launcher, the runit service and the Termux:Boot entry. The Android
# tools it needs do not exist on a developer machine, so they are shimmed - and the
# network is shimmed with them, which keeps the test offline and deterministic.

try:  # the node's one external dependency; without it the installer tries the network
    import websockets  # noqa: F401

    HAS_WEBSOCKETS = True
except Exception:  # pragma: no cover - depends on how the suite was installed
    HAS_WEBSOCKETS = False

#: The answer the shimmed gateway gives to /api/register.
REGISTRATION_JSON = '{"username": "pixel7pro", "token": "tok-termux-e2e"}'
#: The MCP link the installer must print and copy.
EXPECTED_URL = "https://mesh.example.com/sse?user=pixel7pro&token=tok-termux-e2e"


def _posix(path) -> str:
    """A path Git Bash can use: ``D:\\x\\y`` -> ``/d/x/y`` (unchanged elsewhere)."""
    text = str(path).replace("\\", "/")
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


def _shim(directory, name: str, body: str):
    script = directory / name
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    os.chmod(script, 0o755)
    return script


@pytest.fixture
def fake_phone(tmp_path):
    """A simulated phone: shimmed Termux tools, an isolated $HOME and $PREFIX."""
    home = tmp_path / "home"
    prefix = tmp_path / "prefix"
    shim = tmp_path / "shim"
    for directory in (home, prefix / "bin", shim):
        directory.mkdir(parents=True, exist_ok=True)

    log = tmp_path / "shim.log"
    clipboard = tmp_path / "clipboard.txt"

    # The interpreter itself: the real one, reached through a Termux-style name.
    _shim(shim, "python3", 'exec "%s" "$@"\n' % _posix(sys.executable))
    # pkg: records what the installer asks for. It must never see python3-pip.
    _shim(shim, "pkg", 'echo "pkg $*" >> "%s"\nexit 0\n' % _posix(log))
    # Android properties: the device model is the node name.
    _shim(shim, "getprop",
          'case "$1" in\n'
          '  ro.product.model) echo "Pixel 7 Pro" ;;\n'
          '  ro.build.version.release) echo "14" ;;\n'
          '  *) echo "" ;;\n'
          'esac\nexit 0\n')
    # The gateway: registration only. The node code already sits in the checkout,
    # so no download is attempted. A `-w` request is the standalone health probe,
    # which asks for an HTTP status and must see 200.
    _shim(shim, "curl",
          'case "$*" in\n'
          '  *-w*) echo "200" ;;\n'
          "  *) echo '%s' ;;\n"
          'esac\nexit 0\n' % REGISTRATION_JSON)
    _shim(shim, "termux-clipboard-set", 'cat > "%s"\n' % _posix(clipboard))
    # termux-services and the process check: runit "supervises" the node.
    for name in ("sv", "runsvdir", "sv-enable", "service-daemon", "pgrep", "pkill"):
        _shim(shim, name, "exit 0\n")

    return {"home": home, "prefix": prefix, "shim": shim, "log": log,
            "clipboard": clipboard, "root": tmp_path}


def _install_on(phone, *extra_args, installer=None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    for key in ("TERMUX_VERSION", "PREFIX", "MESH_PUBLIC_URL", "AGY_PUBLIC_BASE_URL"):
        environment.pop(key, None)
    environment.update({
        # POSIX forms: install.sh only ever sees these paths from inside bash.
        "HOME": _posix(phone["home"]),
        "TERMUX_VERSION": "0.118.0",
        "PREFIX": _posix(phone["prefix"]),
        "MESH_DOMAIN_FILE": "",
        "MESH_PUBLIC_URL": "https://mesh.example.com",
    })
    # The shims are prepended INSIDE the shell, not through the child's PATH: Git
    # Bash puts its own /mingw64/bin first at startup, so a PATH entry would lose to
    # the real curl (and the test would try to reach the network). Every shim,
    # including curl, is therefore guaranteed to win.
    script = 'export PATH="%s:$PATH"; exec bash "%s" --lang=en %s' % (
        _posix(phone["shim"]), _posix(installer or INSTALLER), " ".join(extra_args))
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                          env=environment, cwd=REPO, timeout=300)


def _installer_alone(tmp_path):
    """The ``curl … | bash`` case: install.sh by itself, with no core/ beside it."""
    isolated = tmp_path / "piped"
    isolated.mkdir()
    copied = isolated / "install.sh"
    shutil.copy(INSTALLER, copied)
    return copied


@needs_bash
@pytest.mark.skipif(not HAS_WEBSOCKETS, reason="websockets must be importable, or the installer goes to the network")
def test_a_full_termux_install_lays_out_runit_and_the_boot_entry(fake_phone):
    result = _install_on(fake_phone, "--quick")
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout

    # The phone was recognised, and the supervisor was found and enabled.
    assert "Android phone/tablet (Termux)" in output
    assert "runit (termux-services) supervises the node" in output
    assert "Termux (Android): what to check on the phone" in output

    config_dir = fake_phone["home"] / ".config" / "remote-harness-mcp"
    agent_env = config_dir / "agent.env"
    assert agent_env.is_file()
    env_text = agent_env.read_text(encoding="utf-8")
    assert "MESH_USER=pixel7pro" in env_text
    assert "MESH_TOKEN=tok-termux-e2e" in env_text
    assert "MESH_GATEWAY=mesh.example.com" in env_text

    # The launcher: absolute interpreter, the node module, the stored environment.
    launcher = config_dir / "rhm-agent.sh"
    launcher_text = launcher.read_text(encoding="utf-8")
    assert launcher_text.startswith("#!/")
    assert "core.agent" in launcher_text
    assert _posix(fake_phone["prefix"]) + "/bin/sh" in launcher_text
    assert "agent.env" in launcher_text
    assert os.access(launcher, os.X_OK)

    # The Termux:Boot entry: wake lock, then the supervisor.
    boot = fake_phone["home"] / ".termux" / "boot" / "rhm-agent.sh"
    boot_text = boot.read_text(encoding="utf-8")
    assert "termux-wake-lock" in boot_text
    assert 'sv up "rhm-agent"' in boot_text
    assert "rhm-agent.sh" in boot_text, "the boot entry must start the launcher"
    assert os.access(boot, os.X_OK)

    # The runit service and its logger.
    service = fake_phone["prefix"] / "var" / "service" / "rhm-agent"
    run_text = (service / "run").read_text(encoding="utf-8")
    assert "rhm-agent.sh" in run_text
    assert os.access(service / "run", os.X_OK)
    # runit refuses to supervise a service without log/run, so one must exist -
    # the svlogger symlink on a normal phone, the equivalent script elsewhere.
    logger = service / "log" / "run"
    assert logger.exists(), "runit needs a log/run to keep logs"
    assert "svlogger" in logger.read_text(encoding="utf-8") or logger.is_symlink()

    # Everything generated must at least parse as a shell script.
    for script in (launcher, boot, service / "run"):
        parsed = subprocess.run([BASH, "-n", _posix(script)],
                                capture_output=True, text=True, timeout=60)
        assert parsed.returncode == 0, "%s does not parse: %s" % (script, parsed.stderr)

    # Packages: Termux names, never the Debian ones, and never through sudo.
    packages = fake_phone["log"].read_text(encoding="utf-8")
    assert "pkg install" in packages
    assert "python3-pip" not in packages and "python3-venv" not in packages

    # The link reached both stdout and the Android clipboard.
    assert EXPECTED_URL in output
    assert fake_phone["clipboard"].read_text(encoding="utf-8").strip() == EXPECTED_URL

    # No systemd unit was written: Android has none.
    assert not (fake_phone["home"] / ".config" / "systemd").exists()


@needs_bash
@pytest.mark.skipif(not HAS_WEBSOCKETS, reason="websockets must be importable, or the installer goes to the network")
def test_the_fallbacks_are_used_when_termux_has_no_runit(fake_phone):
    """Without termux-services the node still runs - and the installer says so."""
    (fake_phone["shim"] / "sv").unlink()
    (fake_phone["shim"] / "runsvdir").unlink()

    result = _install_on(fake_phone, "--quick")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "termux-services is not installed" in result.stdout

    # The boot entry still falls back to the launcher itself.
    boot = fake_phone["home"] / ".termux" / "boot" / "rhm-agent.sh"
    assert "rhm-agent.boot.log" in boot.read_text(encoding="utf-8")
    assert not (fake_phone["prefix"] / "var" / "service" / "rhm-agent").exists()


@needs_bash
@pytest.mark.skipif(not HAS_WEBSOCKETS, reason="websockets must be importable, or the installer goes to the network")
def test_standalone_on_a_phone_gets_its_own_service_and_arguments(fake_phone):
    """`--mode=standalone` shares the Termux autostart but not its argv."""
    result = _install_on(fake_phone, "--mode=standalone", "--port=8096", "--token=tok-standalone")
    assert result.returncode == 0, result.stdout + result.stderr

    launcher = fake_phone["home"] / ".config" / "remote-harness-mcp" / "rhm-standalone.sh"
    launcher_text = launcher.read_text(encoding="utf-8")
    # The module and its arguments, and never the tunnel module.
    assert "-m core.server --host 127.0.0.1 --port=8096" in launcher_text
    assert "core.agent" not in launcher_text
    # --token is honoured through the 0600 env file, not baked into the launcher.
    assert "standalone.env" in launcher_text
    assert "tok-standalone" not in launcher_text
    assert (fake_phone["home"] / ".config" / "remote-harness-mcp" / "standalone.env").exists()

    # The service and the boot entry follow the service name.
    assert (fake_phone["prefix"] / "var" / "service" / "rhm-standalone" / "run").is_file()
    assert (fake_phone["home"] / ".termux" / "boot" / "rhm-standalone.sh").is_file()
    assert not (fake_phone["prefix"] / "var" / "service" / "rhm-agent").exists()

    # The local URL is reachable only inside the phone, and the installer says so.
    assert "http://localhost:8096/sse" in result.stdout
    assert "Local server on a phone is reachable inside Termux only" not in result.stdout
    assert "Termux (Android): what to check on the phone" in result.stdout


# ---------------------------------------------------------------------------
# A gateway this device cannot resolve
# ---------------------------------------------------------------------------
#
# The real-world Android failure: the gateway answers only inside the operator's
# network (Tailscale, a VPN, a DNS override on the laptop), so the phone on mobile
# data gets "curl: (6) Could not resolve host". Before this guard the installer
# silently downloaded nothing and then reported "Failed to obtain authentication
# token", which blames the gateway instead of the missing DNS record.

@needs_bash
def test_an_unresolvable_gateway_is_reported_before_anything_is_written(fake_phone, tmp_path):
    _shim(fake_phone["shim"], "curl", "exit 6\n")   # CURLE_COULDNT_RESOLVE_HOST
    installer = _installer_alone(tmp_path)

    result = _install_on(fake_phone, "--quick", "--domain=mesh.example.com",
                         installer=installer)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "cannot resolve the gateway name" in output
    assert "Tailscale" in output, "the message must name the usual cause"
    # Nothing was created - no code, no config, no launcher, no service.
    assert not (fake_phone["home"] / ".remote-harness-mcp").exists()
    assert not (fake_phone["home"] / ".config" / "remote-harness-mcp").exists()
    assert not (fake_phone["prefix"] / "var" / "service").exists()


@needs_bash
def test_a_bootstrap_that_downloads_nothing_stops_before_writing_anything(fake_phone, tmp_path):
    """A resolvable name that serves no code (404) must not produce a broken node."""
    _shim(fake_phone["shim"], "curl", "exit 22\n")  # HTTP error for every request
    installer = _installer_alone(tmp_path)

    result = _install_on(fake_phone, "--quick", "--domain=mesh.example.com",
                         installer=installer)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "Could not download the node code" in output
    assert not (fake_phone["home"] / ".config" / "remote-harness-mcp").exists()


@needs_bash
def test_a_dry_run_stays_offline_even_with_a_broken_gateway(fake_phone):
    """--dry-run changes nothing and asks nobody: it must survive a dead DNS."""
    _shim(fake_phone["shim"], "curl", "exit 6\n")

    result = _install_on(fake_phone, "--dry-run", "--mode=tunnel", "--user=pixel7pro")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[DRY-RUN] Platform: Termux (Android)" in result.stdout

