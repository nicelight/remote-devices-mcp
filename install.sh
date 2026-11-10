#!/usr/bin/env bash
# ==============================================================================
#  Remote Harness MCP - Universal Turnkey Installer (v0.1.2)
#  Bilingual: English (Default) & Russian, Device Detection, SSH & Autostart
#  Targets: Linux, macOS, Windows (Git Bash) and Android/Termux (phone/tablet)
# ==============================================================================
set -e
printf "[Remote Harness] Подготовка установщика / Preparing installer…\n" >&2

# Defaults
MODE=""
TLS="none"
PORT="8096"
DOMAIN=""
GATEWAY=""
USERNAME=""
TOKEN=""
DRY_RUN=false
PAIR=false
QUICK=false
SSH_TARGET=""
SSH_PORT="22"
LANG_CHOICE=""
EXPLICIT_LANG=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
[ -z "$SCRIPT_DIR" ] && SCRIPT_DIR="."

# ------------------------------------------------------------------------------
#  PUBLIC DOMAIN - resolved from the one source of truth
#
#  The domain is declared exactly once, in core/domain.py. Everything below asks
#  that same chain, in the same order, and this is the ONLY place in this script
#  that picks a hostname:
#
#    1. an explicit --domain= / --gateway= on the command line;
#    2. MESH_PUBLIC_URL in the environment;
#    3. AGY_PUBLIC_BASE_URL in the environment (legacy alias, same meaning);
#    4. the domain file - MESH_DOMAIN_FILE when set, otherwise
#       /etc/remote-harness-mcp/domain.env, or on Windows/Git Bash
#       %USERPROFILE%\.config\remote-harness-mcp\domain.env;
#    5. $PUBLISHED_DOMAIN - the domain this copy was published with. The repository
#       carries the placeholder __MESH_DOMAIN__ here and deploy_gateway.sh rewrites
#       that line while publishing, so a served copy already knows its domain;
#    6. core/domain.py's DEFAULT_PUBLIC_BASE_URL, but only when that module sits
#       next to this script (a repository checkout, or a node that already
#       bootstrapped). A clone has no published value and refusing to install from
#       a clone would be wrong.
#
#  If none of them yields a real hostname the installer stops: registering a node
#  against a domain that does not resolve produces a node that can never connect
#  and an autostart entry that retries forever.
# ------------------------------------------------------------------------------
DOMAIN_PLACEHOLDER="__MESH_DOMAIN__"
# deploy_gateway.sh rewrites THIS line only (it is anchored on PUBLISHED_DOMAIN=),
# so the placeholder token above survives and still proves whether this copy was
# published. In the repository the two are equal, which means "not published".
PUBLISHED_DOMAIN="__MESH_DOMAIN__"

# ------------------------------------------------------------------------------
#  TERMUX / ANDROID
#
#  Termux is NOT FHS-compliant: there is no /etc, no /bin and no /usr at the
#  usual locations, there is no root and no sudo, and its packages are built
#  against Android's bionic libc. A phone is therefore neither "just Linux" (the
#  Debian package names and the host-level domain file do not exist) nor
#  something the Linux branches can serve unchanged. Everything Termux-specific
#  in this script keys off the single flag below:
#
#    IS_TERMUX      - the one question every branch asks;
#    TERMUX_PREFIX  - $PREFIX, the Termux root (.../com.termux/files/usr);
#    TERMUX_SUPERVISED - set by configure_termux_node when runit supervises.
#
#  Detection never trusts one signal: Termux exports TERMUX_VERSION, its $PREFIX
#  points into com.termux, and its application data directory has a fixed path.
#  A Linux host that happens to export an unrelated $PREFIX is not a phone.
# ------------------------------------------------------------------------------
IS_TERMUX=false
if [ -n "${TERMUX_VERSION:-}" ]; then
    IS_TERMUX=true
fi
if [ "$IS_TERMUX" = false ] && [ -n "${PREFIX:-}" ]; then
    case "$PREFIX" in
        *com.termux*) IS_TERMUX=true ;;
    esac
fi
if [ "$IS_TERMUX" = false ] && [ -d /data/data/com.termux/files/usr/bin ]; then
    IS_TERMUX=true
fi

TERMUX_PREFIX=""
TERMUX_SUPERVISED=false
if [ "$IS_TERMUX" = true ]; then
    TERMUX_PREFIX="${PREFIX:-}"
    if [ -z "$TERMUX_PREFIX" ] || [ ! -d "$TERMUX_PREFIX/bin" ]; then
        TERMUX_PREFIX="/data/data/com.termux/files/usr"
    fi
fi

case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) DEFAULT_DOMAIN_DIR="${USERPROFILE:-$HOME}/.config/remote-harness-mcp" ;;
    *)                    DEFAULT_DOMAIN_DIR="/etc/remote-harness-mcp" ;;
esac
# A phone has no /etc and no root, so the per-user directory is the only place
# that can hold the domain file there. core/domain.py answers the same question
# in default_domain_file(), so the installer and the node never disagree about
# where the domain lives.
if [ "$IS_TERMUX" = true ]; then
    DEFAULT_DOMAIN_DIR="$HOME/.config/remote-harness-mcp"
fi
# The project was renamed from "Antigravity Mesh": a host upgraded from that
# version already has its domain file under the old directory name, and writing a
# second, empty one would silently un-configure the node. The pre-rename
# directory wins while it is the only one that exists - the same rule
# core/paths.py applies for every other runtime directory.
LEGACY_DOMAIN_DIR="${DEFAULT_DOMAIN_DIR%/*}/antigravity-mesh"
if [ ! -d "$DEFAULT_DOMAIN_DIR" ] && [ -d "$LEGACY_DOMAIN_DIR" ]; then
    DEFAULT_DOMAIN_DIR="$LEGACY_DOMAIN_DIR"
fi
DEFAULT_DOMAIN_FILE="${MESH_DOMAIN_FILE:-$DEFAULT_DOMAIN_DIR/domain.env}"

# The command that stores the domain, spelled for the platform it is printed on:
# there is no sudo in Termux and no writable /etc, so the hint must not send a
# phone user to a command that cannot exist there.
domain_file_write_cmd() {
    if [ "$IS_TERMUX" = true ]; then
        printf "mkdir -p %s && echo 'MESH_PUBLIC_URL=%s' > %s" \
            "$DEFAULT_DOMAIN_DIR" "$1" "$DEFAULT_DOMAIN_FILE"
    else
        printf "echo 'MESH_PUBLIC_URL=%s' | sudo tee %s" "$1" "$DEFAULT_DOMAIN_FILE"
    fi
}

# https://mesh.example.com/path/ -> mesh.example.com (the authority only)
normalise_host() {
    printf '%s' "$1" | sed -e 's|^[A-Za-z][A-Za-z0-9+.-]*://||' -e 's|/.*$||' \
        -e 's|^[[:space:]]*||' -e 's|[[:space:]]*$||'
}

# A KEY=VALUE file without its leading UTF-8 BOM (Windows PowerShell writes one).
strip_bom() {
    if [ "$(head -c 3 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "efbbbf" ]; then
        tail -c +4 "$1"
    else
        cat "$1"
    fi
}

# MESH_PUBLIC_URL outranks the legacy alias, whatever their order in the file.
domain_file_value() {
    local file="$1" value=""
    if [ ! -f "$file" ]; then
        return 0
    fi
    value="$(strip_bom "$file" | sed -n 's/^[[:space:]]*MESH_PUBLIC_URL[[:space:]]*=[[:space:]]*//p' | head -n 1)"
    if [ -z "$value" ]; then
        value="$(strip_bom "$file" | sed -n 's/^[[:space:]]*AGY_PUBLIC_BASE_URL[[:space:]]*=[[:space:]]*//p' | head -n 1)"
    fi
    printf '%s' "$value" | tr -d '"' | tr -d "'" | tr -d '\r' | sed -e 's|^[[:space:]]*||' -e 's|[[:space:]]*$||'
}

# The project default, taken from core/domain.py when that file is available here.
module_default_domain() {
    local module="$SCRIPT_DIR/core/domain.py" value=""
    if [ ! -f "$module" ]; then
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        value="$(cd "$SCRIPT_DIR" 2>/dev/null && python3 -c 'from core import domain; print(domain.DEFAULT_PUBLIC_BASE_URL)' 2>/dev/null)" || true
    fi
    if [ -z "$value" ]; then
        value="$(sed -n 's/^DEFAULT_PUBLIC_BASE_URL[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$module" | head -n 1)"
    fi
    printf '%s' "$value"
}

resolve_domain() {
    local explicit_value="${1:-}" candidate=""
    if [ -n "$explicit_value" ]; then
        printf '%s' "$(normalise_host "$explicit_value")"
        return 0
    fi
    for candidate in "${MESH_PUBLIC_URL:-}" "${AGY_PUBLIC_BASE_URL:-}"; do
        if [ -n "$candidate" ]; then
            printf '%s' "$(normalise_host "$candidate")"
            return 0
        fi
    done
    candidate="$(domain_file_value "$DEFAULT_DOMAIN_FILE")"
    if [ -n "$candidate" ]; then
        printf '%s' "$(normalise_host "$candidate")"
        return 0
    fi
    if [ -n "$PUBLISHED_DOMAIN" ] && [ "$PUBLISHED_DOMAIN" != "$DOMAIN_PLACEHOLDER" ]; then
        printf '%s' "$(normalise_host "$PUBLISHED_DOMAIN")"
        return 0
    fi
    candidate="$(module_default_domain)"
    if [ -n "$candidate" ]; then
        printf '%s' "$(normalise_host "$candidate")"
        return 0
    fi
    printf '%s' "$DOMAIN_PLACEHOLDER"
}

# ------------------------------------------------------------------------------
#  PERSIST THE DOMAIN (what the node reads to build its public links)
#
#  core/domain.py resolves the public domain from MESH_PUBLIC_URL in the
#  environment, then from the domain file, then from its built-in default - and it
#  never reads the legacy MESH_GATEWAY that agent.env carries. A node installed
#  with only agent.env therefore dials the right gateway (gateway_host() does
#  honour MESH_GATEWAY) while minting share links on the built-in default: a real
#  Termux node published links on the default relay while its tunnel went to the
#  gateway it was actually registered on.
#
#  This script has already resolved the domain, so it writes it down here - the
#  same thing deploy_gateway.sh does for /etc/remote-harness-mcp/domain.env on the
#  gateway host. The place that decides the domain is then also the place that
#  records it, and the node's links, its tunnel and the gateway agree by
#  construction.
#
#  Never fatal: an unwritable /etc (no sudo, a locked-down box) still leaves a
#  working node - the legacy MESH_GATEWAY keeps the tunnel where it was - so a
#  failure only prints the command the operator can run by hand.
# ------------------------------------------------------------------------------
write_domain_file() {
    local domain existing content module_default parent
    domain="$(normalise_host "${1:-}")"
    if [ -z "$domain" ] || [ "$domain" = "$DOMAIN_PLACEHOLDER" ]; then
        return 0
    fi
    # Never record the project's built-in default. It is a fallback, not a decision:
    # writing it would turn "nothing is configured" into a pinned domain file, and
    # once that file exists core/domain.py stops consulting the legacy MESH_GATEWAY
    # at all - the operator's own agent.env knob would silently go inert. Nothing is
    # lost by skipping it: with no file the resolver's last fallback IS that value,
    # so the node's links and its tunnel still name the same host.
    module_default="$(normalise_host "$(module_default_domain)")"
    if [ -n "$module_default" ] && [ "$domain" = "$module_default" ]; then
        return 0
    fi
    existing="$(normalise_host "$(domain_file_value "$DEFAULT_DOMAIN_FILE")")"
    if [ "$existing" = "$domain" ]; then
        return 0
    fi
    content="MESH_PUBLIC_URL=https://${domain}"
    # The parent of the file that is actually used, not of the default path: with
    # MESH_DOMAIN_FILE pointing into a directory that does not exist yet, the old
    # code created a directory nobody would read and then failed to write.
    parent="$(dirname "$DEFAULT_DOMAIN_FILE")"

    if mkdir -p "$parent" 2>/dev/null \
        && printf '%s\n' "$content" > "$DEFAULT_DOMAIN_FILE" 2>/dev/null; then
        :
    elif run_privileged mkdir -p "$parent" >/dev/null 2>&1 \
        && printf '%s\n' "$content" | run_privileged tee "$DEFAULT_DOMAIN_FILE" >/dev/null 2>&1; then
        :
    else
        if [ "$LANG_CHOICE" = "ru" ]; then
            echo -e "${YELLOW}[!] Не удалось записать домен в ${DEFAULT_DOMAIN_FILE}.${RESET}"
            echo -e "    Ссылки узла будут строиться на встроенном домене по умолчанию."
            echo -e "    Запишите вручную: ${CYAN}$(domain_file_write_cmd "https://${domain}")${RESET}"
        else
            echo -e "${YELLOW}[!] Could not write the domain to ${DEFAULT_DOMAIN_FILE}.${RESET}"
            echo -e "    The node's links would use the built-in default domain."
            echo -e "    Write it by hand: ${CYAN}$(domain_file_write_cmd "https://${domain}")${RESET}"
        fi
        return 0
    fi

    if [ "$LANG_CHOICE" = "ru" ]; then
        if [ -n "$existing" ]; then
            echo -e "${GREEN}[OK] Домен узла: ${content} (${DEFAULT_DOMAIN_FILE}, было https://${existing})${RESET}"
        else
            echo -e "${GREEN}[OK] Домен узла: ${content} (${DEFAULT_DOMAIN_FILE})${RESET}"
        fi
    else
        if [ -n "$existing" ]; then
            echo -e "${GREEN}[OK] Node domain: ${content} (${DEFAULT_DOMAIN_FILE}, was https://${existing})${RESET}"
        else
            echo -e "${GREEN}[OK] Node domain: ${content} (${DEFAULT_DOMAIN_FILE})${RESET}"
        fi
    fi
    return 0
}

