#!/usr/bin/env python3
"""Exercise real remote agents via TLS + MCP SDK. Input is a private registration JSON."""
import argparse
import asyncio
import json
import secrets
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client


async def verify(registration, output, auth, transport):
    data = json.loads(Path(registration).read_text())
    url = data['sse_url'].replace('/sse?', '/mcp?')
    headers = {}
    if auth == 'bearer':
        from urllib.parse import urlsplit, parse_qs
        parts = urlsplit(url)
        user = parse_qs(parts.query)['user'][0]
        url = f'{parts.scheme}://{parts.netloc}{parts.path}?user={user}'
        headers['Authorization'] = f'Bearer {data["token"]}'
    results = []
    async with httpx.AsyncClient(headers=headers, timeout=40) as client:
        connection = (sse_client(url.replace('/mcp?', '/sse?'), headers=headers)
                      if transport == 'sse' else streamable_http_client(url, http_client=client))
        async with connection as streams:
            read, write = streams[:2]
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) >= 26
                for tool in tools.tools:
                    if tool.name in {'bash_exec', 'write_file', 'device_control', 'share_file'}:
                        assert tool.annotations.model_dump(by_alias=True)["readOnlyHint"] is False, tool.name
                results.append({'step': 'initialize/tools/list', 'protocol': init.model_dump(by_alias=True)["protocolVersion"],
                                'tools': len(tools.tools), 'auth': auth, 'transport': transport})
                async def call(name, arguments):
                    response = await session.call_tool(name, arguments)
                    text = '\n'.join(c.text for c in response.content if hasattr(c, 'text'))
                    assert not response.model_dump(by_alias=True).get("isError", False), (name, text)
                    return text
                for name in ['mesh_status', 'system_info', 'system_vitals']:
                    text = await call(name, {})
                    results.append({'step': name, 'result': text})
                # Round-trip a unique file, so this cannot pass on a canned status.
                marker = 'mcp-live-' + secrets.token_hex(10)
                workspace = '/var/lib/rhm-node' if data['username'] == 'vps-node' else str(Path.cwd() / '.state/local-node')
                path = workspace + '/integration-proof.txt'
                await call('system_write' if workspace.startswith('/var/') else 'write_file', {'path': path, 'content': marker})
                text = await call('read_file', {'path': path})
                assert marker in text
                results.append({'step': 'write_file/read_file', 'path': path, 'marker': marker})
                for n in range(10):
                    text = await call('bash_exec', {'command': f'printf MCP-PROBE-{n}'})
                    assert f'MCP-PROBE-{n}' in text
                results.append({'step': 'bash_exec', 'successful_repeated_calls': 10})
                # Simultaneous requests travel independently across the same tunnel.
                texts = await asyncio.gather(*(call('bash_exec', {'command': f'printf PARALLEL-{n}'}) for n in range(5)))
                assert all(f'PARALLEL-{n}' in text for n,text in enumerate(texts))
                results.append({'step': 'parallel calls', 'count': 5})
    Path(output).write_text(json.dumps({'node': data['username'], 'passed': True, 'checks': results}, indent=2))
    print(f'{data["username"]}: MCP SDK {transport}/{auth}, {len(tools.tools)} tools; telemetry, file round-trip, 10 sequential + 5 parallel commands passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('registration')
    parser.add_argument('--output', required=True)
    parser.add_argument('--auth', choices=['link', 'bearer'], default='link')
    parser.add_argument('--transport', choices=['http', 'sse'], default='http')
    args = parser.parse_args()
    asyncio.run(verify(args.registration, args.output, args.auth, args.transport))
