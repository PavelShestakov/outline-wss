"""
API wrapper for Outline VPN
"""
import typing
from dataclasses import dataclass
import requests
from urllib3 import PoolManager
from urllib.parse import urlsplit, urlunsplit
import re
import base64


@dataclass
class OutlineKey:
    """
    Describes a key in the Outline server
    """

    key_id: str
    name: str
    password: str
    port: int
    method: str
    access_url: str
    used_bytes: int
    data_limit: typing.Optional[int]


class OutlineServerErrorException(Exception):
    pass


# Таймауты для всех запросов к Outline Management API:
# 3 секунды — установка соединения
# 7 секунд — ожидание ответа после подключения
DEFAULT_TIMEOUT = (3, 7)


class TimeoutSession(requests.Session):
    """
    requests.Session с глобальным timeout по умолчанию.

    Если в конкретном запросе timeout не указан явно,
    автоматически используется DEFAULT_TIMEOUT.
    """

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return super().request(method, url, **kwargs)


class _FingerprintAdapter(requests.adapters.HTTPAdapter):
    """
    This adapter injected into the requests session will check that the
    fingerprint for the certificate matches for every request
    """

    def __init__(self, fingerprint=None, **kwargs):
        self.fingerprint = str(fingerprint)
        super(_FingerprintAdapter, self).__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            assert_fingerprint=self.fingerprint,
        )