# Downloading the node code needs the domain, and so does every cloud-gateway
# mode. A standalone install that already has the code next to it needs neither.
domain_required() {
    if [ ! -f "$SCRIPT_DIR/core/agent.py" ]; then
        return 0
    fi
    if [ "$MODE" != "standalone" ]; then
        return 0
    fi
    return 1
}

CONFIG_DIR="$HOME/.config/remote-harness-mcp"
# An upgraded node keeps the configuration directory its token and agent.env
# already live in; core/paths.py resolves the same preference for the node itself.
LEGACY_CONFIG_DIR="$HOME/.config/antigravity-mesh"
if [ ! -d "$CONFIG_DIR" ] && [ -d "$LEGACY_CONFIG_DIR" ]; then
    CONFIG_DIR="$LEGACY_CONFIG_DIR"
fi
CONFIG_FILE="$CONFIG_DIR/agent.env"

# Colors
BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[1;33m"
BLUE="\033[0;34m"
MAGENTA="\033[0;35m"
RESET="\033[0m"

# ------------------------------------------------------------------------------
#  PYTHON AND ITS DEPENDENCIES
#
#  websockets is the node's ONLY external requirement: core/mcp_tools.py and
#  core/server.py are standard library only. Everything below exists because a
#  fresh or minimal system can fail to provide it for a different reason each time:
#
#    * no Python at all          -> install it from the system package manager;
#    * Python but no pip         -> ensurepip, or uv, which needs no pip;
#    * pip refuses to write      -> PEP 668 "externally managed", or no permission
#                                   on site-packages: --user, then a venv;
#    * no usable package manager -> uv, which fetches its own CPython;
#    * nothing works at all      -> STOP, like the Windows installer does.
#
#  The last point is the important one. This script used to continue with
#  PYTHON_BIN=/usr/bin/python3 whether or not that file existed, wrote a systemd
#  unit or a launchd plist naming it, and reported success. The node then never
#  started and the only evidence was one log line - the exact silent failure the
#  Windows installer already refuses to produce.
#
#  PYTHON_BIN is the single interpreter everything afterwards must use, including
#  the autostart unit. It is NOT necessarily "python3" from PATH: the venv and uv
#  fallbacks both live outside PATH on purpose.
# ------------------------------------------------------------------------------
PYTHON_BIN=""
UV_BIN=""

# Is this a real, runnable Python 3? A name that answers to "command -v" is not
# evidence: a broken shim, a half-removed distro package and a stale symlink all
# pass that test and then fail every call.
python_works() {
    [ -n "$1" ] || return 1
    command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)' >/dev/null 2>&1
}

python_has_websockets() {
    [ -n "$1" ] || return 1
    "$1" -c 'import websockets' >/dev/null 2>&1
}

python_has_pip() {
    [ -n "$1" ] || return 1
    "$1" -m pip --version >/dev/null 2>&1
}

# Every call site after ensure_dependencies uses "$PYTHON_BIN"; this is what finds
# it. The uv-managed interpreter is searched too, because on a machine where uv is
# what provided Python there is no python3 on PATH at all.
resolve_python() {
    local cand uv_root
    for cand in python3 python; do
        if python_works "$cand"; then
            PYTHON_BIN="$(command -v "$cand")"
            return 0
        fi
    done
    uv_root="${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"
    if [ -d "$uv_root" ]; then
        for cand in "$uv_root"/cpython-3*/bin/python3; do
            if [ -x "$cand" ] && python_works "$cand"; then
                PYTHON_BIN="$cand"
                return 0
            fi
        done
    fi
    return 1
}

# sudo when not already root, and nothing at all when neither is available. The old
# code spelled this out per branch and forgot it for apk, so `apk add` silently did
# nothing on a non-root Alpine - the one platform where Python is never preinstalled.
run_privileged() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        return 1
    fi
}

# One system-package install per platform, in a subshell with errexit off: a package
# manager that fails (no network, no sudo, a locked database, a dead mirror) must
# fall through to the next source instead of aborting the installer. Whether Python
# actually arrived is decided by resolve_python afterwards, never by these exit codes.
install_python_packages() {
    (
        set +e
        if [ "$IS_TERMUX" = true ]; then
            # Termux comes first: it HAS apt-get, but the Debian package names below
            # (python3-pip, python3-venv) do not exist there, `pkg` is the wrapper
            # that refreshes the index itself, and run_privileged cannot help -
            # a phone is never root and has no sudo. pip and the venv wheels are
            # separate packages, and python3 is a virtual name provided by python.
            pkg install -y python python-pip python-ensurepip-wheels curl ca-certificates
        elif command -v apt-get >/dev/null 2>&1; then
            run_privileged apt-get update -qq
            run_privileged apt-get install -y -qq python3 python3-pip python3-venv curl ca-certificates
        elif command -v dnf >/dev/null 2>&1; then
            run_privileged dnf install -y -q python3 python3-pip curl ca-certificates
        elif command -v yum >/dev/null 2>&1; then
            run_privileged yum install -y -q python3 python3-pip curl ca-certificates
        elif command -v zypper >/dev/null 2>&1; then
            run_privileged zypper --non-interactive install python3 python3-pip curl ca-certificates
        elif command -v pacman >/dev/null 2>&1; then
            run_privileged pacman -Sy --noconfirm python python-pip curl ca-certificates
        elif command -v apk >/dev/null 2>&1; then
            run_privileged apk add --no-cache python3 py3-pip curl ca-certificates
        elif command -v xbps-install >/dev/null 2>&1; then
            run_privileged xbps-install -Sy python3 python3-pip curl ca-certificates
        elif [ "$(uname -s 2>/dev/null)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
            brew install python3
        else
            return 1
        fi
        return 0
    )
}

# curl is the script's transport, not a Python dependency: the bootstrap download,
# node registration and the standalone health check all go through it. A machine can
# have a perfectly good Python 3 and no curl at all (a minimal WSL image, a slim
# container, a phone where only python was installed), and install_python_packages()
# never runs in that case - it is called only when Python itself is missing. So the
# tool is asked for separately, BEFORE the step that needs it.
install_helper_packages() {
    (
        set +e
        if [ "$IS_TERMUX" = true ]; then
            pkg install -y curl ca-certificates
        elif command -v apt-get >/dev/null 2>&1; then
            run_privileged apt-get update -qq
            run_privileged apt-get install -y -qq curl ca-certificates
        elif command -v dnf >/dev/null 2>&1; then
            run_privileged dnf install -y -q curl ca-certificates
        elif command -v yum >/dev/null 2>&1; then
            run_privileged yum install -y -q curl ca-certificates
        elif command -v zypper >/dev/null 2>&1; then
            run_privileged zypper --non-interactive install curl ca-certificates
        elif command -v pacman >/dev/null 2>&1; then
            run_privileged pacman -Sy --noconfirm curl ca-certificates
        elif command -v apk >/dev/null 2>&1; then
            run_privileged apk add --no-cache curl ca-certificates
        elif command -v xbps-install >/dev/null 2>&1; then
            run_privileged xbps-install -Sy curl ca-certificates
        elif [ "$(uname -s 2>/dev/null)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
            brew install curl
        else
            return 1
        fi
        return 0
    )
}

# True when curl is usable. Installs it if it is not, and fails closed when it cannot
# be obtained: without it the node can never be registered, and the alternative - the
# raw "curl: command not found" at the registration line - names neither the cause nor
# the fix. Called before the bootstrap download, before registration and before the
# standalone health check.
ensure_curl() {
    command -v curl >/dev/null 2>&1 && return 0
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "\033[1;33m[!] curl не найден. Устанавливаю пакеты...\033[0m"
    else
        echo -e "\033[1;33m[!] curl not found. Installing it...\033[0m"
    fi
    install_helper_packages || true
    command -v curl >/dev/null 2>&1 && return 0

    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "\033[1;31m[!] Без curl установка невозможна.\033[0m" >&2
        echo -e "    Через него скачивается код узла, регистрируется узел и проверяется локальный порт." >&2
        if [ "$IS_TERMUX" = true ]; then
            echo -e "    Установите вручную и повторите: ${CYAN}pkg install curl${RESET}" >&2
        else
            echo -e "    Установите вручную и повторите: ${CYAN}sudo apt install curl${RESET}   # или ваш менеджер пакетов" >&2
        fi
        echo -e "    Остановка: без curl ничего не создано." >&2
    else
        echo -e "\033[1;31m[!] curl is required and could not be installed.\033[0m" >&2
        echo -e "    It downloads the node code, registers the node and checks the local port." >&2
        if [ "$IS_TERMUX" = true ]; then
            echo -e "    Install it and re-run: ${CYAN}pkg install curl${RESET}" >&2
        else
            echo -e "    Install it and re-run: ${CYAN}sudo apt install curl${RESET}   # or your package manager" >&2
        fi
        echo -e "    Stopping: nothing was created without it." >&2
    fi
    return 1
}

# uv is the last resort: one static binary that fetches its own CPython and installs
# packages without pip. The release archive is downloaded and unpacked here rather
# than piping astral.sh's install script into sh - a script that arrives over the
# network and is executed unread is a worse default than an archive this unpacks.
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        UV_BIN="$(command -v uv)"
        return 0
    fi
    if [ -x "$CONFIG_DIR/bin/uv" ]; then
        UV_BIN="$CONFIG_DIR/bin/uv"
        return 0
    fi
    # UV_TARGET is empty on an architecture uv publishes no build for, and asking
    # for the wrong one produces an "Exec format error" that names no cause.
    [ -n "$UV_TARGET" ] || return 1
    command -v curl >/dev/null 2>&1 || return 1
    local tmp
    tmp="$(mktemp -d 2>/dev/null)" || return 1
    if curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-${UV_TARGET}.tar.gz" \
        -o "$tmp/uv.tar.gz" 2>/dev/null; then
        if tar -xzf "$tmp/uv.tar.gz" -C "$tmp" 2>/dev/null && [ -f "$tmp/uv-${UV_TARGET}/uv" ]; then
            mkdir -p "$CONFIG_DIR/bin"
            cp "$tmp/uv-${UV_TARGET}/uv" "$CONFIG_DIR/bin/uv" 2>/dev/null && chmod +x "$CONFIG_DIR/bin/uv"
        fi
    fi
    rm -rf "$tmp"
    if [ -x "$CONFIG_DIR/bin/uv" ]; then
        UV_BIN="$CONFIG_DIR/bin/uv"
        return 0
    fi
    return 1
}

uv_pip_install() {
    [ -n "$UV_BIN" ] || return 1
    "$UV_BIN" pip install --python "$1" websockets >/dev/null 2>&1
}

# The two packages that make a Termux node behave like a supervised Linux node
# rather than a bare process: termux-services (runit: Restart=always) and
# termux-api (every termux-* command the device branch uses; the Termux:API app
# must be installed as well for these to answer). Both are optional - the install
# degrades instead of failing - so this never reports an error.
ensure_termux_extras() {
    [ "$IS_TERMUX" = true ] || return 0
    command -v pkg >/dev/null 2>&1 || return 0
    local missing=""
    command -v sv >/dev/null 2>&1 || missing="termux-services"
    # termux-wake-lock is NOT proof that termux-api is installed: on a real phone
    # (Android 16, OPPO PHY110) that one script existed while termux-battery-status,
    # termux-wifi-connectioninfo, termux-sensor and every other command were
    # missing. Checking the wake lock therefore declared the package present and
    # left the whole device branch dead with nothing to explain it. A command that
    # only termux-api ships is the honest probe.
    command -v termux-battery-status >/dev/null 2>&1 || missing="$missing termux-api"
    # Termux ships pip separately from python, and its interpreter is built
    # --without-ensurepip: without these two packages neither `pip install` nor the
    # venv fallback below can work, and the failure would look like a broken node
    # rather than a missing package.
    if ! python_has_pip "$PYTHON_BIN"; then
        missing="$missing python-pip python-ensurepip-wheels"
    fi
    [ -n "$missing" ] || return 0
    # Unquoted on purpose: this is a package list, and pkg install takes them all.
    # shellcheck disable=SC2086
    pkg install -y $missing >/dev/null 2>&1 || true
    return 0
}

# True when a usable Python 3 is on the machine. Installs one if it is not.
ensure_python() {
    resolve_python && return 0
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "\033[1;33m[!] Python 3 не найден. Автоматическая установка системных пакетов...\033[0m"
    else
        echo -e "\033[1;33m[!] Python 3 not found. Installing system packages...\033[0m"
    fi
    install_python_packages || true
    resolve_python && return 0

    # No usable package manager: uv can still bring an interpreter of its own.
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "\033[1;33m[!] Пакетный менеджер не дал Python. Пробую uv...\033[0m"
    else
        echo -e "\033[1;33m[!] The package manager did not provide Python. Trying uv...\033[0m"
    fi
    if ensure_uv && "$UV_BIN" python install 3.12 >/dev/null 2>&1; then
        resolve_python && return 0
    fi
    return 1
}

ensure_dependencies() {
    if ! ensure_python; then
        # Fail closed. An autostart unit naming an interpreter that does not exist
        # is a node that never connects, and nothing in the output would have said so.
        if [ "$LANG_CHOICE" = "ru" ]; then
            echo -e "\033[1;31m[!] Рабочий Python 3 получить не удалось.\033[0m" >&2
            if [ "$IS_TERMUX" = true ]; then
                echo -e "    Проверено: ${CYAN}pkg${RESET} (пакетный менеджер Termux; uv не используется," >&2
                echo -e "    его сборки линкуются с glibc и на Android/bionic не запускаются)." >&2
                echo -e "    Установите Python 3 вручную и повторите:" >&2
                echo -e "      ${CYAN}pkg install python python-pip${RESET}" >&2
            else
                echo -e "    Проверены: пакетный менеджер (apt/dnf/yum/zypper/pacman/apk/xbps/brew) и uv." >&2
                echo -e "    Установите Python 3 вручную и повторите:" >&2
                echo -e "      ${CYAN}sudo apt install python3 python3-pip python3-venv${RESET}   # или ваш менеджер" >&2
            fi
            echo -e "    Остановка: без интерпретатора автозапуск узла работать не будет, ничего не создано." >&2
        else
            echo -e "\033[1;31m[!] Could not obtain a working Python 3.\033[0m" >&2
            if [ "$IS_TERMUX" = true ]; then
                echo -e "    Tried: ${CYAN}pkg${RESET}, the Termux package manager (uv is skipped there: its" >&2
                echo -e "    builds link against glibc and cannot run on Android's bionic libc)." >&2
                echo -e "    Install Python 3 by hand and re-run:" >&2
                echo -e "      ${CYAN}pkg install python python-pip${RESET}" >&2
            else
                echo -e "    Tried: the system package manager (apt/dnf/yum/zypper/pacman/apk/xbps/brew) and uv." >&2
                echo -e "    Install Python 3 by hand and re-run:" >&2
                echo -e "      ${CYAN}sudo apt install python3 python3-pip python3-venv${RESET}   # or your package manager" >&2
            fi
            echo -e "    Stopping: an autostart entry without an interpreter can never work, nothing was created." >&2
        fi
        exit 1
    fi

    ensure_termux_extras

    if python_has_websockets "$PYTHON_BIN"; then
        return 0
    fi
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo "[1/4] Автономная установка зависимостей (websockets)..."
    else
        echo "[1/4] Installing dependencies (websockets)..."
    fi

    # 1. pip, in both spellings. The --break-system-packages retry is the escape
    #    hatch a PEP 668 distribution documents; without it a Debian 12 or Fedora 38
    #    refuses the install and the message never mentions the flag.
    "$PYTHON_BIN" -m pip install --quiet websockets 2>/dev/null || true
    python_has_websockets "$PYTHON_BIN" && return 0
    "$PYTHON_BIN" -m pip install --quiet --break-system-packages websockets 2>/dev/null || true
    python_has_websockets "$PYTHON_BIN" && return 0

    # 2. --user: site-packages is read-only, the home directory is not.
    "$PYTHON_BIN" -m pip install --quiet --user websockets 2>/dev/null || true
    python_has_websockets "$PYTHON_BIN" && return 0

    # 3. ensurepip: a distribution can ship python3 without a pip at all.
    "$PYTHON_BIN" -m ensurepip --default-pip >/dev/null 2>&1 || true
    "$PYTHON_BIN" -m pip install --quiet websockets 2>/dev/null || true
    python_has_websockets "$PYTHON_BIN" && return 0

    # 4. uv: installs into a managed environment without the interpreter's own pip.
    if ensure_uv; then
        uv_pip_install "$PYTHON_BIN" || true
        python_has_websockets "$PYTHON_BIN" && return 0
    fi

    # 5. venv: needs no write access to the system Python, only the interpreter.
    local venv_dir="$CONFIG_DIR/venv"
    if [ ! -x "$venv_dir/bin/python3" ]; then
        "$PYTHON_BIN" -m venv "$venv_dir" >/dev/null 2>&1 || true
    fi
    if [ -x "$venv_dir/bin/python3" ]; then
        "$venv_dir/bin/python3" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
        "$venv_dir/bin/python3" -m pip install --quiet websockets >/dev/null 2>&1 || true
        if python_has_websockets "$venv_dir/bin/python3"; then
            PYTHON_BIN="$venv_dir/bin/python3"
            return 0
        fi
        if uv_pip_install "$venv_dir/bin/python3" && python_has_websockets "$venv_dir/bin/python3"; then
            PYTHON_BIN="$venv_dir/bin/python3"
            return 0
        fi
    fi

    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "\033[1;31m[!] Не удалось установить websockets автоматически.\033[0m" >&2
        echo -e "    Проверены: pip, pip --break-system-packages, pip --user, ensurepip, uv и venv." >&2
        echo -e "    Выполните вручную: ${CYAN}$PYTHON_BIN -m pip install websockets${RESET}" >&2
        if [ "$IS_TERMUX" = true ]; then
            echo -e "    Termux: ${CYAN}pkg install python-pip python-ensurepip-wheels${RESET}, затем повторите." >&2
        fi
        echo -e "    Остановка: узел без websockets не сможет подключиться к шлюзу." >&2
    else
        echo -e "\033[1;31m[!] Could not install websockets automatically.\033[0m" >&2
        echo -e "    Tried: pip, pip --break-system-packages, pip --user, ensurepip, uv and a venv." >&2
        echo -e "    Run manually: ${CYAN}$PYTHON_BIN -m pip install websockets${RESET}" >&2
        if [ "$IS_TERMUX" = true ]; then
            echo -e "    Termux: ${CYAN}pkg install python-pip python-ensurepip-wheels${RESET}, then re-run." >&2
        fi
        echo -e "    Stopping: a node without websockets can never reach the gateway." >&2
    fi
    exit 1
}


# Parse CLI args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --lang=*)
            LANG_CHOICE="${1#*=}"
            EXPLICIT_LANG=true
            shift
            ;;
        --pair) PAIR=true; shift ;;
        -q|--quick)
            QUICK=true
            shift
            ;;
        --mode=*)
            MODE="${1#*=}"
            shift
            ;;
        --user=*)
            USERNAME="${1#*=}"
            shift
            ;;
        --token=*)
            TOKEN="${1#*=}"
            shift
            ;;
        --gateway=*)
            # Historical alias of --domain=: both are the explicit override.
            DOMAIN="${1#*=}"
            shift
            ;;
        --port=*)
            PORT="${1#*=}"
            shift
            ;;
        --ssh=*)
            SSH_ARG="${1#*=}"
            if [[ "$SSH_ARG" =~ ^([^:]+):([0-9]+)$ ]]; then
                SSH_TARGET="${BASH_REMATCH[1]}"
                SSH_PORT="${BASH_REMATCH[2]}"
            else
                SSH_TARGET="$SSH_ARG"
            fi
            shift
            ;;
        --tls=*)
            TLS="${1#*=}"
            shift
            ;;
        --domain=*)
            # The shared public domain that serves every node. This is NOT a
            # per-device subdomain: nodes are selected with ?user=<node>.
            DOMAIN="${1#*=}"
            GATEWAY="$DOMAIN"
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--lang=en|ru] [-q|--quick] [--mode=tunnel|standalone|gateway] [--user=<node-name>] [--token=<token>] [--domain=<shared-domain>] [--port=<port>] [--ssh=user@host] [--dry-run] [--pair]"
            echo ""
            echo "All nodes use ONE shared domain; the node is selected by ?user=<node-name>:"
            echo "  MCP URL: https://<shared-domain>/sse?user=<node-name>&token=<token>"
            echo ""
            echo "The shared domain is resolved from the one place that declares it, in this order:"
            echo "  --domain= / --gateway=, then MESH_PUBLIC_URL, then AGY_PUBLIC_BASE_URL,"
            echo "  then ${DEFAULT_DOMAIN_FILE}, then the value this copy was published with,"
            echo "  then core/domain.py's default when the repository is present."
            echo ""
            echo "Android/Termux is detected automatically: packages come from pkg, the node is"
            echo "named after the device model, and autostart is a runit service plus the"
            echo "Termux:Boot app instead of systemd (see docs/TERMUX.md)."
            exit 0
            ;;
        *)
            echo -e "${YELLOW}[ERROR] Unknown parameter: $1${RESET}"
            exit 1
            ;;
    esac
