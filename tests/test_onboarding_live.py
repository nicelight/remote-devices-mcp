"""Real HTTPS + WebSocket agents; no VPS, phone, production keys or browser used."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest

from core import onboarding as ob

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def live_gateway(tmp_path, monkeypatch):
    if not shutil.which('openssl'):
        pytest.skip('openssl needed for local trusted HTTPS test')
    cert, key = tmp_path/'cert.pem', tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost',
        '-addext','subjectAltName=IP:127.0.0.1,DNS:localhost'],check=True,capture_output=True)
    monkeypatch.setenv('SSL_CERT_FILE',str(cert))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    base=f'https://127.0.0.1:{port}'
    env={k:v for k,v in os.environ.items() if not k.startswith('MESH_')}
    env.update(MESH_PUBLIC_URL=base,MESH_REGISTRY_PATH=str(tmp_path/'registry.json'),
        MESH_GATEWAY_LOCK=str(tmp_path/'gateway.lock'),MESH_ADMIN_TOKEN='test-local-admin',
        MESH_REGISTER_MODE='pair',MESH_PRIVATE_HEALTH='1')
    logs=(tmp_path/'gateway.log').open('w')
    process=subprocess.Popen([sys.executable,'-m','uvicorn','gateway:app','--host','127.0.0.1',
        '--port',str(port),'--ssl-certfile',str(cert),'--ssl-keyfile',str(key),'--no-access-log'],
        cwd=REPO,env=env,stdout=logs,stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail((tmp_path/'gateway.log').read_text())
            try:
                ob.request_json(base,'/health'); break
            except ob.OnboardingError:
                time.sleep(.1)
        else: pytest.fail('local gateway did not start')
        yield base,env,tmp_path
    finally:
        process.terminate()
        try: process.wait(10)
        except subprocess.TimeoutExpired: process.kill();process.wait()
        logs.close()


def test_real_pair_two_agents_commands_and_revocation(live_gateway):
    base,env,tmp=live_gateway
    agents=[]
    configs=[]
    try:
        for i in range(2):
            config_dir=tmp/('node'+str(i));config_dir.mkdir()
            def approve(pending):
                ob.request_json(base,'/v1/devices/pair/'+pending['pair_code']+'/approve',
                    data={'username':'same-name'},headers={'X-Admin-Token':'test-local-admin'})
                with pytest.raises(ob.OnboardingError,match='403'):
                    ob.request_json(base,'/v1/devices/pair/'+pending['pair_code'])
            config=ob.pair(base,config_dir,'same-name',open_browser=False,progress=approve)
            configs.append(config)
            # Isolate heartbeat/locks from this workstation's real agent.
            script='from core import paths; paths.config_dir=lambda: '+repr(str(config_dir))+'; from core import agent; agent.main()'
            node_env=dict(env,MESH_CONFIG_FILE=str(config_dir/'agent.env'),MESH_WORKSPACE=str(config_dir),
                MESH_JOBS_DIR=str(config_dir/'jobs'),MESH_WEB_DIR=str(config_dir/'shares'))
            logfile=(config_dir/'agent.log').open('w')
            agent=subprocess.Popen([sys.executable,'-c',script],cwd=REPO,env=node_env,stdout=logfile,stderr=subprocess.STDOUT)
            agents.append((agent,logfile))
            evidence=ob.verify_device(base,config,timeout=15)
            assert str(config_dir) in evidence
            assert str(agent.pid) in evidence
        assert configs[0]['MESH_USER']=='same-name'
        assert configs[1]['MESH_USER']=='same-name-2'
        assert configs[0]['MESH_TOKEN']!=configs[1]['MESH_TOKEN']
        # Same name request never inherits somebody else's identity or key.
        for index,config in enumerate(configs):
            for method,args in [('write_file',{'path':str(tmp/f'node{index}'/'proof.txt'),'content':f'actual-device-{index}'}),
                                ('read_file',{'path':str(tmp/f'node{index}'/'proof.txt')}),
                                ('bash_exec',{'command':f'printf node-{index}'})]:
                tool='system_write' if method=='write_file' else method
                result=ob.request_json(base,'/mcp?user='+config['MESH_USER'],
                    data={'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':tool,'arguments':args}},
                    headers={'Authorization':'Bearer '+config['MESH_LINK_KEY']})['result']
                assert not result['isError'],result
            with pytest.raises(ob.OnboardingError,match='401'):
                ob.request_json(base,'/mcp?user='+configs[1-index]['MESH_USER'],
                    data={'jsonrpc':'2.0','id':1,'method':'tools/list'},
                    headers={'Authorization':'Bearer '+config['MESH_LINK_KEY']})
        ob.request_json(base,'/v1/devices/'+configs[0]['MESH_USER']+'/revoke',data={},
            headers={'Authorization':'Bearer '+configs[0]['MESH_TOKEN']})
        with pytest.raises(ob.OnboardingError,match='401'):
            ob.request_json(base,'/mcp?user='+configs[0]['MESH_USER'],data={'jsonrpc':'2.0','id':1,'method':'tools/list'},
                headers={'Authorization':'Bearer '+configs[0]['MESH_LINK_KEY']})
        assert 'NODE REACHABLE' in ob.verify_device(base,configs[1],timeout=3)
    finally:
        for agent,logfile in agents:
            agent.terminate()
            try:agent.wait(5)
            except subprocess.TimeoutExpired:agent.kill();agent.wait()
            logfile.close()


def test_published_installers_and_pairing_page(live_gateway):
    import urllib.request
    base,env,tmp=live_gateway
    started=ob.request_json(base,'/v1/devices/pair',data={'hostname':'<script>danger</script>','os':'Linux'})
    with urllib.request.urlopen(base+'/pair?code='+started['pair_code']) as response:
        page=response.read().decode()
        assert response.headers['Cache-Control']=='no-store'
        assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    assert '<script>danger</script>' not in page
    assert '&lt;script&gt;danger&lt;/script&gt;' in page
    assert started['device_secret'] not in page
    assert 'test-local-admin' not in page
    for path in ['/install.sh','/install.ps1','/core/onboarding.py','/core/device.py','/ops/windows/run-hidden.vbs']:
        with urllib.request.urlopen(base+path) as response:
            payload=response.read().decode('utf-8-sig')
            assert payload
            if path=='/install.sh':
                assert 'PAIR=true' in payload
                assert 'PUBLISHED_DOMAIN="'+base.removeprefix('https://')+'"' in payload
                subprocess.run(['bash','-n'],input=payload,text=True,check=True)
            if path=='/install.ps1':
                assert '[switch]$Pair = $true' in payload
                assert '$PublishedDomain = "'+base.removeprefix('https://')+'"' in payload
    for path in ['/core/../registry.json','/core/registry.json','/core/.env']:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(base+path)
        assert denied.value.code==404
