import asyncio

import pytest

import gateway


def test_second_gateway_with_same_registry_refuses_start(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_LOCK", str(tmp_path / "gateway.lock"))

    async def check():
        async with gateway.lifespan(gateway.app):
            with pytest.raises(RuntimeError, match="exactly one worker"):
                async with gateway.lifespan(gateway.app):
                    pass
        # Lock is released on shutdown.
        async with gateway.lifespan(gateway.app):
            pass

    asyncio.run(check())


def test_mutation_is_not_replayed_after_uncertain_disconnect(monkeypatch):
    class BrokenSocket:
        calls = 0

        async def send_text(self, payload):
            self.calls += 1
            raise ConnectionError("response lost")

    ws = BrokenSocket()
    monkeypatch.setattr(gateway, "active_tunnels", {"one": {"ws": ws, "pending": {}}})
    result = asyncio.run(gateway.call_remote_tool("one", "bash_exec", {"command": "echo changed"}))
    assert ws.calls == 1
    assert "outcome unknown" in result["error"]
    assert not gateway.active_tunnels["one"]["pending"]


def test_corrupt_registry_is_not_silently_replaced(tmp_path, monkeypatch):
    registry = tmp_path / 'registry.json'
    registry.write_text('{broken')
    monkeypatch.setattr(gateway, 'REGISTRY_PATH', str(registry))
    with pytest.raises(RuntimeError, match='Cannot read device registry'):
        gateway.load_registry()
    assert registry.read_text() == '{broken'


def test_options_does_not_require_auth_or_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_LOCK", str(tmp_path / "gateway.lock"))
    from starlette.testclient import TestClient
    # Isolated protocol preflight check.
    with TestClient(gateway.app, raise_server_exceptions=True) as client:
        response = client.options('/mcp?user=one')
        assert response.status_code == 204
        assert 'POST' in response.headers['allow']
