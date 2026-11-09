import json
from pathlib import Path

import pytest

from core import onboarding as ob


def test_urls_reject_plain_http_and_credentials():
    for value in ['http://gateway.example', 'https://user:secret@example.com', 'https://example.com/mcp?token=secret']:
        with pytest.raises(ob.OnboardingError):
            ob.gateway_url(value)
    assert ob.gateway_url('http://127.0.0.1:8080', True) == 'http://127.0.0.1:8080'
    assert ob.gateway_url('gateway.example') == 'https://gateway.example'


def test_pair_receipt_and_resume_do_not_create_second_device(tmp_path, monkeypatch):
    calls = []
    credentials = {'status': 'approved', 'username': 'phone-2', 'token': 'device-secret', 'link_key': 'lk_private'}
    def request(base, path, **kwargs):
        calls.append((path, kwargs))
        if path.endswith('/pair'):
            return {'pair_code': 'ABCD-EFGH', 'device_secret': 'poll-secret', 'expires_in': 600}
        assert kwargs['headers'] == {'X-Pair-Secret': 'poll-secret'}
        return credentials
    monkeypatch.setattr(ob, 'request_json', request)
    lines = []
    config = ob.pair('https://gateway.example', tmp_path, 'phone', open_browser=False, announce=lines.append)
    assert config['MESH_USER'] == 'phone-2'
    assert 'device-secret' not in '\n'.join(lines)
    assert 'poll-secret' not in '\n'.join(lines)
    assert 'lk_private' not in '\n'.join(lines)
    assert (tmp_path / 'agent.env').stat().st_mode & 0o777 == 0o600
    assert '/mcp?user=phone-2&token=lk_private' in (tmp_path / 'chatgpt-url.txt').read_text()
    again = ob.pair('https://gateway.example', tmp_path, 'new-name', open_browser=False, announce=lines.append)
    assert again == config and len(calls) == 2
    with pytest.raises(ob.OnboardingError, match='different Gateway'):
        ob.pair('https://other.example', tmp_path, 'phone', open_browser=False)


def test_pending_pair_resumes_without_new_code(tmp_path, monkeypatch):
    ob.private_write(tmp_path/'onboarding.pending.json', json.dumps({'gateway':'https://gw.example',
        'pair_code':'ABCD-EFGH','device_secret':'poll','expires_at':ob.time.time()+600}))
    seen = []
    def request(base,path,**kwargs):
        seen.append(path)
        return {'status':'approved','username':'same','token':'tok','link_key':'lk_123'}
    monkeypatch.setattr(ob,'request_json', request)
    ob.pair('https://gw.example',tmp_path,'ignored',open_browser=False)
    assert seen == ['/v1/devices/pair/ABCD-EFGH']


def test_doctor_rejects_http_success_without_real_agent_reply(monkeypatch):
    monkeypatch.setattr(ob, 'request_json', lambda *a, **kw: {'status': 'healthy'})
    with pytest.raises(ob.OnboardingError, match='actual MCP'):
        ob.verify_device('https://gw.example', {'MESH_USER':'node','MESH_TOKEN':'tok'}, timeout=0)


def test_doctor_uses_device_header_and_correct_node(monkeypatch):
    def request(base, path, **kwargs):
        assert path == '/mcp?user=one'
        assert kwargs['headers']['Authorization'] == 'Bearer device-token'
        assert kwargs['data']['params']['name'] == 'mesh_status'
        return {'result': {'isError': False, 'content':[{'type':'text','text':'[NODE REACHABLE] hostname: actual-host'}]}}
    monkeypatch.setattr(ob,'request_json',request)
    assert 'actual-host' in ob.verify_device('https://gw.example',{'MESH_USER':'one','MESH_TOKEN':'device-token'})


def test_expired_pair_stops_without_touching_device_config(tmp_path):
    ob.private_write(tmp_path/'onboarding.pending.json', json.dumps({'gateway':'https://gw.example',
        'pair_code':'expired','device_secret':'poll','expires_at':0}))
    with pytest.raises(ob.OnboardingError, match='expired'):
        ob.pair('https://gw.example',tmp_path,'phone',open_browser=False)
    assert not (tmp_path/'agent.env').exists()