done

# Visible before DNS, interpreter discovery or the first bootstrap request.
INSTALL_STAGE=""
INSTALL_HEARTBEAT=""
BOOTSTRAP_STAGE=""
progress_stage() {
    INSTALL_STAGE="$1"
    printf '\n[Remote Harness] %s\n' "$INSTALL_STAGE" >&2
}
stop_progress() {
    if [ -n "$INSTALL_HEARTBEAT" ]; then
        kill "$INSTALL_HEARTBEAT" 2>/dev/null || true
        wait "$INSTALL_HEARTBEAT" 2>/dev/null || true
        INSTALL_HEARTBEAT=""
    fi
}
install_exit() {
    local code=$?
    stop_progress
    if [ -n "$BOOTSTRAP_STAGE" ]; then rm -rf "$BOOTSTRAP_STAGE"; fi
    if [ "$code" -ne 0 ]; then
        printf '[ERROR] %s (exit %s). Исправьте указанную ошибку и запустите установщик снова. / Fix the error above and re-run.\n' "$INSTALL_STAGE" "$code" >&2
    fi
}
trap install_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
run_busy() {
    local label="$1" elapsed=0
    shift
    progress_stage "$label"
    (
        while sleep 4; do
            elapsed=$((elapsed + 4))
            printf '  … %s [%ss]\n' "$label" "$elapsed" >&2
        done
    ) &
    INSTALL_HEARTBEAT=$!
    "$@"
    local result=$?
    stop_progress
    return "$result"
}
progress_stage "Начинаем установку / Starting installation"

# ------------------------------------------------------------------------------
#  Resolve the shared domain, then fetch the node code from it.
#
#  Order matters: the CLI arguments are parsed first (so --domain= counts), the
#  domain is resolved second, and only then is the bootstrap downloaded - from
#  the resolved domain, never from a name baked into this file.
# ------------------------------------------------------------------------------
GATEWAY="$(resolve_domain "$DOMAIN")"

if [ "$GATEWAY" = "$DOMAIN_PLACEHOLDER" ]; then
    if domain_required; then
        echo ""
        if [ "$LANG_CHOICE" = "ru" ]; then
            echo -e "${RED}${BOLD}[ОШИБКА] Домен шлюза не задан.${RESET}"
            echo -e "  В этой копии установщика остался плейсхолдер ${BOLD}${DOMAIN_PLACEHOLDER}${RESET}: она не была опубликована"
            echo -e "  скриптом deploy_gateway.sh, а домена нет ни в окружении, ни в файле ${BOLD}${DEFAULT_DOMAIN_FILE}${RESET}."
            echo -e "  Укажите домен одним из способов:"
            echo -e "    ${CYAN}./install.sh --domain=<общий-домен>${RESET}"
            echo -e "    ${CYAN}MESH_PUBLIC_URL=https://<общий-домен> ./install.sh${RESET}"
            echo -e "    ${CYAN}$(domain_file_write_cmd 'https://<общий-домен>')${RESET}"
            echo -e "  ${YELLOW}Установка остановлена: регистрация на несуществующем домене не выполняется, ничего не создано.${RESET}"
        else
            echo -e "${RED}${BOLD}[ERROR] No gateway domain configured.${RESET}"
            echo -e "  This copy of the installer still carries the ${BOLD}${DOMAIN_PLACEHOLDER}${RESET} placeholder: it was not"
            echo -e "  published by deploy_gateway.sh, and no domain was found in the environment or in ${BOLD}${DEFAULT_DOMAIN_FILE}${RESET}."
            echo -e "  Pass the domain in one of these ways:"
            echo -e "    ${CYAN}./install.sh --domain=<shared-domain>${RESET}"
            echo -e "    ${CYAN}MESH_PUBLIC_URL=https://<shared-domain> ./install.sh${RESET}"
            echo -e "    ${CYAN}$(domain_file_write_cmd 'https://<shared-domain>')${RESET}"
            echo -e "  ${YELLOW}Stopping: a node is never registered against a domain that does not exist, nothing was created.${RESET}"
        fi
        echo ""
        exit 1
    fi
    # Standalone install with the node code already next to this script: no domain
    # is needed, so this is a warning rather than a stop.
    GATEWAY=""
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "${YELLOW}[!] Домен не задан - автономному режиму он не нужен, продолжаю.${RESET}"
    else
        echo -e "${YELLOW}[!] No domain configured - not needed for standalone mode, continuing.${RESET}"
    fi
fi

