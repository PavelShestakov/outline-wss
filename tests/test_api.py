import importlib
import os
import yaml

from fastapi.testclient import TestClient
from app.core import Settings, Manager


def test_shadowbox_endpoints(tmp_path, monkeypatch):
    cfg = tmp_path / 'server.yml'
    cfg.write_text(yaml.safe_dump({
        'web': {'servers': [{'id': 'myweb', 'listen': ['127.0.0.1:636']}]},
        'services': [{'listeners': [{'type': 'websocket-stream', 'web_server': 'myweb', 'path': '/secretsecret/tcp'},
                                   {'type': 'websocket-packet', 'web_server': 'myweb', 'path': '/secretsecret/udp'}],
                      'keys': []}]
    }))
    s = Settings(cfg,tmp_path/'base.yml',tmp_path/'state.db',tmp_path/'keys',
        'wss://srv.polly-tech.ru','https://conn.polly-tech.ru','secretsecret')
    m = Manager(s, reloader=lambda: None, metrics_fetcher=lambda: {})
    m.poll()
    monkeypatch.setenv('OUTLINE_ADMIN_TOKEN','X'*24)
    mod = importlib.import_module('app.main')
    monkeypatch.setattr(mod,'_manager',m)
    url='/m/'+'X'*24
    with TestClient(mod.app) as c:
        assert c.get('/access-keys/').status_code==404
        response=c.post(url+'/access-keys/',json={'port':636})
        assert response.status_code==201,response.text
        kid=response.json()['id']
        assert c.get(url+'/access-keys/').json()['accessKeys'][0]['id']==kid
        assert c.put(url+f'/access-keys/{kid}/name',files={'name':(None,'Test-user')}).status_code==204
        assert c.put(url+f'/access-keys/{kid}/data-limit',json={'limit':{'bytes':0}}).status_code==204
        assert c.get(url+'/access-keys/').json()['accessKeys'][0]['dataLimit']['bytes']==0
        assert c.delete(url+f'/access-keys/{kid}/data-limit').status_code==204
        assert c.get(url+'/metrics/transfer').json()['bytesTransferredByUserId'][kid]==0
        assert c.put(url+'/server/port-for-new-access-keys',json={'port':637}).status_code==409
        assert c.delete(url+f'/access-keys/{kid}').status_code==204
        assert c.get(url+'/access-keys/').json()=={'accessKeys': []}
