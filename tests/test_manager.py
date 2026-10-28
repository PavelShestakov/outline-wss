from pathlib import Path
import sqlite3
import yaml
import pytest

from app.core import Manager, Settings, metric_totals, Unavailable


def fixture(tmp_path, metrics=None):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({
        'web': {'servers': [{'id': 'outline-wss', 'listen': ['127.0.0.1:636']}]},
        'services': [{'listeners': [
            {'type': 'websocket-stream', 'web_server': 'outline-wss', 'path': '/randomsecret123/tcp'},
            {'type': 'websocket-packet', 'web_server': 'outline-wss', 'path': '/randomsecret123/udp'}
        ], 'keys': [{'id': 'test-user', 'cipher': 'chacha20-ietf-poly1305', 'secret': 'original-secret'}]}]
    }))
    s = Settings(path, tmp_path / 'manager/base.yaml', tmp_path / 'manager/state.db',
                 tmp_path / 'keys', 'wss://srv.polly-tech.ru', 'https://conn.polly-tech.ru',
                 'randomsecret123')
    reloads = []
    data = metrics if metrics is not None else {}
    m = Manager(s, reloader=lambda: reloads.append(True), metrics_fetcher=lambda: dict(data))
    m.poll()
    return m, data, reloads


def keys_in_config(manager):
    return yaml.safe_load(manager.s.config_file.read_text())['services'][0]['keys']


def test_create_url_and_preserve_test_key(tmp_path):
    m, data, reloads = fixture(tmp_path)
    created = m.create(name='Test-user')
    assert created['id'].isdigit() and created['port'] == 636
    assert created['accessUrl'].startswith('ssconf://conn.polly-tech.ru/')
    assert any(k['id'] == 'test-user' and k['secret'] == 'original-secret' for k in keys_in_config(m))
    assert any(k['id'] == created['id'] for k in keys_in_config(m))
    token = created['accessUrl'].split('/')[-1].split('.yml')[0]
    text = (m.s.keys_dir / f'{token}.yml').read_text()
    assert 'wss://srv.polly-tech.ru/randomsecret123/tcp' in text
    assert created['password'] in text
    assert reloads


def test_disable_at_zero_and_restore(tmp_path):
    m, data, _ = fixture(tmp_path)
    created = m.create()
    k = created['id']
    assert m.set_limit(k, 0)
    assert not any(row['id'] == k for row in keys_in_config(m))
    assert m.get(k)['dataLimit'] == {'bytes': 0}
    assert m.remove_limit(k)
    assert any(row['id'] == k for row in keys_in_config(m))


def test_usage_counters_quota(tmp_path):
    m, data, _ = fixture(tmp_path)
    created = m.create()
    kid = created['id']
    data[kid] = 0
    m.poll()
    m.set_limit(kid, 100)
    data[kid] = 70
    m.poll()
    assert m.metrics()[kid] == 70
    assert any(k['id'] == kid for k in keys_in_config(m))
    data[kid] = 125
    m.poll()
    assert m.metrics()[kid] == 125
    assert not any(k['id'] == kid for k in keys_in_config(m))
    # Reset of Prometheus counters must not reset lifetime usage.
    data[kid] = 4
    m.poll()
    assert m.metrics()[kid] == 129
    assert m.remove_limit(kid)
    assert any(k['id'] == kid for k in keys_in_config(m))


def test_delete_removes_generated_file(tmp_path):
    m, _, _ = fixture(tmp_path)
    created = m.create()
    token = created['accessUrl'].split('/')[-1].split('.yml')[0]
    assert (m.s.keys_dir / (token + '.yml')).exists()
    m.delete(created['id'])
    assert not (m.s.keys_dir / (token + '.yml')).exists()
    assert not any(k['id'] == created['id'] for k in keys_in_config(m))


def test_missing_metrics_refuse_new_key_and_disable_limited(tmp_path):
    m, _, _ = fixture(tmp_path)
    row = m.create()
    m.set_limit(row['id'], 10000)
    m.metrics_fetcher = lambda: (_ for _ in ()).throw(TimeoutError('metrics offline'))
    m.poll()
    assert not any(k['id'] == row['id'] for k in keys_in_config(m))
    with pytest.raises(Unavailable):
        m.create()


def test_prometheus_directional_accounting():
    text = '''# HELP shadowsocks_data_bytes Transfers
shadowsocks_data_bytes{dir="c<p",access_key="1000001",proto="tcp"} 50
shadowsocks_data_bytes{dir="p>t",access_key="1000001",proto="tcp"} 30
shadowsocks_data_bytes{dir="p>c",access_key="1000001",proto="tcp"} 200
shadowsocks_data_bytes{dir="c<p",access_key="other",proto="udp"} 11
'''
    assert metric_totals(text) == {'1000001': 80, 'other': 11}


def test_reloader_failure_rolls_back(tmp_path):
    m, _, _ = fixture(tmp_path)
    before = m.s.config_file.read_bytes()
    m.reloader = lambda: (_ for _ in ()).throw(Unavailable('reload failed'))
    with pytest.raises(Unavailable):
        m.create()
    assert m.list() == []
    assert m.s.config_file.read_bytes() == before


def test_restart_preserves_users_and_base(tmp_path):
    m, data, _ = fixture(tmp_path)
    new = m.create()
    m2 = Manager(m.s, reloader=lambda: None, metrics_fetcher=lambda: dict(data))
    assert m2.get(new['id'])['password'] == new['password']
    assert len(keys_in_config(m2)) == 2