# ------------------------------------------------------------------------------
#  GATEWAY REACHABILITY (the one preflight that cannot be deferred)
#
#  A node downloads its code from the gateway, registers there, and then dials the
#  same name for its tunnel. A name this device cannot resolve therefore dooms
#  every later step - and the failure used to surface much later, as "Failed to
#  obtain authentication token", which blames the gateway's answer instead of the
#  missing DNS record. Phones are where this bites: a gateway on a private network
#  (Tailscale, a VPN, a DNS override on the operator's laptop) resolves on that
#  laptop and nowhere else, and mobile data resolves nothing private.
#
#  Only curl's exit code 6 ("could not resolve host") stops the install: it is the
#  one answer that cannot become true later in this run. A timeout, a 404 or a 5xx
#  is not fatal - a gateway without /health still serves the tunnel, and refusing
#  to install for that would be wrong.
# ------------------------------------------------------------------------------
require_gateway_dns() {
    [ -n "$GATEWAY" ] || return 0
    command -v curl >/dev/null 2>&1 || return 0
    local rc=0
    curl -fsS -m 8 -o /dev/null "https://${GATEWAY}/health" >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 6 ] || return 0

    if [ "$LANG_CHOICE" = "ru" ]; then
        echo ""
        echo -e "${RED}${BOLD}[ОШИБКА] Имя шлюза не разрешается на этом устройстве: ${GATEWAY}${RESET}"
        echo -e "  DNS не знает такого имени, поэтому ни скачать код узла, ни зарегистрировать его не выйдет."
        echo -e "  Обычно так бывает, когда шлюз живёт в приватной сети (Tailscale, VPN, локальный DNS):"
        echo -e "  на рабочем ноутбуке имя резолвится, а на телефоне в мобильной сети - нет."
        echo -e "  Что делать:"
        echo -e "    • подключить это устройство к той же сети (на Android - приложение Tailscale);"
        echo -e "    • или указать домен, который резолвится публично: ${CYAN}--domain=<домен>${RESET};"
        echo -e "    • проверить вручную: ${CYAN}curl -v https://${GATEWAY}/health${RESET}"
        echo -e "  ${YELLOW}Остановка: без DNS узел не подключится к шлюзу, ничего не создано.${RESET}"
        echo ""
    else
        echo ""
        echo -e "${RED}${BOLD}[ERROR] This device cannot resolve the gateway name: ${GATEWAY}${RESET}"
        echo -e "  DNS does not know the name, so neither the node code nor a registration can arrive."
        echo -e "  This is the usual state of a gateway on a private network (Tailscale, a VPN, a local"
        echo -e "  DNS override): it resolves on the operator's laptop and not on a phone on mobile data."
        echo -e "  What to do:"
        echo -e "    • put this device on the same network (on Android: the Tailscale app);"
        echo -e "    • or name a domain that resolves publicly: ${CYAN}--domain=<domain>${RESET};"
        echo -e "    • check by hand: ${CYAN}curl -v https://${GATEWAY}/health${RESET}"
        echo -e "  ${YELLOW}Stopping: without DNS the node can never reach the gateway, nothing was created.${RESET}"
        echo ""
    fi
    return 1
}

# Bootstrap when piped from curl or run outside the project: the node code is
# fetched from the SAME domain that was just resolved. core/domain.py travels with
# it, because the node asks that module for the domain as well.
if [ ! -f "$SCRIPT_DIR/core/agent.py" ]; then
    # The node code is fetched with curl. Piping this script through curl means curl
    # exists; arriving through wget, a copy-paste or an embedded runner does not.
    run_busy "Проверка загрузчика / Checking downloader" ensure_curl || exit 1
    run_busy "Проверка адреса Gateway / Checking Gateway DNS" require_gateway_dns || exit 1
    BOOTSTRAP_DIR="$HOME/.remote-harness-mcp"
    mkdir -p "$BOOTSTRAP_DIR/core" "$BOOTSTRAP_DIR/skills"
    # EVERY module the node can import, not a hand-picked few: the list below used to
    # stop at the files that existed when it was written, and a fresh phone install
    # then answered "the device layer (core/device.py) is missing from this checkout"
    # while the branch was advertised - the tool was there, its module was not.
    # tests/test_installer_bootstrap.py fails when a core module is missing here.
    BOOTSTRAP_STAGE="$(mktemp -d "$BOOTSTRAP_DIR/.download.XXXXXX")"
    mkdir -p "$BOOTSTRAP_STAGE/core" "$BOOTSTRAP_STAGE/skills"
    downloaded_files=0
    for f in core/__init__.py core/agent.py core/agent_harness.py core/code_cache.py \
             core/device.py core/domain.py core/hooks.py core/mcp_tools.py core/paths.py \
             core/policies.py core/rekey.py core/onboarding.py \
             core/redact.py core/schemas.py core/server.py core/subagents.py core/termux.py \
             core/triggers.py core/updater.py core/version.py core/vitals.py core/web_share.py \
             skills/orchestrator.md; do
        bootstrap_tmp="$BOOTSTRAP_STAGE/${f}.part"
        downloaded_files=$((downloaded_files + 1))
        if run_busy "Загрузка / Download ${downloaded_files}/23: ${f}" \
            curl -fSL --connect-timeout 10 --max-time 60 --silent --show-error "https://${GATEWAY}/${f}" -o "$bootstrap_tmp"; then
            mv "$bootstrap_tmp" "$BOOTSTRAP_STAGE/${f}"
        else
            rm -f "$bootstrap_tmp"
            printf '[ERROR] Не удалось загрузить / Could not download: %s. Проверьте Gateway и сеть. / Check Gateway and network.\n' "$f" >&2
            exit 1
        fi
    done
    unset bootstrap_tmp
    # Commit only after every required file arrived from our own Gateway.
    cp -R "$BOOTSTRAP_STAGE/." "$BOOTSTRAP_DIR/"
    rm -rf "$BOOTSTRAP_STAGE"
    BOOTSTRAP_STAGE=""
    SCRIPT_DIR="$BOOTSTRAP_DIR"
fi

# ------------------------------------------------------------------------------
#  SHARED-DOMAIN CONTRACT (canonical step)
#  Every node is reached through ONE public domain ($GATEWAY); the node is
#  selected by the ?user= query parameter. The installer never mints a
#  per-device subdomain: each extra hostname would need its own DNS record and
#  TLS SAN, and a name missing from the certificate fails the handshake with an
#  opaque "cannot connect to host" error in the Gemini client.
#     sse    https://<shared-domain>/sse?user=<node-name>&token=<token>
#     http   https://<shared-domain>/mcp?user=<node-name>&token=<token>
#     tunnel wss://<shared-domain>/ws/tunnel?user=<node-name>&token=<token>
#  $GATEWAY is read at call time so the interactive menu can still change it.
# ------------------------------------------------------------------------------
node_sse_url() { printf 'https://%s/sse?user=%s&token=%s' "$GATEWAY" "$1" "$2"; }

# Language prompt if interactive and not specified
if [ -z "$LANG_CHOICE" ]; then
    if [ "$QUICK" = false ] && [ -z "$MODE" ] && [ -t 0 ]; then
        echo -e "\n${BOLD}${CYAN}╔════════════════════════════════════════════════════════════════════════╗${RESET}"
        echo -e "${BOLD}${CYAN}║              🌐 LANGUAGE SELECTION / ВЫБОР ЯЗЫКА                       ║${RESET}"
        echo -e "${BOLD}${CYAN}╚════════════════════════════════════════════════════════════════════════╝${RESET}"
        echo -e "  ${GREEN}1)${RESET} English (Default)"
        echo -e "  ${BLUE}2)${RESET} Русский"
        read -rp "Select / Выберите [1]: " LANG_INPUT
        if [ "$LANG_INPUT" = "2" ] || [ "$LANG_INPUT" = "ru" ] || [ "$LANG_INPUT" = "RU" ]; then
            LANG_CHOICE="ru"
        else
            LANG_CHOICE="en"
        fi
    else
        LANG_CHOICE="en"
    fi
fi

# Device detection
# ------------------------------------------------------------------------------
#  ARCHITECTURE
#
#  The system package managers resolve their own architecture, so on Linux this
#  matters for exactly two things: the uv archive, which is published per CPU and
#  answers the wrong one with an unhelpful "Exec format error", and the
#  registration payload, so a gateway operator can see which build a node runs.
#  macOS reports the same machine names as Linux but needs the -apple-darwin assets.
# ------------------------------------------------------------------------------
detect_arch() {
    DETECTED_ARCH="$(uname -m 2>/dev/null || echo unknown)"
    case "$DETECTED_ARCH" in
        x86_64|amd64)        ARCH_LABEL="x86_64 (64-bit)";      UV_TARGET="x86_64-unknown-linux-gnu" ;;
        aarch64|arm64)       ARCH_LABEL="aarch64 (64-bit ARM)"; UV_TARGET="aarch64-unknown-linux-gnu" ;;
        armv7l|armv7|armv6l) ARCH_LABEL="armv7 (32-bit ARM)";   UV_TARGET="armv7-unknown-linux-gnueabihf" ;;
        i386|i486|i586|i686) ARCH_LABEL="x86 (32-bit)";         UV_TARGET="i686-unknown-linux-gnu" ;;
        *)                   ARCH_LABEL="$DETECTED_ARCH";       UV_TARGET="" ;;
    esac
    if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
        case "$DETECTED_ARCH" in
            x86_64) UV_TARGET="x86_64-apple-darwin" ;;
            arm64)  UV_TARGET="aarch64-apple-darwin" ;;
        esac
    fi
    if [ "$IS_TERMUX" = true ]; then
        # uv's archives are glibc/musl builds; neither runs against Android's
        # bionic libc, and asking for one yields an "Exec format error" that names
        # no cause. On a phone the package manager is the only source of Python.
        UV_TARGET=""
    fi
}

# Android system properties. getprop lives in /system/bin on every Android build
# and is readable without root; a missing getprop (or an empty property) simply
# yields an empty value, never a failed install.
android_prop() {
    command -v getprop >/dev/null 2>&1 || return 0
    getprop "$1" 2>/dev/null | tr -d '\r' | head -n 1
}

detect_device() {
    # `|| true` on purpose: a minimal container (and Termux, which has no
    # hostname(1) of its own) answers neither spelling, and an assignment whose
    # substitution fails would abort the whole installer under `set -e`.
    DETECTED_HOSTNAME=$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)
    detect_arch

    if [ "$IS_TERMUX" = true ]; then
        ANDROID_MODEL="$(android_prop ro.product.model)"
        [ -n "$ANDROID_MODEL" ] || ANDROID_MODEL="$(android_prop ro.product.device)"
        ANDROID_RELEASE="$(android_prop ro.build.version.release)"
        [ -n "$ANDROID_RELEASE" ] || ANDROID_RELEASE="$(android_prop ro.build.version.sdk)"
        DETECTED_OS="Android ${ANDROID_RELEASE:-unknown} (Termux)"
        # The phone itself names the node. Termux has no hostname binary, and
        # Android's gethostname(2) answers "localhost" - a name every phone would
        # claim, and the gateway keeps exactly one tunnel per name, so they would
        # knock each other offline in a loop. The device model is stable, and it is
        # sanitised below exactly like a hostname.
        DETECTED_HOSTNAME="${ANDROID_MODEL:-android-node}"
        if [ "$LANG_CHOICE" = "ru" ]; then
            DETECTED_TYPE="Смартфон/планшет Android (Termux)"
        else
            DETECTED_TYPE="Android phone/tablet (Termux)"
        fi
    elif [ "$(uname -s)" = "Darwin" ]; then
        DETECTED_OS="macOS $(sw_vers -productVersion 2>/dev/null || '')"
        if [ "$LANG_CHOICE" = "ru" ]; then
            DETECTED_TYPE="Apple Mac"
            sysctl -n hw.model 2>/dev/null | grep -qi "book" && DETECTED_TYPE="Apple MacBook (Ноутбук)"
        else
            DETECTED_TYPE="Apple Mac"
            sysctl -n hw.model 2>/dev/null | grep -qi "book" && DETECTED_TYPE="Apple MacBook (Laptop)"
        fi
    elif [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        DETECTED_OS="${PRETTY_NAME:-$NAME}"
    else
        DETECTED_OS=$(uname -s)
    fi

    if [ "$(uname -s)" != "Darwin" ] && [ "$IS_TERMUX" != true ]; then
        # The virtualised environments are asked FIRST, before the battery. WSL2
        # forwards the host's battery into /sys/class/power_supply, so the battery
        # test matched inside WSL and reported the user's laptop instead of the
        # subsystem they were actually installing into - the node registered as
        # "Laptop" while running under Windows.
        if [ "$LANG_CHOICE" = "ru" ]; then
            DETECTED_TYPE="Десктоп / Сервер"
            if grep -q -i "microsoft" /proc/version 2>/dev/null; then
                DETECTED_TYPE="WSL (Windows Subsystem for Linux)"
            elif [ -f /.dockerenv ] || grep -q "docker\|containerd" /proc/1/cgroup 2>/dev/null; then
                DETECTED_TYPE="Контейнер (Docker/LXC)"
            elif [ -d /sys/class/power_supply ] && ls /sys/class/power_supply/BAT* 1>/dev/null 2>&1; then
                DETECTED_TYPE="Ноутбук (Laptop)"
            elif command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt -q; then
                DETECTED_TYPE="Облачный сервер / VPS ($(systemd-detect-virt))"
            fi
        else
            DETECTED_TYPE="Desktop / Server"
            if grep -q -i "microsoft" /proc/version 2>/dev/null; then
                DETECTED_TYPE="WSL (Windows Subsystem for Linux)"
            elif [ -f /.dockerenv ] || grep -q "docker\|containerd" /proc/1/cgroup 2>/dev/null; then
                DETECTED_TYPE="Container (Docker/LXC)"
            elif [ -d /sys/class/power_supply ] && ls /sys/class/power_supply/BAT* 1>/dev/null 2>&1; then
                DETECTED_TYPE="Laptop"
            elif command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt -q; then
                DETECTED_TYPE="Cloud VPS ($(systemd-detect-virt))"
            fi
        fi
    fi
}

detect_device

