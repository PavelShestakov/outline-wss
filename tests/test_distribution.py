import base64
import importlib
import os
import re
from pathlib import Path

from app.core import Manager, Settings
from app.keys_server import TOKEN_RE


def test_key_url_does_not_use_old_brand(tmp_path):
    import yaml
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({
        'web': {'servers': [{'id': 'outline-wss', 'listen': ['0.0.0.0:636']}]},
        'services': [{'listeners': [
            {'type': 'websocket-stream', 'web_server': 'outline-wss', 'path': '/secretsecret/tcp'},
            {'type': 'websocket-packet', 'web_server': 'outline-wss', 'path': '/secretsecret/udp'}
        ], 'keys': []}]
    }))
    s = Settings(config, tmp_path / 'base.yaml', tmp_path / 'state.db', tmp_path / 'keys',
                 'wss://srv.example.org', 'https://conn.example.org', 'secretsecret')
    manager = Manager(s, metrics_fetcher=lambda: {}, reloader=lambda: None)
    manager.poll()
    result = manager.create()
    assert result['accessUrl'].startswith('ssconf://conn.example.org/')
    assert result['accessUrl'].endswith('#Outline-WSS')
    assert TOKEN_RE.fullmatch('/' + result['accessUrl'].split('/')[-1].split('#')[0])


def test_fingerprint_base64url_parser():
    from client.outline_vpn.outline_vpn import OutlineVPN
    digest = bytes(range(32))
    pin = base64.urlsafe_b64encode(digest).decode().rstrip('=')
    api = OutlineVPN('https://203.0.113.12:18443/m/' + ('x' * 24) + '#fp=' + pin)
    assert api.api_url.endswith('/m/' + ('x' * 24))
    assert api.session.adapters['https://'].fingerprint == digest.hex()
    assert len('https://255.255.255.255:18443/m/' + ('x' * 24) + '#fp=' + pin) <= 128


def test_nginx_and_compose_defaults_are_internal():
    root = Path(__file__).resolve().parents[1]
    compose = (root / 'compose.yaml').read_text()
    assert 'image: nginx:stable-alpine' in compose
    assert 'docker.sock' not in compose
    assert 'api-tls:' in compose
    assert 'cloudflared:' in compose
    assert '"${API_PORT}:443/tcp"' in compose
    assert '127.0.0.1:9091' in (root / 'app/supervisor.py').read_text()