class OutlineVPN:
    """
    An Outline VPN connection
    """

    def __init__(self, api_url: str, cert_sha256: str = None):
        # A WSS manager URL fits existing Django Server.Api_outline CharField(128):
        # https://NODE_IP:18443/m/ADMIN_TOKEN#sha256=CERT_SHA256
        # URL fragments are local metadata, never sent over HTTP.
        parsed = urlsplit(api_url)
        fragment = parsed.fragment
        if fragment.startswith("fp="):
            encoded = fragment.removeprefix("fp=")
            try:
                raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            except Exception as exc:
                raise ValueError("Invalid base64 certificate fingerprint") from exc
            if len(raw) != 32:
                raise ValueError("Invalid certificate fingerprint length")
            cert_sha256 = cert_sha256 or raw.hex()
        if fragment.startswith("sha256="):
            url_pin = fragment.removeprefix("sha256=")
            if not re.fullmatch(r"[a-fA-F0-9]{64}", url_pin):
                raise ValueError("Invalid SHA256 certificate fingerprint")
            cert_sha256 = cert_sha256 or url_pin
        self.api_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), parsed.query, ""))

        # Используем Session с глобальными timeout.
        session = TimeoutSession()

        if cert_sha256:
            fingerprint = cert_sha256.replace(":", "").lower()
            if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                raise ValueError("Invalid SHA256 certificate fingerprint")
            if parsed.scheme != "https":
                raise ValueError("Certificate fingerprint requires HTTPS")
            session.mount("https://", _FingerprintAdapter(fingerprint))

        self.session = session

    def get_keys(self):
        """Get all keys in the outline server"""

        response = self.session.get(
            f"{self.api_url}/access-keys/",
            verify=False
        )

        if response.status_code == 200 and "accessKeys" in response.json():
            response_metrics = self.session.get(
                f"{self.api_url}/metrics/transfer",
                verify=False
            )

            if (
                response_metrics.status_code >= 400
                or "bytesTransferredByUserId" not in response_metrics.json()
            ):
                raise OutlineServerErrorException("Unable to get metrics")

            response_json = response.json()
            result = []

            for key in response_json.get("accessKeys"):
                result.append(
                    OutlineKey(
                        key_id=key.get("id"),
                        name=key.get("name"),
                        password=key.get("password"),
                        port=key.get("port"),
                        method=key.get("method"),
                        access_url=key.get("accessUrl"),
                        data_limit=key.get("dataLimit", {}).get("bytes"),
                        used_bytes=response_metrics.json()
                        .get("bytesTransferredByUserId")
                        .get(key.get("id")),
                    )
                )

            return result

        raise OutlineServerErrorException("Unable to retrieve keys")

    def create_key(self, port=636, key_name=None) -> OutlineKey:
        """Create a new key"""

        """генерация ключа с рандомным портом"""
        # port = random.randint(10001, 65535)

        data = {
            "port": port
        }

        response = self.session.post(
            f"{self.api_url}/access-keys/",
            verify=False,
            json=data
        )

        if response.status_code == 201:
            key = response.json()

            outline_key = OutlineKey(
                key_id=key.get("id"),
                name=key.get("name"),
                password=key.get("password"),
                port=key.get("port"),
                method=key.get("method"),
                access_url=key.get("accessUrl"),
                used_bytes=0,
                data_limit=None,
            )

            if key_name and self.rename_key(
                outline_key.key_id,
                key_name
            ):
                outline_key.name = key_name

            return outline_key

        raise OutlineServerErrorException("Unable to create key")

    def delete_key(self, key_id: str) -> bool:
        """Delete a key"""

        response = self.session.delete(
            f"{self.api_url}/access-keys/{key_id}",
            verify=False
        )

        return response.status_code == 204

    def rename_key(self, key_id: str, name: str):
        """Rename a key"""

        files = {
            "name": (None, name),
        }

        response = self.session.put(
            f"{self.api_url}/access-keys/{key_id}/name",
            files=files,
            verify=False
        )

        return response.status_code == 204

    def add_data_limit(
        self,
        key_id: str,
        limit_bytes: int
    ) -> bool:
        """Set data limit for a key (in bytes)"""

        data = {
            "limit": {
                "bytes": limit_bytes
            }
        }

        response = self.session.put(
            f"{self.api_url}/access-keys/{key_id}/data-limit",
            json=data,
            verify=False
        )

        return response.status_code == 204

    def delete_data_limit(self, key_id: str) -> bool:
        """Removes data limit for a key"""

        response = self.session.delete(
            f"{self.api_url}/access-keys/{key_id}/data-limit",
            verify=False
        )

        return response.status_code == 204

    def get_transferred_data(self):
        """
        Gets how much data all keys have used

        {
            "bytesTransferredByUserId": {
                "1": 1008040941,
                "2": 5958113497,
                "3": 752221577
            }
        }
        """

        response = self.session.get(
            f"{self.api_url}/metrics/transfer",
            verify=False
        )

        if (
            response.status_code >= 400
            or "bytesTransferredByUserId" not in response.json()
        ):
            raise OutlineServerErrorException(
                "Unable to get metrics"
            )

        return response.json()

    def api_url_information(self):
        """
        Get information about the server

        {
            "name": "My Server",
            "serverId": "7fda0079-5317-4e5a-bb41-5a431dddae21",
            "metricsEnabled": true,
            "createdTimestampMs": 1536613192052,
            "version": "1.0.0",
            "accessKeyDataLimit": {
                "bytes": 8589934592
            },
            "portForNewAccessKeys": 1234,
            "hostnameForAccessKeys": "example.com"
        }
        """

        response = self.session.get(
            f"{self.api_url}/server",
            verify=False
        )

        if response.status_code != 200:
            raise OutlineServerErrorException(
                "Unable to get information about the server"
            )

        return response.json()

    def set_server_name(self, name: str) -> bool:
        """Renames the server"""

        data = {
            "name": name
        }

        response = self.session.put(
            f"{self.api_url}/name",
            verify=False,
            json=data
        )

        return response.status_code == 204

    def set_hostname(self, hostname: str) -> bool:
        """
        Changes the hostname for access keys.
        Must be a valid hostname or IP address.
        """

        data = {
            "hostname": hostname
        }

        response = self.session.put(
            f"{self.api_url}/server/hostname-for-access-keys",
            verify=False,
            json=data
        )

        return response.status_code == 204

    def get_metrics_status(self) -> bool:
        """Returns whether metrics is being shared"""

        response = self.session.get(
            f"{self.api_url}/metrics/enabled",
            verify=False
        )

        return response.json().get("metricsEnabled")

    def set_metrics_status(self, status: bool) -> bool:
        """Enables or disables sharing of metrics"""

        data = {
            "metricsEnabled": status
        }

        response = self.session.put(
            f"{self.api_url}/metrics/enabled",
            verify=False,
            json=data
        )

        return response.status_code == 204

    def set_port_new_for_access_keys(
        self,
        port: int
    ) -> bool:
        """
        Changes the default port for newly created access keys.
        This can be a port already used for access keys.
        """

        data = {
            "port": port
        }

        response = self.session.put(
            f"{self.api_url}/server/port-for-new-access-keys",
            verify=False,
            json=data
        )

        if response.status_code == 400:
            raise OutlineServerErrorException(
                "The requested port wasn't an integer from "
                "1 through 65535, or the request had no "
                "port parameter."
            )

        elif response.status_code == 409:
            raise OutlineServerErrorException(
                "The requested port was already in use "
                "by another service."
            )

        return response.status_code == 204

    def set_data_limit_for_all_keys(
        self,
        limit_bytes: int
    ) -> bool:
        """Sets a data transfer limit for all access keys."""

        data = {
            "limit": {
                "bytes": limit_bytes
            }
        }

        response = self.session.put(
            f"{self.api_url}/server/access-key-data-limit",
            verify=False,
            json=data
        )

        return response.status_code == 204

    def delete_data_limit_for_all_keys(self) -> bool:
        """
        Removes the access key data limit,
        lifting data transfer restrictions on all access keys.
        """

        response = self.session.delete(
            f"{self.api_url}/server/access-key-data-limit",
            verify=False
        )

        return response.status_code == 204