print_device_info() {
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "${CYAN}╔════════════════════════════════════════════════════════════════════════╗${RESET}"
        echo -e "${CYAN}║${RESET} ${BOLD}🔍 Обнаружено устройство:${RESET}"
        echo -e "${CYAN}║${RESET}   • Имя хоста   : ${GREEN}${DETECTED_HOSTNAME}${RESET}"
        echo -e "${CYAN}║${RESET}   • Тип         : ${YELLOW}${DETECTED_TYPE}${RESET}"
        echo -e "${CYAN}║${RESET}   • ОС          : ${DETECTED_OS}"
        echo -e "${CYAN}║${RESET}   • Архитектура : ${ARCH_LABEL}"
        echo -e "${CYAN}╚════════════════════════════════════════════════════════════════════════╝${RESET}"
    else
        echo -e "${CYAN}╔════════════════════════════════════════════════════════════════════════╗${RESET}"
        echo -e "${CYAN}║${RESET} ${BOLD}🔍 Detected Device:${RESET}"
        echo -e "${CYAN}║${RESET}   • Hostname    : ${GREEN}${DETECTED_HOSTNAME}${RESET}"
        echo -e "${CYAN}║${RESET}   • Type        : ${YELLOW}${DETECTED_TYPE}${RESET}"
        echo -e "${CYAN}║${RESET}   • OS          : ${DETECTED_OS}"
        echo -e "${CYAN}║${RESET}   • Architecture: ${ARCH_LABEL}"
        echo -e "${CYAN}╚════════════════════════════════════════════════════════════════════════╝${RESET}"
    fi
}

