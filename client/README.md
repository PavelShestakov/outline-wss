# Python Outline API client

`outline_vpn/outline_vpn.py` implements the existing Outline-compatible interface (`OutlineVPN`, `OutlineKey`) with optional HTTPS certificate fingerprint pinning.

```python
from outline_vpn.outline_vpn import OutlineVPN
api = OutlineVPN("https://NODE_IP:18443/m/SECRET#fp=BASE64URL_SHA256_CERT_PIN")
keys = api.get_keys()
```

The `fp` fragment is local-only metadata and never sent over HTTP. The pin is mandatory for self-signed node API certificates; a wrong pin causes the TLS request to fail. Do not commit actual server URLs with credentials.

For a custom local WebSocket port configured at installation, call `api.create_key(port=YOUR_WSS_PORT)`.
