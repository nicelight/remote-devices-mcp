"""Shared device pairing and diagnostics for the existing platform installers.

Standard library only. A human code authorises a pairing, while a separate
256-bit secret (never in a URL) lets only the initiating device collect keys.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from core import domain, paths


class OnboardingError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def announce_progress(message):
    print(message, flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward device credentials to a redirected destination.
        return None


def gateway_url(value: str, allow_local_http: bool = False) -> str:
    if '://' not in value:
        value = 'https://' + value
    p = urllib.parse.urlsplit(value)
    local = p.hostname in {'127.0.0.1', 'localhost', '::1'}
    if (p.scheme != 'https' and not (allow_local_http and local and p.scheme == 'http')) \
            or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in {'', '/'}:
        raise OnboardingError('Use the HTTPS Gateway origin, without a path, credentials or query')
    return value.rstrip('/')


def request_json(base, path, *, data=None, headers=None, timeout=35):
    req = urllib.request.Request(base + path, data=None if data is None else json.dumps(data).encode(),
                                 headers={'Content-Type': 'application/json', 'Accept': 'application/json', **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not include the request, credentials or response body in error output.
        messages = {
            403: 'Доступ отклонён. В браузере войдите через Google и подтвердите своё устройство; не вводите ключ администратора.',
            404: 'Заявка истекла или Gateway был перезапущен. Запустите установщик ещё раз.',
            410: 'Привязка отозвана. Запустите установщик для новой заявки.',
            429: 'Слишком много запросов. Подождите минуту и повторите.',
            503: 'Gateway или устройство пока недоступны. Проверьте состояние службы.',
        }
        raise OnboardingError('Gateway returned HTTP %d. %s' % (exc.code,
            messages.get(exc.code, 'Проверьте Gateway или выполните status.')), exc.code) from None
    except (OSError, ValueError) as exc:
        raise OnboardingError('Gateway unavailable: check internet, DNS and the HTTPS certificate') from None


def private_dir(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        principal = subprocess.check_output(['whoami'], text=True).strip()
        result = subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r', principal + ':(OI)(CI)F'],
                                capture_output=True)
        if result.returncode:
            raise OnboardingError('Cannot restrict access to the configuration directory')
    else:
        directory.chmod(0o700)


def private_write(path, text):
    path = Path(path)
    private_dir(path.parent)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def connector_url(base, config):
    key = config.get('MESH_LINK_KEY', '')
    if not key.startswith('lk_'):
        raise OnboardingError('No connector key saved; use the existing link rotation procedure')
    return base + '/mcp?' + urllib.parse.urlencode({'user': config['MESH_USER'], 'token': key})


def confirm_browser(announce=announce_progress):
    prompt = 'Приступить к авторизации через браузер? [Enter — да / нет]: '
    try:
        if sys.stdin.isatty():
            answer = input(prompt)
        else:
            # A curl | bash installation must not read an answer from the script
            # pipe. Ask through the controlling terminal when one is available.
            with open('CONIN$' if os.name == 'nt' else '/dev/tty', 'r') as terminal:
                print(prompt, end='', flush=True)
                answer = terminal.readline()
                if not answer:
                    raise EOFError
    except (EOFError, OSError):
        announce('Нет интерактивного ввода. Откройте ссылку выше вручную.')
        return False
    return answer.strip().casefold() not in {'нет', 'no', 'n'}


def pair(base, config_dir, name, *, timeout=600, open_browser=True, announce=announce_progress, progress=None):
    """Resume the same pairing/config on retries; never silently re-enrol a node."""
    root = Path(config_dir)
    private_dir(root)
    config_path = root / 'agent.env'
    if config_path.exists():
        config = domain.read_env_file(str(config_path))
        previous = config.get('MESH_PUBLIC_URL', '')
        if previous.rstrip('/') != base:
            raise OnboardingError('This device is already configured for a different Gateway; disconnect it explicitly first')
        if not config.get('MESH_TOKEN') or not config.get('MESH_USER'):
            raise OnboardingError('Existing agent.env is incomplete; preserve it before repairing')
        connector_url(base, config)
        announce('Existing device configuration reused; no new registration created.')
        return config
    pending_path = root / 'onboarding.pending.json'
    if pending_path.exists():
        pending = json.loads(pending_path.read_text())
        if pending['gateway'] != base:
            raise OnboardingError('A pairing with another Gateway is pending')
        if pending['expires_at'] <= time.time():
            pending_path.unlink()
            announce('Срок старой заявки истёк. Создаём новую / Previous request expired; starting a new one.')
    if not pending_path.exists():
        announce('Создаём защищённую заявку / Creating a secure device request…')
        started = request_json(base, '/v1/devices/pair', data={
            'hostname': name, 'os': platform.system(), 'arch': platform.machine()})
        # Never open an arbitrary verification URL supplied by a server response.
        pending = {'gateway': base, 'pair_code': started['pair_code'],
                   'device_secret': started['device_secret'],
                   'expires_at': time.time() + started['expires_in']}
        private_write(pending_path, json.dumps(pending))
    code = pending['pair_code']
    verify_url = base + '/pair?' + urllib.parse.urlencode({'code': code})
    announce('Войдите через Google и нажмите «Подключить устройство». Код вводить не нужно.\nSign in with Google and confirm this device; no code entry needed.\n' + verify_url + '\nКод для сверки / Device code: ' + code)
    if open_browser and confirm_browser(announce):
        try:
            if not webbrowser.open(verify_url):
                announce('Не удалось открыть браузер. Откройте ссылку выше вручную.')
        except Exception:
            announce('Не удалось открыть браузер. Откройте ссылку выше вручную.')
    if progress:
        progress(pending)
    deadline = min(time.time() + timeout, pending['expires_at'])
    next_notice = 0
    while time.time() < deadline:
        if time.time() >= next_notice:
            announce('Ожидаем подтверждения в браузере / Waiting for browser confirmation (%ss осталось / remaining)…' % max(0, int(deadline-time.time())))
            next_notice = time.time() + 10
        try:
            status = request_json(base, '/v1/devices/pair/' + urllib.parse.quote(code, safe=''),
                                  headers={'X-Pair-Secret': pending['device_secret']}, timeout=15)
        except OnboardingError as exc:
            if exc.status_code in {404, 410}:
                pending_path.unlink(missing_ok=True)
            raise
        if status.get('status') == 'approved':
            config = {'MESH_PUBLIC_URL': base,
                      'MESH_USER': status['username'], 'MESH_TOKEN': status['token'],
                      'MESH_LINK_KEY': status['link_key'], 'MESH_UPDATE_AUTO': '0', 'MESH_UPDATE_CHECK': '0',
                      'MESH_READ_ONLY': '1' if status.get('access') == 'read_only' else '0'}
            if not all(config[k] for k in ('MESH_USER', 'MESH_TOKEN', 'MESH_LINK_KEY')):
                raise OnboardingError('Gateway returned incomplete device credentials')
            # Persist a receipt before committing agent.env, so a crash/expiry
            # between writes can be retried without issuing another identity.
            private_write(root / 'onboarding.receipt.json', json.dumps(config))
            save_config(root, config)
            pending_path.unlink(missing_ok=True)
            announce('Device approved: ' + config['MESH_USER'] + '. Configuration saved privately.')
            return config
        time.sleep(2)
    raise OnboardingError('Pairing is still waiting for confirmation. Re-run to continue with the same code.')


def save_config(root, config):
    private_write(root / 'domain.env', 'MESH_PUBLIC_URL=' + config['MESH_PUBLIC_URL'] + '\n')
    private_write(root / 'chatgpt-url.txt', connector_url(config['MESH_PUBLIC_URL'], config) + '\n')
    private_write(root / 'agent.env', ''.join(k + '=' + str(v) + '\n' for k, v in config.items()))
    (root / 'onboarding.receipt.json').unlink(missing_ok=True)


def load_device(config_dir):
    config = domain.read_env_file(str(Path(config_dir) / 'agent.env'))
    if not config.get('MESH_USER') or not config.get('MESH_TOKEN'):
        raise OnboardingError('Device is not paired. Run the installer to connect it.')
    return config


def verify_device(base, config, timeout=40, announce=None):
    deadline = time.monotonic() + timeout
    last_error = 'Agent not connected'
    next_notice = 0
    while True:
        if announce and time.monotonic() >= next_notice:
            announce('Проверяем реальный ответ MCP / Checking a real MCP response…')
            next_notice = time.monotonic() + 5
        try:
            response = request_json(base, '/mcp?' + urllib.parse.urlencode({'user': config['MESH_USER']}),
                data={'jsonrpc': '2.0', 'id': secrets.randbelow(2**31), 'method': 'tools/call',
                      'params': {'name': 'mesh_status', 'arguments': {}}},
                headers={'Authorization': 'Bearer ' + config['MESH_TOKEN']}, timeout=max(1, min(5, deadline-time.monotonic())))
            result = response.get('result', {})
            text = '\n'.join(c.get('text', '') for c in result.get('content', []))
            if not result.get('isError') and 'NODE REACHABLE' in text:
                return text
            last_error = 'Agent has not answered the actual MCP call yet'
        except OnboardingError as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            raise OnboardingError(last_error + '. Check the agent service and agent.log; HTTP health alone is insufficient.')
        time.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Pair a device, check real MCP connectivity, or show its private ChatGPT link.')
    parser.add_argument('action', choices=['pair', 'status', 'link'])
    parser.add_argument('--gateway', default=None)
    parser.add_argument('--config-dir', default=paths.config_dir())
    parser.add_argument('--name', default=socket.gethostname())
    parser.add_argument('--timeout', type=int, default=None)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--allow-local-http', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--result-file', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        saved = domain.read_env_file(str(Path(args.config_dir) / 'agent.env'))
        base = gateway_url(args.gateway or saved.get('MESH_PUBLIC_URL') or domain.public_base_url(), args.allow_local_http)
        if args.action == 'pair':
            receipt = Path(args.config_dir) / 'onboarding.receipt.json'
            if receipt.exists() and not (Path(args.config_dir) / 'agent.env').exists():
                config = json.loads(receipt.read_text())
                if config['MESH_PUBLIC_URL'] != base:
                    raise OnboardingError('A different Gateway has an unfinished receipt')
                save_config(Path(args.config_dir), config)
            else:
                config = pair(base, args.config_dir, args.name, timeout=args.timeout or 600,
                              open_browser=not args.no_browser)
            if args.result_file:
                private_write(Path(args.result_file), json.dumps(config))
        else:
            config = load_device(args.config_dir)
            # Stored identity wins over a machine-wide default on status/link.
            base = gateway_url(config['MESH_PUBLIC_URL'], args.allow_local_http)
            evidence = verify_device(base, config, timeout=args.timeout or 40, announce=announce_progress)
            print('CONNECTED — real MCP response from ' + config['MESH_USER'] + '\n' + evidence)
            if args.action == 'link':
                print('Private ChatGPT Server URL (No authentication):\n' + connector_url(base, config))
        return 0
    except (OnboardingError, OSError, ValueError, KeyError) as exc:
        print('Connection not completed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
