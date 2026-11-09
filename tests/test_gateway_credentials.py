"""Credentials: a device token for the tunnel, a link key for URLs.

The regression this file guards:

* ``/api/register`` used to return the token of an EXISTING node to anyone who
  presented that node's MAC address. A MAC is visible to the whole LAN and is
  trivially spoofed, so that was an account takeover with extra steps.
* the node's single secret travelled in every URL (``?token=``), which put it in
  nginx access logs, browser history, ``Referer`` headers, a chat client's
  connector settings and the installer's clipboard.
* nothing rotated: a leaked link could only be fixed by reinstalling the node.

The properties asserted here are the ones the paid service depends on:
possession of the device token is the only way to inherit a registration, the
URL credential is a rotatable link key, and the tunnel refuses link keys.
"""

import asyncio
import json
import logging
from urllib.parse import urlparse

import pytest
from starlette.requests import Request

import gateway


def _request(query: str = "", method: str = "GET", body: bytes = b"",
             headers: dict | None = None, path_params: dict | None = None,
             path: str = "/sse", host: str = "smart-server.online") -> Request:
    raw_headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": raw_headers,
        "query_string": urlparse(f"/{query}").query.encode(),
        "scheme": "https",
        "server": (host, 443),
        "client": ("127.0.0.1", 12345),
        "path_params": path_params or {},
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "REGISTRY_PATH", str(tmp_path / "registry.json"))
    gateway._rate_windows.clear()
    gateway.pending_pairs.clear()
    yield
    gateway._rate_windows.clear()
    gateway.pending_pairs.clear()


def _register(username: str, mac: str = "", token: str | None = None, auto_suffix: bool = False,
              headers: dict | None = None) -> dict:
    payload = {"username": username, "mac_address": mac, "os": "linux", "arch": "x64",
               "auto_suffix": auto_suffix}
    if token:
        payload["token"] = token
    body = json.dumps(payload).encode()
    response = asyncio.run(gateway.api_register(_request(method="POST", body=body, headers=headers)))
    return {"status_code": response.status_code, **json.loads(response.body)}