print_mcp_box() {
    local title="$1"
    local url="$2"
    local copy_msg="$3"

    local max_len=${#title}
    [ ${#url} -gt $max_len ] && max_len=${#url}
    [ -n "$copy_msg" ] && [ ${#copy_msg} -gt $max_len ] && max_len=${#copy_msg}

    local width=$(( max_len + 4 ))
    [ $width -lt 84 ] && width=84

    local dashes=$(printf '%*s' "$width" '' | tr ' ' '-')
    local border="  +${dashes}+"
    local empty="  |$(printf '%*s' "$width" '')|"

    echo -e "${GREEN}${border}${RESET}"
    
    # Title
    local pad_title=$(( width - 2 - ${#title} ))
    printf "  ${GREEN}|${RESET} ${BOLD}%s${RESET}%*s ${GREEN}|${RESET}\n" "$title" "$pad_title" ""

    echo -e "${GREEN}${empty}${RESET}"

    # URL
    local pad_url=$(( width - 2 - ${#url} ))
    printf "  ${GREEN}|${RESET} ${BOLD}${YELLOW}%s${RESET}%*s ${GREEN}|${RESET}\n" "$url" "$pad_url" ""

    echo -e "${GREEN}${empty}${RESET}"

    # Copy message
    if [ -n "$copy_msg" ]; then
        local pad_copy=$(( width - 2 - ${#copy_msg} ))
        printf "  ${GREEN}|${RESET} ${BOLD}${GREEN}%s${RESET}%*s ${GREEN}|${RESET}\n" "$copy_msg" "$pad_copy" ""
    fi

    echo -e "${GREEN}${border}${RESET}"
}

# ------------------------------------------------------------------------------
#  TERMUX (ANDROID) AUTOSTART
#
#  An unprivileged Android app gets no service manager, so there is no systemd
#  unit, no launchd agent and no init script to install. Two Termux mechanisms
#  replace all three, and configure_termux_node sets up both:
#
#    1. runit, from the termux-services package: $PREFIX/var/service/<name>/run is
#       supervised inside the running Termux session, so a node that dies is
#       restarted - the systemd Restart=always equivalent;
#    2. ~/.termux/boot/<name>.sh, executed by the Termux:Boot app (F-Droid) after
#       the device boots: it takes the wake lock and starts the supervisor.
#
#  Without termux-services the node still runs and still autostarts at boot, but
#  nothing restarts it when Android kills the process. The installer says that
#  plainly instead of implying supervision that is not there.
#
#  configure_termux_node <service name> <python module> <extra args> <env file>
# ------------------------------------------------------------------------------
configure_termux_node() {
    local name="$1" module="$2" extra="$3" env_file="$4"
    local run_script="$CONFIG_DIR/${name}.sh"
    local boot_script="$HOME/.termux/boot/${name}.sh"
    local svdir="$TERMUX_PREFIX/var/service"
    local svlogdir="$TERMUX_PREFIX/var/log"
    local log_file="$CONFIG_DIR/${name}.log"
    local env_block=""
    local module_args="$module"
    local alive=false
    local waited=0

    [ -n "$extra" ] && module_args="$module $extra"

    TERMUX_SUPERVISED=false
    mkdir -p "$CONFIG_DIR" "$HOME/.termux/boot"

    if [ -n "$env_file" ]; then
        env_block="if [ -f \"$env_file\" ]; then
    set -a
    . \"$env_file\"
    set +a
fi"
    fi

    # The launcher is the one thing every mechanism below starts. Absolute paths
    # only: runit and the boot script run it with a minimal environment in which
    # $PATH may not even contain $PREFIX/bin.
    cat << RUNNER_EOF > "$run_script"
#!$TERMUX_PREFIX/bin/sh
# Remote Harness MCP - $name launcher (written by install.sh; do not edit).
# Run this file to start the node by hand; docs/TERMUX.md documents the automatic
# paths (runit service, Termux:Boot) and where the logs are.
export PATH="$TERMUX_PREFIX/bin:/system/bin:\$PATH"
$env_block
cd "$SCRIPT_DIR" || exit 1
exec "$PYTHON_BIN" -m $module_args
RUNNER_EOF
    chmod 700 "$run_script"

    # 1. runit supervision, when termux-services is installed. install.sh asks the
    #    package manager for it first; a phone that refused (no network, an old
    #    mirror) simply keeps the unattended launcher below.
    if command -v sv >/dev/null 2>&1 && command -v runsvdir >/dev/null 2>&1; then
        mkdir -p "$svdir/$name/log"
        cat << SV_EOF > "$svdir/$name/run"
#!$TERMUX_PREFIX/bin/sh
exec "$run_script"
SV_EOF
        chmod 755 "$svdir/$name/run"
        # runit refuses to supervise without log/run, and svlogger is a script, not
        # a program: the documented setup is a symlink to it. Two cases need the
        # equivalent script instead, because a link that cannot resolve is worse
        # than no link at all: a $PREFIX that refuses symlinks (a FUSE-backed home,
        # an unusual mount), and a phone where sv and runsvdir exist but
        # termux-services never installed its svlogger - 'ln -sf' happily creates a
        # dangling log/run against a missing target, and runit then has a logger it
        # cannot execute.
        local svlogger="$TERMUX_PREFIX/share/termux-services/svlogger"
        if [ ! -e "$svlogger" ] \
            || ! ln -sf "$svlogger" "$svdir/$name/log/run" 2>/dev/null; then
            printf '#!%s/bin/sh\nexec "%s"\n' \
                "$TERMUX_PREFIX" "$svlogger" > "$svdir/$name/log/run"
            chmod 755 "$svdir/$name/log/run"
        fi
        export SVDIR="$svdir"
        export LOGDIR="$svlogdir"
        # The supervisor is normally started by the login shell (profile.d), which
        # has not necessarily run when this installer is piped straight into bash.
        # Starting it here is what makes the node run *now*; the pgrep guard keeps a
        # second runsvdir off an SVDIR that already has one.
        if ! pgrep -f "runsvdir" >/dev/null 2>&1; then
            command -v service-daemon >/dev/null 2>&1 && service-daemon start >/dev/null 2>&1 || true
        fi
        sv up "$name" >/dev/null 2>&1 || true
        sv-enable "$name" >/dev/null 2>&1 || true
        TERMUX_SUPERVISED=true
    fi

    # 2. The boot entry, written even when runit supervises: after a reboot the
    #    supervisor itself has to be started by something, and on Android that
    #    something is the Termux:Boot app.
    cat << BOOT_EOF > "$boot_script"
#!$TERMUX_PREFIX/bin/sh
# Remote Harness MCP - starts the $name node after the device boots (Termux:Boot).
#
# Requirements, both of them outside this file (docs/TERMUX.md):
#   * the Termux:Boot app (F-Droid), opened once so Android lets it run;
#   * battery optimisation OFF for Termux and for Termux:Boot.
export PATH="$TERMUX_PREFIX/bin:/system/bin:\$PATH"
export HOME="$HOME"
export SVDIR="$svdir"
export LOGDIR="$svlogdir"

# A partial wake lock keeps Android from freezing the node while the screen is
# off. It needs the termux-api package plus the Termux:API app; without either,
# this line is a no-op rather than a failure.
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock >/dev/null 2>&1

if command -v sv >/dev/null 2>&1 && [ -d "$svdir/$name" ]; then
    if ! pgrep -f "runsvdir" >/dev/null 2>&1; then
        command -v service-daemon >/dev/null 2>&1 && service-daemon start >/dev/null 2>&1
    fi
    sv up "$name" >/dev/null 2>&1
else
    # No supervisor on this phone: start the launcher directly and keep its output.
    "$run_script" >> "$CONFIG_DIR/$name.boot.log" 2>&1 &
fi
BOOT_EOF
    chmod 700 "$boot_script"

    if [ "$TERMUX_SUPERVISED" != true ]; then
        # The systemd branch's fallback, spelled for a phone: nothing supervises
        # the process, so it is started detached and its log is the only evidence.
        pkill -f "$module" 2>/dev/null || true
        nohup "$run_script" >> "$log_file" 2>&1 &
    fi

    # Termux has no `systemctl status` to consult, so the installer waits for the
    # process it just started (runit may take a moment) and reports what it saw.
    while [ "$waited" -lt 10 ]; do
        if pgrep -f "$module" >/dev/null 2>&1; then
            alive=true
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if [ "$LANG_CHOICE" = "ru" ]; then
        if [ "$TERMUX_SUPERVISED" = true ]; then
            echo -e "${GREEN}[OK] runit (termux-services) следит за узлом и перезапустит его сам.${RESET}"
            echo -e "     Статус: ${CYAN}sv status $name${RESET}    Логи: ${CYAN}$svlogdir/sv/$name/current${RESET}"
        else
            echo -e "${YELLOW}[!] Узел запущен без супервизора: termux-services не установлен.${RESET}"
            echo -e "     Автоперезапуск после сбоя: ${CYAN}pkg install termux-services${RESET}, затем повторите установку."
            echo -e "     Логи: ${CYAN}$log_file${RESET}"
        fi
        echo -e "     Автозапуск после перезагрузки: ${CYAN}$boot_script${RESET} (нужно приложение Termux:Boot)."
        if [ "$alive" != true ]; then
            echo -e "${YELLOW}[!] Процесс узла пока не виден — смотрите лог: ${CYAN}$log_file${RESET}"
        fi
    else
        if [ "$TERMUX_SUPERVISED" = true ]; then
            echo -e "${GREEN}[OK] runit (termux-services) supervises the node and restarts it.${RESET}"
            echo -e "     Status: ${CYAN}sv status $name${RESET}    Logs: ${CYAN}$svlogdir/sv/$name/current${RESET}"
        else
            echo -e "${YELLOW}[!] The node runs unsupervised: termux-services is not installed.${RESET}"
            echo -e "     Restart after a crash: ${CYAN}pkg install termux-services${RESET}, then re-run the installer."
            echo -e "     Logs: ${CYAN}$log_file${RESET}"
        fi
        echo -e "     Autostart after a reboot: ${CYAN}$boot_script${RESET} (the Termux:Boot app is required)."
        if [ "$alive" != true ]; then
            echo -e "${YELLOW}[!] The node process is not visible yet - check: ${CYAN}$log_file${RESET}"
        fi
    fi
}

# The Android-side switches no installer can flip for the user: whether the node
# comes back after a reboot and whether Android keeps it alive with the screen
# off. Printed after the MCP URL, in the language of the run.
print_termux_hints() {
    local name="$1"
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "  ${BOLD}📱 Termux (Android): что проверить на телефоне${RESET} (служба: ${CYAN}$name${RESET})"
        echo -e "  1. Автозапуск после перезагрузки делает приложение ${CYAN}Termux:Boot${RESET} (F-Droid)."
        echo -e "     Установите и ${BOLD}запустите его один раз${RESET}, иначе скрипт в ~/.termux/boot не выполнится."
        echo -e "  2. Отключите оптимизацию батареи для ${CYAN}Termux${RESET} и Termux:Boot: иначе Android"
        echo -e "     выгружает фоновый процесс вместе с туннелем."
        echo -e "  3. Скопировать ссылку выше: долгое нажатие в терминале, либо приложение Termux:API и"
        echo -e "     ${CYAN}termux-clipboard-set '<ссылка>'${RESET}."
        echo -e "  4. Выдайте приложению ${CYAN}Termux:API${RESET} разрешения для инструментов устройства"
        echo -e "     (Настройки Android → Приложения → Termux:API → Разрешения): камера, микрофон,"
        echo -e "     местоположение, SMS, журнал вызовов, контакты. Без них ${CYAN}device_capture${RESET} и"
        echo -e "     ${CYAN}device_messages${RESET} ответят, какого именно разрешения не хватает."
        echo -e "  5. Ветка устройства включена по умолчанию (${CYAN}MESH_DEVICE=auto${RESET}). SMS, контакты и"
        echo -e "     журнал вызовов открывает ${CYAN}MESH_DEVICE_PIM=1${RESET}; ${CYAN}MESH_DEVICE_QUICK=1${RESET} оставляет"
        echo -e "     system_info мгновенным (без сети, камер, микрофонов и датчиков)."
        echo -e "  6. Полная инструкция: ${CYAN}docs/TERMUX.md${RESET} (установка, автозапуск, логи, удаление)."
        echo ""
    else
        echo -e "  ${BOLD}📱 Termux (Android): what to check on the phone${RESET} (service: ${CYAN}$name${RESET})"
        echo -e "  1. Autostart after a reboot is done by the ${CYAN}Termux:Boot${RESET} app (F-Droid)."
        echo -e "     Install it and ${BOLD}open it once${RESET}, or the script in ~/.termux/boot never runs."
        echo -e "  2. Turn battery optimisation OFF for ${CYAN}Termux${RESET} and Termux:Boot: otherwise Android"
        echo -e "     unloads the background process together with the tunnel."
        echo -e "  3. Copy the URL above: long-press the terminal, or install the Termux:API app and use"
        echo -e "     ${CYAN}termux-clipboard-set '<url>'${RESET}."
        echo -e "  4. Grant the ${CYAN}Termux:API${RESET} app the permissions the device tools need"
        echo -e "     (Android Settings → Apps → Termux:API → Permissions): camera, microphone, location,"
        echo -e "     SMS, call log, contacts. Without them ${CYAN}device_capture${RESET} / ${CYAN}device_messages${RESET}"
        echo -e "     answer with the exact permission that is missing."
        echo -e "  5. The device branch is on by default (${CYAN}MESH_DEVICE=auto${RESET}). ${CYAN}MESH_DEVICE_PIM=1${RESET}"
        echo -e "     enables SMS, contacts and the call log; ${CYAN}MESH_DEVICE_QUICK=1${RESET} keeps system_info"
        echo -e "     instant (no network, cameras, microphones or sensors)."
        echo -e "  6. Full guide: ${CYAN}docs/TERMUX.md${RESET} (install, autostart, logs, uninstall)."
        echo ""
    fi
}

# ------------------------------------------------------------------------------
#  Retire the pre-rename autostart entries.
#
#  This project was published as "Antigravity Mesh" and its supervisors were named
#  agy-* (agy-agent.service, com.antigravity.mesh.agent, the runit service
#  agy-agent, ~/.termux/boot/agy-agent.sh). Installing under the new names must not
#  leave the old supervisor behind: two agents would share one tunnel, one config
#  and one token, and would fight over them. Each legacy entry is stopped and
#  removed, but only when it actually exists - a fresh machine has none of them,
#  and no system-wide unit is ever touched.
# ------------------------------------------------------------------------------
retire_legacy_autostart() {
    local legacy_units="agy-agent.service agy-standalone.service agy-gateway.service agy-watcher.service agy-mcp.service agy-webhook.service"
    local unit unit_file
    local home="${HOME:-}"
    for unit in $legacy_units; do
        unit_file="$home/.config/systemd/user/$unit"
        if [ -f "$unit_file" ]; then
            if command -v systemctl >/dev/null 2>&1; then
                systemctl --user disable --now "$unit" >/dev/null 2>&1 || true
                systemctl --user daemon-reload >/dev/null 2>&1 || true
            fi
            rm -f "$unit_file"
            echo "  Retired the pre-rename service $unit."
        fi
    done

    if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
        local label plist
        for label in com.antigravity.mesh.agent com.antigravity.mesh.standalone; do
            plist="$home/Library/LaunchAgents/$label.plist"
            if [ -f "$plist" ]; then
                command -v launchctl >/dev/null 2>&1 && \
                    launchctl unload -w "$plist" >/dev/null 2>&1 || true
                rm -f "$plist"
                echo "  Retired the pre-rename launch agent $label."
            fi
        done
    fi

    if [ "$IS_TERMUX" = true ] && [ -n "$TERMUX_PREFIX" ]; then
        local name svdir boot_script
        for name in agy-agent agy-standalone; do
            svdir="$TERMUX_PREFIX/var/service/$name"
            boot_script="$home/.termux/boot/$name.sh"
            if [ -d "$svdir" ] || [ -f "$boot_script" ]; then
                if command -v sv >/dev/null 2>&1; then
                    sv down "$name" >/dev/null 2>&1 || true
                fi
                rm -rf "$svdir"
                rm -f "$boot_script" "$CONFIG_DIR/$name.sh"
                echo "  Retired the pre-rename Termux service $name."
            fi
        done
    fi
}

# Dry-run
if [ "$DRY_RUN" = true ]; then
    echo "[DRY-RUN] Simulating Remote Harness MCP installation..."
    echo "[DRY-RUN] Language: $LANG_CHOICE"
    echo "[DRY-RUN] Detected Host: $DETECTED_HOSTNAME ($DETECTED_TYPE)"
    echo "[DRY-RUN] Architecture: ${ARCH_LABEL:-$DETECTED_ARCH}"
    echo "[DRY-RUN] Selected Mode: ${MODE:-standalone}"
    echo "[DRY-RUN] Port: $PORT"
    echo "[DRY-RUN] TLS: $TLS"
    if [ -n "$GATEWAY" ]; then echo "[DRY-RUN] Domain: $GATEWAY"; fi
    if [ -n "$USERNAME" ]; then
        echo "[DRY-RUN] User: $USERNAME"
        if [ -n "$GATEWAY" ]; then
            echo "[DRY-RUN] Canonical MCP URL: $(node_sse_url "$USERNAME" "${TOKEN:-<token>}")"
        fi
    fi
    if [ -n "$TOKEN" ]; then echo "[DRY-RUN] Token: set (standalone: stored in ${CONFIG_DIR}/standalone.env, chmod 600)"; fi
    if [ "$IS_TERMUX" = true ]; then
        echo "[DRY-RUN] Platform: Termux (Android)"
        echo "[DRY-RUN] Termux prefix: $TERMUX_PREFIX"
        echo "[DRY-RUN] Package manager: pkg (sudo, systemd and uv do not exist on Android)"
        echo "[DRY-RUN] Autostart: runit service <name> + ~/.termux/boot/<name>.sh (Termux:Boot)"
        echo "[DRY-RUN] Domain file: $DEFAULT_DOMAIN_FILE"
    else
        echo "[DRY-RUN] Domain file: $DEFAULT_DOMAIN_FILE"
        echo "[DRY-RUN] Systemd service and dependencies check: OK"
    fi
    if [ -z "$GATEWAY" ]; then
        echo "[DRY-RUN] Domain file value: not written (no domain configured)"
    elif [ "$GATEWAY" = "$(normalise_host "$(module_default_domain)")" ]; then
        echo "[DRY-RUN] Domain file value: not written (the built-in default is a fallback, not a configuration)"
    else
        echo "[DRY-RUN] Domain file value: MESH_PUBLIC_URL=https://$GATEWAY"
    fi
    exit 0
fi

# SSH Remote install branch
if [ -n "$SSH_TARGET" ]; then
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "${BOLD}${BLUE}=== Удаленная установка Remote Harness MCP через SSH ===${RESET}"
        echo -e "Целевой сервер: ${CYAN}${SSH_TARGET}${RESET} (порт: ${SSH_PORT})"
    else
        echo -e "${BOLD}${BLUE}=== Remote Remote Harness MCP SSH Installation ===${RESET}"
        echo -e "Target Server: ${CYAN}${SSH_TARGET}${RESET} (port: ${SSH_PORT})"
    fi

    SSH_BIN="ssh -o StrictHostKeyChecking=no"
    SCP_BIN="scp -o StrictHostKeyChecking=no"
    if [ -n "$SSHPASS" ] && command -v sshpass >/dev/null 2>&1; then
        SSH_BIN="sshpass -e $SSH_BIN"
        SCP_BIN="sshpass -e $SCP_BIN"
    fi

    $SSH_BIN -p "$SSH_PORT" "$SSH_TARGET" "mkdir -p ~/remote-harness-mcp/core ~/remote-harness-mcp/skills"
    $SCP_BIN -P "$SSH_PORT" -r "$SCRIPT_DIR/core"/* "$SSH_TARGET:~/remote-harness-mcp/core/"
    $SCP_BIN -P "$SSH_PORT" "$SCRIPT_DIR/install.sh" "$SSH_TARGET:~/remote-harness-mcp/"
    if [ -d "$SCRIPT_DIR/skills" ]; then
        $SCP_BIN -P "$SSH_PORT" -r "$SCRIPT_DIR/skills"/* "$SSH_TARGET:~/remote-harness-mcp/skills/" 2>/dev/null || true
    fi
    # The domain was resolved on THIS machine, so it is handed to the target: the
    # remote copy otherwise re-resolves from its own environment, and a repository
    # copy whose $PUBLISHED_DOMAIN is still the placeholder falls back to the
    # built-in default - which the target would now also record as its domain.
    REMOTE_DOMAIN_ARG=""
    if [ -n "$GATEWAY" ] && [ "$GATEWAY" != "$DOMAIN_PLACEHOLDER" ]; then
        REMOTE_DOMAIN_ARG=" --domain=$GATEWAY"
    fi
    $SSH_BIN -p "$SSH_PORT" "$SSH_TARGET" "cd ~/remote-harness-mcp && bash install.sh --quick --lang=$LANG_CHOICE$REMOTE_DOMAIN_ARG"
    exit 0
fi

# Interactive Menu if no mode is specified and not in quick mode
if [ "$QUICK" = false ] && [ -z "$MODE" ] && [ -t 0 ]; then
    print_device_info
    echo ""
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "${BOLD}Выберите режим установки:${RESET}"
        echo -e "  ${GREEN}1)${RESET} ⚡ ${BOLD}Быстрая настройка${RESET} (Имя узла как имя ПК + Общий домен + Автозапуск) [Рекомендуется]"
        echo -e "  ${BLUE}2)${RESET} 🖥️  ${BOLD}Локальный Standalone${RESET} (Только localhost:${PORT}, без облачного шлюза)"
        echo -e "  ${YELLOW}3)${RESET} ⚙️  ${BOLD}Кастомная настройка${RESET} (Ввести имя узла вручную, выбор общего домена)"
        echo -e "  ${CYAN}4)${RESET} 📡 ${BOLD}Удаленная установка на SSH-сервер${RESET}"
        echo -e "  ${RESET}0) Выход"
        echo ""
        read -rp "Ваш выбор [1]: " MENU_CHOICE
    else
        echo -e "${BOLD}Select Installation Mode:${RESET}"
        echo -e "  ${GREEN}1)${RESET} ⚡ ${BOLD}Quick Setup${RESET} (Node name from PC name + shared domain + Autostart) [Recommended]"
        echo -e "  ${BLUE}2)${RESET} 🖥️  ${BOLD}Local Standalone${RESET} (localhost:${PORT} only, no cloud gateway)"
        echo -e "  ${YELLOW}3)${RESET} ⚙️  ${BOLD}Custom Setup${RESET} (Custom node name, custom shared domain)"
        echo -e "  ${CYAN}4)${RESET} 📡 ${BOLD}Remote SSH Installation${RESET}"
        echo -e "  ${RESET}0) Exit"
        echo ""
        read -rp "Select [1]: " MENU_CHOICE
    fi
    MENU_CHOICE=${MENU_CHOICE:-1}

    case "$MENU_CHOICE" in
        1)
            MODE="tunnel"
            QUICK=true
            ;;
        2)
            MODE="standalone"
            ;;
        3)
            MODE="tunnel"
            echo ""
            if [ "$LANG_CHOICE" = "ru" ]; then
                read -rp "Введите имя узла [по умолчанию: ${DETECTED_HOSTNAME}]: " CUSTOM_SUB
                read -rp "Общий домен шлюза [по умолчанию: ${GATEWAY}]: " CUSTOM_GW
            else
                read -rp "Enter node name [default: ${DETECTED_HOSTNAME}]: " CUSTOM_SUB
                read -rp "Shared gateway domain [default: ${GATEWAY}]: " CUSTOM_GW
            fi
            if [ -n "$CUSTOM_SUB" ]; then USERNAME="$CUSTOM_SUB"; fi
            if [ -n "$CUSTOM_GW" ]; then GATEWAY="$CUSTOM_GW"; fi
            ;;
        4)
            echo ""
            if [ "$LANG_CHOICE" = "ru" ]; then
                read -rp "Введите SSH цель (например, user@<host-ip>): " REMOTE_TARGET
                read -rp "Порт SSH [22]: " REMOTE_PORT
            else
                read -rp "Enter SSH target (e.g., user@<host-ip>): " REMOTE_TARGET
                read -rp "SSH Port [22]: " REMOTE_PORT
            fi
            REMOTE_PORT=${REMOTE_PORT:-22}
            exec "$0" "--ssh=${REMOTE_TARGET}:${REMOTE_PORT}" "--lang=${LANG_CHOICE}"
            ;;
        0)
            echo "Cancelled."
            exit 0
            ;;
        *)
            MODE="tunnel"
            QUICK=true
            ;;
    esac
fi

if [ -z "$MODE" ]; then
    MODE="tunnel"
fi

print_device_info

# Before any new unit is created: stop and remove the pre-rename autostart entries
# (see retire_legacy_autostart above).
retire_legacy_autostart

# ==============================================================================
#  BRANCH: STANDALONE MODE
# ==============================================================================
if [ "$MODE" = "standalone" ]; then
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "\n${BOLD}${BLUE}=== Настройка локального Standalone FastMCP сервера ===${RESET}"
        echo "[1/3] Проверка окружения Python..."
    else
        echo -e "\n${BOLD}${BLUE}=== Setting up Local Standalone FastMCP Server ===${RESET}"
        echo "[1/3] Checking Python environment..."
    fi
    run_busy "Python и зависимости / Python and dependencies" ensure_dependencies
    # The endpoint check at the end of this branch runs through curl.
    ensure_curl || exit 1
    mkdir -p "$CONFIG_DIR"
    # A standalone node answers share_file too, so the domain it resolved is written
    # down here as well - before the server is started, not after.
    write_domain_file "$GATEWAY"

    # Honour --token in standalone mode without embedding the secret in the
    # (world-readable) unit/plist: keep it in a 0600 env file for systemd
    # (EnvironmentFile) and use a 0600 plist environment block on macOS.
    # core.server reads MESH_TOKEN as the default for --token.
    STANDALONE_ENV_FILE="$CONFIG_DIR/standalone.env"
    ENV_FILE_LINE=""
    PLIST_ENV_BLOCK=""
    if [ -n "$TOKEN" ]; then
        printf 'MESH_TOKEN=%s\n' "$TOKEN" > "$STANDALONE_ENV_FILE"
        chmod 600 "$STANDALONE_ENV_FILE"
        ENV_FILE_LINE="EnvironmentFile=$STANDALONE_ENV_FILE"
        PLIST_TOKEN=$(printf '%s' "$TOKEN" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')
        PLIST_ENV_BLOCK="    <key>EnvironmentVariables</key>
    <dict>
        <key>MESH_TOKEN</key>
        <string>${PLIST_TOKEN}</string>
    </dict>"
        if [ "$LANG_CHOICE" = "ru" ]; then
            echo -e "${GREEN}[✓] Токен авторизации включён (chmod 600).${RESET}"
        else
            echo -e "${GREEN}[OK] Authorization token enabled (chmod 600).${RESET}"
        fi
    else
        rm -f "$STANDALONE_ENV_FILE" 2>/dev/null || true
        if [ "$LANG_CHOICE" = "ru" ]; then
            echo -e "${YELLOW}[!] Токен не задан (--token): сервер доступен без авторизации на 127.0.0.1.${RESET}"
        else
            echo -e "${YELLOW}[!] No --token given: server is unauthenticated on 127.0.0.1.${RESET}"
        fi
    fi

    if [ "$LANG_CHOICE" = "ru" ]; then
        if [ "$IS_TERMUX" = true ]; then
            echo "[2/3] Настройка автозапуска на телефоне (runit + Termux:Boot)..."
        else
            echo "[2/3] Настройка systemd автозапуска на ПК..."
        fi
    else
        echo "[2/3] Configuring autostart background service..."
    fi

    if [ "$(uname -s)" = "Darwin" ]; then
        LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
        mkdir -p "$LAUNCH_AGENTS"
        PLIST_FILE="$LAUNCH_AGENTS/com.remoteharness.mcp.standalone.plist"
        cat << EOF > "$PLIST_FILE"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.remoteharness.mcp.standalone</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>-m</string>
        <string>core.server</string>
        <string>--host=127.0.0.1</string>
        <string>--port=$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
$PLIST_ENV_BLOCK
</dict>
</plist>
EOF
        if [ -n "$TOKEN" ]; then
            chmod 600 "$PLIST_FILE"
        fi
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
        launchctl load -w "$PLIST_FILE" 2>/dev/null || true
    elif [ "$IS_TERMUX" = true ]; then
        # Android: neither systemd nor launchd exists - runit inside Termux
        # supervises the server, and Termux:Boot brings it back after a reboot.
        configure_termux_node "rhm-standalone" "core.server" "--host 127.0.0.1 --port=$PORT" "$STANDALONE_ENV_FILE"
    else
        USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
        mkdir -p "$USER_SYSTEMD_DIR"
        SERVICE_FILE="$USER_SYSTEMD_DIR/rhm-standalone.service"
        cat << EOF > "$SERVICE_FILE"
[Unit]
Description=Remote Harness MCP Local Standalone MCP Server
After=network.target

[Service]
Type=simple
$ENV_FILE_LINE
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON_BIN -m core.server --host 127.0.0.1 --port=$PORT
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload 2>/dev/null || true
        systemctl --user enable --now rhm-standalone.service 2>/dev/null || {
            pkill -f "core.server" 2>/dev/null || true
            if [ -n "$TOKEN" ]; then
                nohup env MESH_TOKEN="$TOKEN" $PYTHON_BIN -m core.server --host 127.0.0.1 --port="$PORT" > "$CONFIG_DIR/standalone.log" 2>&1 &
            else
                nohup $PYTHON_BIN -m core.server --host 127.0.0.1 --port="$PORT" > "$CONFIG_DIR/standalone.log" 2>&1 &
            fi
        }
        loginctl enable-linger "$USER" 2>/dev/null || true
    fi

    if [ "$LANG_CHOICE" = "ru" ]; then
        echo "[3/3] Проверка локального эндпоинта..."
    else
        echo "[3/3] Verifying local endpoint..."
    fi
    HEALTH_URL="http://127.0.0.1:${PORT}/health"
    HEALTH_OK=false
    for _attempt in 1 2 3 4 5 6 7 8 9 10; do
        HTTP_CODE=$(curl -s -o /dev/null -m 2 -w "%{http_code}" "$HEALTH_URL" 2>/dev/null || true)
        if [ "$HTTP_CODE" = "200" ]; then
            HEALTH_OK=true
            break
        fi
        sleep 1
    done
    if [ "$HEALTH_OK" != true ]; then
        if [ "$LANG_CHOICE" = "ru" ]; then
            echo -e "\033[1;31m[ОШИБКА] Standalone MCP-сервер не отвечает (HTTP ${HTTP_CODE:-нет ответа}): $HEALTH_URL\033[0m" >&2
            if [ "$IS_TERMUX" = true ]; then
                echo -e "  Статус: ${CYAN}sv status rhm-standalone${RESET} (runit, termux-services)" >&2
                echo -e "  Логи:   ${CYAN}$TERMUX_PREFIX/var/log/sv/rhm-standalone/current${RESET}" >&2
                echo -e "  Локальный сервер на телефоне доступен только внутри Termux (127.0.0.1):" >&2
                echo -e "  для Gemini с телефона используйте режим туннеля (--mode=tunnel)." >&2
            else
                echo -e "  Проверьте статус: ${CYAN}systemctl --user status rhm-standalone.service${RESET}" >&2
                echo -e "  Логи:             ${CYAN}journalctl --user -u rhm-standalone.service -n 50 --no-pager${RESET}" >&2
            fi
        else
            echo -e "\033[1;31m[ERROR] Standalone MCP server is not responding (HTTP ${HTTP_CODE:-no response}): $HEALTH_URL\033[0m" >&2
            if [ "$IS_TERMUX" = true ]; then
                echo -e "  Status: ${CYAN}sv status rhm-standalone${RESET} (runit, termux-services)" >&2
                echo -e "  Logs:   ${CYAN}$TERMUX_PREFIX/var/log/sv/rhm-standalone/current${RESET}" >&2
                echo -e "  A local server on a phone is reachable inside Termux only (127.0.0.1):" >&2
                echo -e "  use --mode=tunnel to drive this phone from Gemini." >&2
            else
                echo -e "  Check status: ${CYAN}systemctl --user status rhm-standalone.service${RESET}" >&2
                echo -e "  Logs:         ${CYAN}journalctl --user -u rhm-standalone.service -n 50 --no-pager${RESET}" >&2
            fi
        fi
        exit 1
    fi

    MCP_LOCAL_URL="http://localhost:${PORT}/sse"
    
    # Quick Copy to Clipboard
    COPIED=false
    B64_URL=$(printf "%s" "$MCP_LOCAL_URL" | base64 | tr -d '\r\n')
    printf "\033]52;c;%s\a" "$B64_URL" 2>/dev/null || true
    if command -v termux-clipboard-set >/dev/null 2>&1; then
        # Termux on a phone: termux-api plus the Termux:API app put the URL on
        # Android's clipboard, where the Gemini app can paste it. Without the app
        # the command exists but fails, so COPIED stays false and the hint below
        # tells the user to copy by hand.
        printf "%s" "$MCP_LOCAL_URL" | termux-clipboard-set 2>/dev/null && COPIED=true
    elif command -v wl-copy >/dev/null 2>&1; then
        printf "%s" "$MCP_LOCAL_URL" | wl-copy 2>/dev/null && COPIED=true
    elif command -v xclip >/dev/null 2>&1; then
        printf "%s" "$MCP_LOCAL_URL" | xclip -selection clipboard 2>/dev/null && COPIED=true
    elif command -v xsel >/dev/null 2>&1; then
        printf "%s" "$MCP_LOCAL_URL" | xsel --clipboard --input 2>/dev/null && COPIED=true
    elif command -v pbcopy >/dev/null 2>&1; then
        printf "%s" "$MCP_LOCAL_URL" | pbcopy 2>/dev/null && COPIED=true
    elif command -v clip.exe >/dev/null 2>&1; then
        printf "%s" "$MCP_LOCAL_URL" | clip.exe 2>/dev/null && COPIED=true
    fi

    [ "$PAIR" = true ] || { clear 2>/dev/null || true; }

    if [ "$LANG_CHOICE" = "ru" ]; then
        echo ""
        copy_msg=""
        if [ "$COPIED" = true ]; then
            copy_msg="[OK] ССЫЛКА СКОПИРОВАНА В БУФЕР ОБМЕНА! (Вставьте через Ctrl+V)"
        else
            copy_msg="Скопируйте ссылку выше"
        fi
        print_mcp_box "ССЫЛКА ЛОКАЛЬНОГО MCP-СЕРВЕРА (СКОПИРУЙТЕ):" "$MCP_LOCAL_URL" "$copy_msg"
        echo ""
        echo -e "  ${BOLD}Куда вставлять ссылку в Google Gemini:${RESET}"
        echo -e "  1. Откройте в браузере: ${CYAN}https://gemini.google.com/customize/connectors${RESET}"
        echo -e "  2. Нажмите ${BOLD}'Добавить коннектор'${RESET} (Add connector) и задайте имя ${BOLD}'MyDevice'${RESET}"
        echo -e "  3. Вставьте скопированную ссылку в поле ${BOLD}'URL сервера'${RESET} (${BOLD}Ctrl+V${RESET}) и нажмите ${BOLD}Подключить${RESET}."
        echo ""
    else
        echo ""
        copy_msg=""
        if [ "$COPIED" = true ]; then
            copy_msg="[OK] URL COPIED TO CLIPBOARD! (Press Ctrl+V to paste)"
        else
            copy_msg="Copy the URL above"
        fi
        print_mcp_box "LOCAL MCP SERVER URL (COPY THIS):" "$MCP_LOCAL_URL" "$copy_msg"
        echo ""
        echo -e "  ${BOLD}Where to paste this URL in Google Gemini:${RESET}"
        echo -e "  1. Open in your browser: ${CYAN}https://gemini.google.com/customize/connectors${RESET}"
        echo -e "  2. Click ${BOLD}'Add connector'${RESET} and name it ${BOLD}'MyDevice'${RESET}"
        echo -e "  3. Paste the URL into the ${BOLD}'Server URL'${RESET} field (${BOLD}Ctrl+V${RESET}) and click ${BOLD}Connect${RESET}."
        echo ""
    fi
    if [ "$IS_TERMUX" = true ]; then
        print_termux_hints "rhm-standalone"
    fi
    exit 0
fi

# ==============================================================================
#  BRANCH: CLOUD GATEWAY + REVERSE TUNNEL
# ==============================================================================
if [ "$LANG_CHOICE" = "ru" ]; then
    echo -e "\n${BOLD}${MAGENTA}=== Настройка облачного туннеля (общий домен) ===${RESET}"
else
    echo -e "\n${BOLD}${MAGENTA}=== Configuring Cloud Gateway Tunnel (shared domain) ===${RESET}"
fi

if [ -z "$USERNAME" ]; then
    CLEAN_HOST=$(echo "$DETECTED_HOSTNAME" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')
    USERNAME="$CLEAN_HOST"
fi
USERNAME=$(echo "$USERNAME" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')
[ -z "$USERNAME" ] && USERNAME="mesh-node"

mkdir -p "$CONFIG_DIR"

if [ "$LANG_CHOICE" = "ru" ]; then
    echo "[1/4] Проверка Python зависимостей (websockets)..."
else
    echo "[1/4] Checking Python dependencies (websockets)..."
fi

run_busy "Python и зависимости / Python and dependencies" ensure_dependencies
# Registration - the step right after this - is a curl POST. A machine with a usable
# Python and no curl (a minimal WSL image, a slim container) used to die there with
# "curl: command not found" and no hint about what to install.
run_busy "Проверка загрузчика / Checking downloader" ensure_curl || exit 1
# Registration is also the first step that truly needs the gateway: a name that does
# not resolve here would be reported a moment later as "no authentication token",
# which blames the gateway's answer instead of this device's DNS.
run_busy "Проверка адреса Gateway / Checking Gateway DNS" require_gateway_dns || exit 1

if [ "$LANG_CHOICE" = "ru" ]; then
    echo "[2/4] Регистрация узла '${USERNAME}' на общем домене (${GATEWAY})..."
else
    echo "[2/4] Registering node '${USERNAME}' on the shared domain (${GATEWAY})..."
fi

EXISTING_TOKEN=""
if [ -f "$CONFIG_FILE" ]; then
    CFG_USER=$(grep "MESH_USER" "$CONFIG_FILE" 2>/dev/null | cut -d '=' -f2 || true)
    CFG_TOKEN=$(grep "MESH_TOKEN" "$CONFIG_FILE" 2>/dev/null | cut -d '=' -f2 || true)
    if [ "$CFG_USER" = "$USERNAME" ] && [ -n "$CFG_TOKEN" ]; then
        EXISTING_TOKEN="$CFG_TOKEN"
    fi
fi

if [ -n "$TOKEN" ]; then
    REQ_TOKEN="$TOKEN"
elif [ -n "$EXISTING_TOKEN" ]; then
    REQ_TOKEN="$EXISTING_TOKEN"
else
    REQ_TOKEN=""
fi

DETECTED_MAC=""
# "$PYTHON_BIN", never a bare python3: after the venv or uv fallback the working
# interpreter is not on PATH, and the bare name would silently produce no MAC.
if python_works "$PYTHON_BIN"; then
    DETECTED_MAC=$("$PYTHON_BIN" -c "import uuid; print(':'.join(['{:02x}'.format((uuid.getnode() >> ele) & 0xff) for ele in range(0,8*6,8)][::-1]))" 2>/dev/null || true)
fi
if [ -z "$DETECTED_MAC" ] || [ "$DETECTED_MAC" = "00:00:00:00:00:00" ]; then
    for iface in /sys/class/net/*; do
        if [ -f "$iface/address" ] && [ "$(cat "$iface/type" 2>/dev/null)" = "1" ]; then
            addr=$(cat "$iface/address" 2>/dev/null)
            if [ -n "$addr" ] && [ "$addr" != "00:00:00:00:00:00" ]; then
                DETECTED_MAC="$addr"
                break
            fi
        fi
    done
fi

# One field out of the registration answer. The JSON arrives on stdin rather than
# in argv (a token or an error message can contain quotes), and "$PYTHON_BIN" is
# used because after the venv or uv fallback there is no python3 on PATH - a bare
# name would return an empty token, which the check below would then blame on the
# gateway.
json_field() {
    # tr strips the CR that a Windows python.exe appends under Git Bash/MSYS.
    # install.sh explicitly supports MINGW/MSYS/CYGWIN (see DEFAULT_DOMAIN_DIR), and
    # a trailing CR inside the token would be pasted straight into the wss:// URL.
    printf '%s' "$1" | "$PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$2" 2>/dev/null | tr -d '\r\n' || true
}

if [ "$PAIR" = true ]; then
    progress_stage "Google: вход и подтверждение устройства / Sign in and confirm device"
    (cd "$SCRIPT_DIR" && "$PYTHON_BIN" -u -m core.onboarding pair \
        --gateway "https://${GATEWAY}" --config-dir "$CONFIG_DIR" --name "$USERNAME") || exit 1
    REG_RESP=$(cd "$SCRIPT_DIR" && "$PYTHON_BIN" - "$CONFIG_FILE" <<'PAIR_JSON'
import json,sys
from core.domain import read_env_file
c=read_env_file(sys.argv[1])
print(json.dumps({"username":c["MESH_USER"],"token":c["MESH_TOKEN"],"link_key":c["MESH_LINK_KEY"]}))
PAIR_JSON
    )
    ASSIGNED_USER=$(json_field "$REG_RESP" username)
    ASSIGNED_TOKEN=$(json_field "$REG_RESP" token)
    ASSIGNED_LINK=$(json_field "$REG_RESP" link_key)
else
REG_BODY="{\"username\": \"${USERNAME}\", \"auto_suffix\": true, \"mac_address\": \"${DETECTED_MAC}\", \"os\": \"${DETECTED_OS}\", \"arch\": \"${DETECTED_ARCH}\""
if [ -n "$REQ_TOKEN" ]; then
    REG_BODY="${REG_BODY}, \"token\": \"${REQ_TOKEN}\""
fi
REG_BODY="${REG_BODY}}"

REG_RESP=$(curl -s -X POST "https://${GATEWAY}/api/register" \
    -H "Content-Type: application/json" \
    -d "$REG_BODY")

ASSIGNED_USER=$(json_field "$REG_RESP" username)
ASSIGNED_TOKEN=$(json_field "$REG_RESP" token)
# The URL a client gets carries the link key, not the device token: a URL is
# logged, kept in history and pasted into a third-party connector, while the
# token opens the tunnel. Older gateways return no link key, in which case the
# URL falls back to the token exactly as before.
ASSIGNED_LINK=$(json_field "$REG_RESP" link_key)

if [ -z "$ASSIGNED_TOKEN" ]; then
    FALLBACK_USER="${USERNAME}-$(date +%s | tail -c 4)"
    REG_RESP=$(curl -s -X POST "https://${GATEWAY}/api/register" \
        -H "Content-Type: application/json" \
        -d "{\"username\": \"${FALLBACK_USER}\", \"auto_suffix\": true, \"mac_address\": \"${DETECTED_MAC}\", \"os\": \"${DETECTED_OS}\", \"arch\": \"${DETECTED_ARCH}\"}")
    ASSIGNED_USER=$(json_field "$REG_RESP" username)
    ASSIGNED_TOKEN=$(json_field "$REG_RESP" token)
    ASSIGNED_LINK=$(json_field "$REG_RESP" link_key)
fi

if [ -z "$ASSIGNED_TOKEN" ]; then
    echo -e "${RED}[ERROR] Failed to obtain authentication token from gateway.${RESET}"
    exit 1
fi

fi

if [ "$ASSIGNED_USER" != "$USERNAME" ]; then
    if [ "$LANG_CHOICE" = "ru" ]; then
        echo -e "${YELLOW}ℹ️  Имя '${USERNAME}' уже было занято. Автоматически назначено имя узла:${RESET} ${BOLD}${GREEN}${ASSIGNED_USER}${RESET}"
    else
        echo -e "${YELLOW}ℹ️  Name '${USERNAME}' was already taken. Automatically assigned node name:${RESET} ${BOLD}${GREEN}${ASSIGNED_USER}${RESET}"
    fi
fi

if [ "$LANG_CHOICE" = "ru" ]; then
    echo "[3/4] Сохранение конфигурации в ${CONFIG_FILE}..."
else
    echo "[3/4] Saving configuration to ${CONFIG_FILE}..."
fi

if [ "$PAIR" != true ]; then
cat << EOF > "$CONFIG_FILE"
MESH_GATEWAY=${GATEWAY}
MESH_USER=${ASSIGNED_USER}
MESH_TOKEN=${ASSIGNED_TOKEN}
MESH_LINK_KEY=${ASSIGNED_LINK}
EOF
chmod 600 "$CONFIG_FILE"
fi

# agent.env carries only the legacy MESH_GATEWAY, which core/domain.py honours for
# the tunnel but never for the links a node publishes. Writing the domain down is
# what keeps share links on the gateway this node is registered on.
write_domain_file "$GATEWAY"

if [ "$LANG_CHOICE" = "ru" ]; then
    if [ "$IS_TERMUX" = true ]; then
        echo "[4/4] Настройка и запуск автозапуска на телефоне (runit + Termux:Boot)..."
    else
        echo "[4/4] Настройка и запуск службы автозапуска на ПК..."
    fi
else
    echo "[4/4] Configuring and starting background autostart service..."
fi

if [ "$(uname -s)" = "Darwin" ]; then
    LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
    mkdir -p "$LAUNCH_AGENTS"
    PLIST_FILE="$LAUNCH_AGENTS/com.remoteharness.mcp.agent.plist"
    cat << EOF > "$PLIST_FILE"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.remoteharness.mcp.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>-m</string>
        <string>core.agent</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MESH_GATEWAY</key>
        <string>$GATEWAY</string>
        <key>MESH_USER</key>
        <string>$ASSIGNED_USER</string>
        <key>MESH_TOKEN</key>
        <string>$ASSIGNED_TOKEN</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF
    launchctl unload "$PLIST_FILE" 2>/dev/null || true
    launchctl load -w "$PLIST_FILE" 2>/dev/null || true
elif [ "$IS_TERMUX" = true ]; then
    # Android: the tunnel node is supervised by runit inside Termux, and the
    # Termux:Boot entry brings it back after a reboot - see configure_termux_node.
    configure_termux_node "rhm-agent" "core.agent" "" "$CONFIG_FILE"
else
    USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$USER_SYSTEMD_DIR"
    cat << EOF > "$USER_SYSTEMD_DIR/rhm-agent.service"
[Unit]
Description=Remote Harness MCP Reverse RPC Agent
After=network.target

[Service]
Type=simple
EnvironmentFile=$CONFIG_FILE
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON_BIN -m core.agent
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable --now rhm-agent.service 2>/dev/null || {
        pkill -f "core.agent" 2>/dev/null || true
        nohup $PYTHON_BIN -m core.agent > "$CONFIG_DIR/agent.log" 2>&1 &
    }
    loginctl enable-linger "$USER" 2>/dev/null || true
fi

sleep 1

# Canonical shared-domain MCP URL (one domain for every node; ?user= selects it).
# Canonical shared-domain MCP URL (one domain for every node; ?user= selects it).
# The URL uses the link key when the gateway issued one: it is rotatable and it
# cannot open a tunnel, so a leaked link is revoked without reinstalling the node.
MCP_URL="$(node_sse_url "$ASSIGNED_USER" "${ASSIGNED_LINK:-$ASSIGNED_TOKEN}")"

# A connector is ready only after a real command has reached this device.
if [ "$PAIR" = true ]; then
    progress_stage "Проверка реальной связи с устройством / Verifying real device connection"
    (cd "$SCRIPT_DIR" && "$PYTHON_BIN" -u -m core.onboarding status --config-dir "$CONFIG_DIR") || exit 1
    MCP_URL="https://${GATEWAY}/mcp?user=${ASSIGNED_USER}&token=${ASSIGNED_LINK}"
    echo "ChatGPT: add a separate MCP connector for this device; choose No authentication."
fi

# Quick Copy to Clipboard
COPIED=false
B64_URL=$(printf "%s" "$MCP_URL" | base64 | tr -d '\r\n')
printf "\033]52;c;%s\a" "$B64_URL" 2>/dev/null || true
if command -v termux-clipboard-set >/dev/null 2>&1; then
    # Termux on a phone: termux-api plus the Termux:API app put the URL on
    # Android's clipboard, where the Gemini app can paste it. Without the app the
    # command exists but fails, so COPIED stays false and the hint says so.
    printf "%s" "$MCP_URL" | termux-clipboard-set 2>/dev/null && COPIED=true
elif command -v wl-copy >/dev/null 2>&1; then
    printf "%s" "$MCP_URL" | wl-copy 2>/dev/null && COPIED=true
elif command -v xclip >/dev/null 2>&1; then
    printf "%s" "$MCP_URL" | xclip -selection clipboard 2>/dev/null && COPIED=true
elif command -v xsel >/dev/null 2>&1; then
    printf "%s" "$MCP_URL" | xsel --clipboard --input 2>/dev/null && COPIED=true
elif command -v pbcopy >/dev/null 2>&1; then
    printf "%s" "$MCP_URL" | pbcopy 2>/dev/null && COPIED=true
elif command -v clip.exe >/dev/null 2>&1; then
    printf "%s" "$MCP_URL" | clip.exe 2>/dev/null && COPIED=true
fi

[ "$PAIR" = true ] || { clear 2>/dev/null || true; }

if [ "$PAIR" = true ]; then
    progress_stage "Готово: устройство отвечает / Ready: device answered"
    echo "Управление устройствами / Manage devices: https://${GATEWAY}/account"
    print_mcp_box "ChatGPT — private Server URL for ${ASSIGNED_USER}" "$MCP_URL" "No authentication; one connector per device"
elif [ "$LANG_CHOICE" = "ru" ]; then
    echo ""
    copy_msg=""
    if [ "$COPIED" = true ]; then
        copy_msg="[OK] ССЫЛКА СКОПИРОВАНА В БУФЕР ОБМЕНА! (Вставьте через Ctrl+V)"
    else
        copy_msg="Скопируйте ссылку выше"
    fi
    print_mcp_box "ССЫЛКА MCP-СЕРВЕРА ДЛЯ ПОДКЛЮЧЕНИЯ (СКОПИРУЙТЕ):" "$MCP_URL" "$copy_msg"
    echo ""
    echo -e "  ${BOLD}Куда вставлять ссылку в Google Gemini:${RESET}"
    echo -e "  1. Откройте в браузере: ${CYAN}https://gemini.google.com/customize/connectors${RESET}"
    echo -e "  2. Нажмите ${BOLD}'Добавить коннектор'${RESET} (Add connector) и задайте имя ${BOLD}'MyDevice'${RESET}"
    echo -e "  3. Вставьте скопированную ссылку в поле ${BOLD}'URL сервера'${RESET} (${BOLD}Ctrl+V${RESET}) и нажмите ${BOLD}Подключить${RESET}."
    echo ""
else
    echo ""
    copy_msg=""
    if [ "$COPIED" = true ]; then
        copy_msg="[OK] URL COPIED TO CLIPBOARD! (Paste with Ctrl+V)"
    else
        copy_msg="Copy the URL above"
    fi
    print_mcp_box "MCP SERVER CONNECTION URL (COPY THIS):" "$MCP_URL" "$copy_msg"
    echo ""
    echo -e "  ${BOLD}Where to paste this URL in Google Gemini:${RESET}"
    echo -e "  1. Open in your browser: ${CYAN}https://gemini.google.com/customize/connectors${RESET}"
    echo -e "  2. Click ${BOLD}'Add connector'${RESET} and name it ${BOLD}'MyDevice'${RESET}"
    echo -e "  3. Paste the URL into the ${BOLD}'Server URL'${RESET} field (${BOLD}Ctrl+V${RESET}) and click ${BOLD}Connect${RESET}."
    echo ""
fi

# The phone-specific steps (Termux:Boot, battery optimisation, clipboard) are the
# same in both modes; on a Linux/macOS/Windows node there is nothing to add.
if [ "$IS_TERMUX" = true ]; then
    print_termux_hints "rhm-agent"
fi
