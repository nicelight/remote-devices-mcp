#!/usr/bin/env python3
"""Enroll a node; print no credentials. Run from a checkout with websockets installed."""
import argparse
import json
import os
from pathlib import Path
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway', required=True, help='https://gateway.example.com')
    parser.add_argument('--name', required=True)
    parser.add_argument('--enroll-secret-file', required=True, help='File containing only the enrollment secret')
    parser.add_argument('--config-dir', default='~/.config/remote-harness-mcp')
    args = parser.parse_args()
    gateway = args.gateway.rstrip('/')
    if not gateway.startswith('https://'):
        parser.error('Gateway must use HTTPS')
    config_dir = Path(args.config_dir).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = config_dir / 'agent.env'
    if config.exists():
        parser.error(f'{config} already exists; back it up and move it before enrollment')
    secret = Path(args.enroll_secret_file).expanduser().read_text().strip()
    request = urllib.request.Request(gateway + '/api/register',
        data=json.dumps({'username': args.name, 'os': os.name}).encode(),
        headers={'Content-Type': 'application/json', 'X-Enroll-Secret': secret}, method='POST')
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if result.get('status') != 'success':
        raise RuntimeError('Enrollment failed')
    # O_EXCL prevents clobbering existing credentials; owner-only before first byte.
    with os.fdopen(os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:
        handle.write(f'MESH_PUBLIC_URL={gateway}\nMESH_USER={result["username"]}\nMESH_TOKEN={result["token"]}\nMESH_UPDATE_AUTO=0\nMESH_UPDATE_CHECK=0\n')
    connector = config_dir / 'chatgpt-url.txt'
    with os.fdopen(os.open(connector, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as handle:
        handle.write(result['sse_url'].replace('/sse?', '/mcp?') + '\n')
    print(f'Enrolled {result["username"]}; agent config: {config}; private connector URL: {connector}')


if __name__ == '__main__':
    main()