def _registry() -> dict:
    try:
        with open(gateway.REGISTRY_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


# ---------------------------------------------------------------------------
# Storage: no plaintext device token
# ---------------------------------------------------------------------------

def test_new_registration_stores_the_device_token_only_as_a_hash():
    data = _register("node-alpha")

    assert data["status"] == "success"
    entry = _registry()["node-alpha"]
    assert "token" not in entry, "the device token must never be stored in plaintext"
    assert entry["token_sha256"] == gateway.hash_secret(data["token"])
    assert data["link_key"].startswith("lk_")
    assert entry["link_key"] == data["link_key"]
    assert data["token"] not in data["sse_url"]


# ---------------------------------------------------------------------------
# MAC is inventory, not identity
# ---------------------------------------------------------------------------

def test_mac_address_does_not_hand_out_another_nodes_token():
    first = _register("node-one", mac="AA:BB:CC:DD:EE:FF")
    second = _register("node-two", mac="AA:BB:CC:DD:EE:FF")

    assert first["token"] != second["token"], "a shared MAC must not return the existing token"
    assert second["status"] == "success"
    assert set(_registry()) == {"node-one", "node-two"}


def test_re_registration_by_mac_and_name_alone_is_refused():
    _register("node-one", mac="AA:BB:CC:DD:EE:FF")
    attempt = _register("node-one", mac="AA:BB:CC:DD:EE:FF")

    assert attempt["status_code"] == 409
    assert "token" not in attempt
    assert "suggested_username" in attempt


def test_re_registration_with_the_device_token_reuses_the_same_node():
    first = _register("node-one")
    again = _register("node-one", token=first["token"])

    assert again["status_code"] == 200
    assert again["reused"] is True
    assert again["username"] == "node-one"
    assert again["link_key"] == first["link_key"], "a re-register must not rotate the link key"
    assert set(_registry()) == {"node-one"}


# ---------------------------------------------------------------------------
# URL credential == link key; device token stays in a header
# ---------------------------------------------------------------------------

def test_device_token_in_the_query_string_is_rejected():
    data = _register("node-alpha")
    ok, message, _user = gateway.verify_token(_request(f"?user=node-alpha&token={data['token']}"))
    assert ok is False
    assert "does not accept" in message or "link key" in message


def test_device_token_is_accepted_in_the_authorization_header():
    data = _register("node-alpha")
    ok, _message, user = gateway.verify_token(
        _request("?user=node-alpha", headers={"Authorization": f"Bearer {data['token']}"})
    )
    assert ok is True and user == "node-alpha"


def test_link_key_is_accepted_in_the_query_string():
    data = _register("node-alpha")
    ok, _message, user = gateway.verify_token(_request(f"?user=node-alpha&token={data['link_key']}"))
    assert ok is True and user == "node-alpha"


def test_link_key_is_not_accepted_where_the_device_token_belongs():
    """The tunnel is the machine's identity: a link key must not open one."""
    data = _register("node-alpha")
    entry = _registry()["node-alpha"]
    assert gateway.token_matches_link(entry, data["link_key"]) is True
    assert gateway.token_matches_device(entry, data["link_key"]) is False


# ---------------------------------------------------------------------------
# Rotation and revocation
# ---------------------------------------------------------------------------

def test_rotate_replaces_the_link_key_and_kills_the_old_url(monkeypatch):
    monkeypatch.setattr(gateway, "ADMIN_TOKEN", "admin-secret")
    data = _register("node-alpha")

    response = asyncio.run(gateway.device_rotate(_request(
        method="POST",
        headers={"X-Admin-Token": "admin-secret"},
        path_params={"user": "node-alpha"},
    )))
    rotated = json.loads(response.body)

    assert response.status_code == 200
    assert rotated["link_key"] != data["link_key"]
    old_ok, _m, _u = gateway.verify_token(_request(f"?user=node-alpha&token={data['link_key']}"))
    new_ok, _m, _u = gateway.verify_token(_request(f"?user=node-alpha&token={rotated['link_key']}"))
    assert old_ok is False
    assert new_ok is True


def test_rotation_requires_the_device_token_or_admin(monkeypatch):
    monkeypatch.setattr(gateway, "ADMIN_TOKEN", "admin-secret")
    data = _register("node-alpha")

    denied = asyncio.run(gateway.device_rotate(_request(
        method="POST",
        headers={"Authorization": f"Bearer {data['link_key']}"},
        path_params={"user": "node-alpha"},
    )))
    assert denied.status_code == 403

    allowed = asyncio.run(gateway.device_rotate(_request(
        method="POST",
        headers={"Authorization": f"Bearer {data['token']}"},
        path_params={"user": "node-alpha"},
    )))
    assert allowed.status_code == 200


def test_revoke_removes_the_node(monkeypatch):
    monkeypatch.setattr(gateway, "ADMIN_TOKEN", "admin-secret")
    data = _register("node-alpha")

    response = asyncio.run(gateway.device_revoke(_request(
        method="POST",
        headers={"Authorization": f"Bearer {data['token']}"},
        path_params={"user": "node-alpha"},
    )))

    assert response.status_code == 200
    assert _registry() == {}
    ok, _message, _user = gateway.verify_token(_request(f"?user=node-alpha&token={data['link_key']}"))
    assert ok is False


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

def _pair_start() -> dict:
    body = json.dumps({"hostname": "workstation", "os": "linux", "arch": "x64"}).encode()
    response = asyncio.run(gateway.device_pair_start(_request(method="POST", body=body, path="/v1/devices/pair")))
    return {"status_code": response.status_code, **json.loads(response.body)}


def test_pairing_issues_nothing_before_an_admin_approves():
    started = _pair_start()
    assert started["status_code"] == 201
    assert "token" not in started
    assert _registry() == {}, "a pairing request alone must not create a node"

    code = started["pair_code"]
    status = asyncio.run(gateway.device_pair_status(_request(path_params={"code": code})))
    assert json.loads(status.body)["status"] == "pending"


def test_pairing_refuses_to_approve_without_admin_rights(monkeypatch):
    monkeypatch.setattr(gateway, "ADMIN_TOKEN", "admin-secret")
    code = _pair_start()["pair_code"]
    body = json.dumps({"username": "workstation"}).encode()

    response = asyncio.run(gateway.device_pair_approve(_request(
        method="POST", body=body, path_params={"code": code},
        headers={"Authorization": "Bearer guessed"},
    )))
    assert response.status_code == 403
    assert _registry() == {}


def test_pairing_issues_once_and_allows_same_device_to_retry(monkeypatch):
    monkeypatch.setattr(gateway, "ADMIN_TOKEN", "admin-secret")
    started = _pair_start()
    code = started["pair_code"]
    body = json.dumps({"username": "workstation"}).encode()

    approved = asyncio.run(gateway.device_pair_approve(_request(
        method="POST", body=body, path_params={"code": code},
        headers={"X-Admin-Token": "admin-secret"},
    )))
    assert approved.status_code == 200

    collected = asyncio.run(gateway.device_pair_status(_request(path_params={"code": code}, headers={"X-Pair-Secret": started["device_secret"]})))
    credentials = json.loads(collected.body)
    assert credentials["status"] == "approved"
    assert credentials["username"] == "workstation"
    assert credentials["link_key"].startswith("lk_")
    assert credentials["token"] not in credentials["sse_url"]

    again = asyncio.run(gateway.device_pair_status(_request(path_params={"code": code}, headers={"X-Pair-Secret": started["device_secret"]})))
    assert again.status_code == 200
    assert json.loads(again.body) == credentials
    stolen = asyncio.run(gateway.device_pair_status(_request(path_params={"code": code})))
    assert stolen.status_code == 403


def test_a_leaked_pair_code_is_not_enough_to_get_credentials():
    code = _pair_start()["pair_code"]
    status = asyncio.run(gateway.device_pair_status(_request(path_params={"code": code})))
    payload = json.loads(status.body)
    assert payload == {"status": "pending", "expires_in": payload["expires_in"]}


# ---------------------------------------------------------------------------
# Legacy registries and logs
# ---------------------------------------------------------------------------

def test_a_pre_0_4_6_registry_entry_still_authenticates():
    """Upgrading the gateway must not disconnect nodes that already exist."""
    gateway.save_registry({"old-node": {"token": "legacy-token", "created_at": "2025-01-01"}})
    ok, _message, user = gateway.verify_token(_request("?user=old-node&token=legacy-token"))
    assert ok is True and user == "old-node"
    ok_header, _message, _user = gateway.verify_token(
        _request("?user=old-node", headers={"Authorization": "Bearer legacy-token"})
    )
    assert ok_header is True


def test_reinstalling_a_legacy_node_keeps_its_existing_url_working():
    """Re-running the installer must not break the connector already configured.

    A pre-0.4.6 node is re-registered with the token it already has: the token is
    unchanged (the gateway echoes the one that was proved), the entry is upgraded
    to a link key, and BOTH URLs keep working - the new `lk_` one the installer
    prints and the old one already pasted into Gemini. Killing the old one here
    was the silent breakage this test exists to prevent; `rotate-token` is what
    ends it, deliberately.
    """
    gateway.save_registry({"old-node": {"token": "legacy-token", "created_at": "2025-01-01"}})

    again = _register("old-node", token="legacy-token")

    assert again["status_code"] == 200
    assert again["reused"] is True
    assert again["token"] == "legacy-token", "a reinstall must not mint a new token"

    entry = _registry()["old-node"]
    assert "token" not in entry, "the plaintext token is replaced by its hash"
    assert entry["token_sha256"] == gateway.hash_secret("legacy-token")

    old_url, _m, _u = gateway.verify_token(_request("?user=old-node&token=legacy-token"))
    new_url, _m, _u = gateway.verify_token(_request(f"?user=old-node&token={again['link_key']}"))
    assert old_url is True, "the URL already configured in the client keeps working"
    assert new_url is True, "and the link-key URL the installer printed works too"


def test_access_log_records_are_scrubbed():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5555", "GET", "/sse?user=node&token=lk_deadbeef", "1.1", 200),
        None,
    )
    gateway.CredentialRedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "lk_deadbeef" not in rendered
    assert "token=<redacted>" in rendered
