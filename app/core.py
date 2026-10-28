"""Persistent WSS key manager. No secrets are ever logged."""
from __future__ import annotations

import copy
import os
import signal
import re
import secrets
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml

CIPHER = "chacha20-ietf-poly1305"
METRIC_NAME = "shadowsocks_data_bytes"
# Same transfer directions recommended by Outline for data-limit accounting.
LIMIT_DIRECTIONS = frozenset(("c<p", "p>t"))
SAMPLE_RE = re.compile(r'^shadowsocks_data_bytes\{([^}]*)\}\s+([\d.eE+-]+)(?:\s|$)')
LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


class ManagerError(Exception):
    pass


class Unavailable(ManagerError):
    pass


@dataclass(frozen=True)
class Settings:
    config_file: Path
    base_file: Path
    db_file: Path
    keys_dir: Path
    ws_base_url: str
    config_public_base: str
    ws_path: str
    metrics_url: str = "http://127.0.0.1:9091/metrics"
    service_name: str = "outline-wss"
    poll_seconds: int = 5
    max_age_seconds: int = 20

    @classmethod
    def from_env(cls):
        return cls(
            config_file=Path(os.getenv("OUTLINE_CONFIG", "/opt/outline-wss/config.yaml")),
            base_file=Path(os.getenv("OUTLINE_BASE_CONFIG", "/opt/outline-wss/manager/base.yaml")),
            db_file=Path(os.getenv("OUTLINE_MANAGER_DB", "/opt/outline-wss/manager/state.db")),
            keys_dir=Path(os.getenv("OUTLINE_KEYS_DIR", "/opt/outline-wss/keys")),
            ws_base_url=os.environ["OUTLINE_WS_BASE"].rstrip("/"),
            config_public_base=os.environ["OUTLINE_CONFIG_PUBLIC_BASE"].rstrip("/"),
            ws_path=os.environ["OUTLINE_WS_PATH"].strip("/"),
            metrics_url=os.getenv("OUTLINE_METRICS_URL", "http://127.0.0.1:9091/metrics"),
            poll_seconds=int(os.getenv("OUTLINE_POLL_SECONDS", "5")),
            max_age_seconds=int(os.getenv("OUTLINE_METRICS_MAX_AGE", "20")),
        )

    def validate(self):
        if not self.ws_path or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", self.ws_path):
            raise ValueError("OUTLINE_WS_PATH must match server secret path")
        for url, proto in ((self.ws_base_url, "wss"), (self.config_public_base, "https")):
            parsed = urlsplit(url)
            if parsed.scheme != proto or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(f"Public URL must use {proto} and must not have credentials or query")
        if self.poll_seconds < 1 or self.max_age_seconds < self.poll_seconds:
            raise ValueError("Invalid metric sampling intervals")


def atomic_write(path: Path, data: bytes, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def metric_totals(text: str) -> dict[str, int]:
    """Sum only outbound directions, avoiding double-counting intermediate links."""
    totals = {}
    for line in text.splitlines():
        m = SAMPLE_RE.match(line)
        if not m:
            continue
        labels = {k: v for k, v in LABEL_RE.findall(m.group(1))}
        if labels.get("dir") not in LIMIT_DIRECTIONS:
            continue
        key = labels.get("access_key")
        if not key:
            continue
        value = float(m.group(2))
        if not (0 <= value < float("inf")):
            raise Unavailable("Bad metrics counter")
        totals[key] = totals.get(key, 0) + int(value)
    return totals


class Manager:
    def __init__(self, settings: Settings, *, reloader=None, metrics_fetcher=None):
        settings.validate()
        self.s = settings
        self.lock = threading.RLock()
        self.reloader = reloader or self._systemd_reload
        self.metrics_fetcher = metrics_fetcher or self._fetch_metrics
        self.last_metrics_ok = None
        self.last_metrics_error = None
        self._init_db()
        self._ensure_base()
        self._render_current()  # Recover generated files after a restart.

    def _connect(self):
        c = sqlite3.connect(self.s.db_file, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=15000")
        return c

    def _init_db(self):
        self.s.db_file.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                password TEXT NOT NULL UNIQUE,
                token TEXT NOT NULL UNIQUE,
                data_limit INTEGER,
                bytes_used INTEGER NOT NULL DEFAULT 0,
                last_raw INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK(data_limit IS NULL OR data_limit >= 0),
                CHECK(bytes_used >= 0)
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def _ensure_base(self):
        # Immutable copy of the initial, existing service config, preserving test-user.
        if not self.s.base_file.exists():
            if not self.s.config_file.exists():
                raise ManagerError("Existing outline-ss-server config not found")
            raw = self.s.config_file.read_bytes()
            config = yaml.safe_load(raw)
            self._validate_base(config)
            atomic_write(self.s.base_file, raw)
        base = yaml.safe_load(self.s.base_file.read_text())
        self._validate_base(base)
        # Keep numeric IDs from the existing test/base keys out of our sequence.
        occupied = [int(str(k["id"])) for k in base["services"][0]["keys"]
                    if str(k.get("id", "")).isdigit()]
        with self._connect() as c:
            maximum = max(occupied + [0])
            entry = c.execute("SELECT seq FROM sqlite_sequence WHERE name='keys'").fetchone()
            if maximum > (entry["seq"] if entry else 0):
                c.execute("INSERT OR REPLACE INTO sqlite_sequence(name,seq) VALUES('keys',?)", (maximum,))

    @staticmethod
    def _validate_base(base):
        if not isinstance(base, dict) or not isinstance(base.get("services"), list) or len(base["services"]) != 1:
            raise ManagerError("Expected exactly one service in existing Outline configuration")
        svc = base["services"][0]
        kinds = {item.get("type") for item in svc.get("listeners", [])}
        if not {"websocket-stream", "websocket-packet"}.issubset(kinds):
            raise ManagerError("Existing config must contain websocket-stream and websocket-packet")
        if not isinstance(svc.get("keys"), list):
            raise ManagerError("Invalid keys in base config")

    def _rows(self, c):
        return c.execute("SELECT * FROM keys ORDER BY id").fetchall()

    def _enabled(self, row, *, metrics_fresh=True):
        if row["data_limit"] is not None and row["bytes_used"] >= row["data_limit"]:
            return False
        # Fail closed for quota-limited keys if metrics not fresh.
        if row["data_limit"] is not None and not metrics_fresh:
            return False
        return True

    def _metrics_fresh(self):
        if self.last_metrics_ok is None:
            return False
        return time.monotonic() - self.last_metrics_ok < self.s.max_age_seconds

    def _config(self, rows, metrics_fresh=None):
        cfg = copy.deepcopy(yaml.safe_load(self.s.base_file.read_text()))
        original_ids = {str(k.get("id")) for k in cfg["services"][0]["keys"]}
        if metrics_fresh is None:
            metrics_fresh = self._metrics_fresh()
        for row in rows:
            if str(row["id"]) in original_ids:
                raise ManagerError("Managed ID conflicts with base access key")
            if self._enabled(row, metrics_fresh=metrics_fresh):
                cfg["services"][0]["keys"].append({
                    "id": str(row["id"]), "cipher": CIPHER, "secret": row["password"]
                })
        return yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True).encode()

    def _key_yaml(self, row):
        base = f"{self.s.ws_base_url}/{self.s.ws_path}"
        obj = {
            "transport": {
                "$type": "tcpudp",
                "tcp": {"$type": "shadowsocks", "endpoint": {"$type": "websocket", "url": f"{base}/tcp"}, "cipher": CIPHER, "secret": row["password"]},
                "udp": {"$type": "shadowsocks", "endpoint": {"$type": "websocket", "url": f"{base}/udp"}, "cipher": CIPHER, "secret": row["password"]},
            }
        }
        return yaml.safe_dump(obj, sort_keys=False).encode()

    def _file(self, row):
        return self.s.keys_dir / f"{row['token']}.yml"

    def _update_files(self, c):
        rows = self._rows(c)
        atomic_write(self.s.config_file, self._config(rows))
        self.s.keys_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            atomic_write(self._file(row), self._key_yaml(row))
        # Record managed filenames. Do not delete old files until transaction commits.
        current = {r["token"] for r in rows}
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('known_tokens',?)", (','.join(sorted(current)),))

    def _known_tokens(self, c):
        entry = c.execute("SELECT value FROM meta WHERE key='known_tokens'").fetchone()
        return entry["value"].split(',') if entry and entry["value"] else []

    def _render_current(self):
        with self.lock, self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = set(self._known_tokens(c))
            self._update_files(c)
            c.commit()
            current = {r["token"] for r in self._rows(c)}
            for token in previous - current:
                (self.s.keys_dir / f"{token}.yml").unlink(missing_ok=True)

    def _systemd_reload(self):
        if os.getenv("OUTLINE_RELOAD_MODE") == "pidfile":
            pid_file = Path(os.getenv("OUTLINE_PID_FILE", "/run/outline-wss/outline.pid"))
            try:
                pid = int(pid_file.read_text().strip())
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                if b"outline-ss-server" not in cmdline:
                    raise Unavailable("Outline PID mismatch")
                os.kill(pid, signal.SIGHUP)
            except (OSError, ValueError) as exc:
                raise Unavailable("Could not signal Outline process") from exc
            return
        result = subprocess.run(
            ["systemctl", "kill", "--kill-whom=main", "--signal=HUP", self.s.service_name],
            check=False, capture_output=True, text=True, timeout=5,
        )
        if result.returncode:
            raise Unavailable("Could not reload outline-wss service")

    def _fetch_metrics(self):
        r = requests.get(self.s.metrics_url, timeout=(2, 3), allow_redirects=False)
        r.raise_for_status()
        if "shadowsocks_" not in r.text:
            raise Unavailable("Not an Outline Prometheus metrics endpoint")
        # An unused key may have no shadowsocks_data_bytes series yet.
        return metric_totals(r.text)

    def _change(self, modifier, *, read_metrics=True):
        with self.lock:
            if read_metrics:
                self._sample_locked()
                if not self._metrics_fresh():
                    raise Unavailable("Traffic metrics unavailable: refusing state change")
            with self._connect() as c:
                c.execute("BEGIN IMMEDIATE")
                prev = self.s.config_file.read_bytes()
                previous_tokens = {r["token"] for r in self._rows(c)}
                try:
                    result = modifier(c)
                    self._update_files(c)
                    if self.s.config_file.read_bytes() != prev:
                        self.reloader()
                    c.commit()
                except Exception:
                    c.rollback()
                    atomic_write(self.s.config_file, prev)
                    try:
                        self.reloader()
                    except Exception:
                        pass
                    # Restore generated YAML from committed DB state.
                    self._render_current()
                    raise
                # Remove orphaned files after commit (not before)
                for old in previous_tokens - {r["token"] for r in self._rows(c)}:
                    (self.s.keys_dir / f"{old}.yml").unlink(missing_ok=True)
                return result

    def _sample_locked(self):
        """Account for counter resets and enforce quotas. Called by poller and mutations."""
        try:
            raw = self.metrics_fetcher()
        except Exception as exc:
            self.last_metrics_error = str(exc)
            # Fail closed for limited keys, leave unlimited keys usable.
            self.last_metrics_ok = None
            with self._connect() as c:
                rows = self._rows(c)
                if any(r["data_limit"] is not None for r in rows):
                    new_config = self._config(rows, metrics_fresh=False)
                    if new_config != self.s.config_file.read_bytes():
                        atomic_write(self.s.config_file, new_config)
                        self.reloader()
            return False
        was_fresh = self._metrics_fresh()
        self.last_metrics_ok = time.monotonic()
        self.last_metrics_error = None
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = self._rows(c)
            before_enabled = {r["id"] for r in rows if self._enabled(r, metrics_fresh=was_fresh)}
            changed = False
            for r in rows:
                new_raw = raw.get(str(r["id"]))
                if new_raw is None:
                    # Missing series must not be mistaken for a counter reset.
                    continue
                old_raw = r["last_raw"]
                if old_raw is None:
                    # New key starts from zero. In case of bootstrap, retain raw count.
                    delta = new_raw
                elif new_raw < old_raw:
                    # Counter reset (reload/restart); keep durable accumulated usage.
                    delta = new_raw
                else:
                    delta = new_raw - old_raw
                if delta or old_raw != new_raw:
                    c.execute("UPDATE keys SET bytes_used=bytes_used+?, last_raw=? WHERE id=?", (delta, new_raw, r["id"]))
                    changed = True
            updated = self._rows(c)
            after_enabled = {r["id"] for r in updated if self._enabled(r, metrics_fresh=True)}
            if before_enabled != after_enabled or not was_fresh:
                new_config = self._config(updated, metrics_fresh=True)
                if new_config != self.s.config_file.read_bytes():
                    atomic_write(self.s.config_file, new_config)
                    self.reloader()
            c.commit()
        return changed

    def poll(self):
        with self.lock:
            return self._sample_locked()

    def key_url(self, row):
        return f"ssconf://{self.s.config_public_base.removeprefix('https://')}/{row['token']}.yml#Outline-WSS"

    def _info(self, row):
        return {
            "id": str(row["id"]), "name": row["name"], "password": row["password"],
            "port": int(os.getenv("OUTLINE_WS_PORT", "636")), "method": CIPHER, "accessUrl": self.key_url(row),
            **({"dataLimit": {"bytes": row["data_limit"]}} if row["data_limit"] is not None else {}),
        }

    def create(self, name=None, port=636):
        if port != int(os.getenv("OUTLINE_WS_PORT", "636")):
            raise ValueError("This WSS node only supports its configured shared internal port")
        def do(c):
            password = secrets.token_urlsafe(32)
            token = secrets.token_urlsafe(24)
            c.execute("INSERT INTO keys(name,password,token) VALUES (?,?,?)", (name or "New key", password, token))
            row = c.execute("SELECT * FROM keys WHERE id=last_insert_rowid()").fetchone()
            if name is None:
                c.execute("UPDATE keys SET name=? WHERE id=?", (str(row["id"]), row["id"]))
                row = c.execute("SELECT * FROM keys WHERE id=?", (row["id"],)).fetchone()
            return self._info(row)
        return self._change(do)

    @staticmethod
    def _int_id(key_id):
        if not re.fullmatch(r"[1-9][0-9]{0,15}", str(key_id)):
            raise KeyError(key_id)
        return int(key_id)

    def get(self, key_id):
        with self._connect() as c:
            row = c.execute("SELECT * FROM keys WHERE id=?", (self._int_id(key_id),)).fetchone()
        if row is None:
            raise KeyError(key_id)
        return self._info(row)

    def list(self):
        with self._connect() as c:
            return [self._info(r) for r in self._rows(c)]

    def _update(self, key_id, sql, value=None):
        id_ = self._int_id(key_id)
        def do(c):
            if not c.execute("SELECT 1 FROM keys WHERE id=?", (id_,)).fetchone():
                raise KeyError(key_id)
            if value is None:
                c.execute(sql, (id_,))
            else:
                c.execute(sql, (value, id_))
            return True
        return self._change(do)

    def rename(self, key_id, name):
        if not name or len(name) > 64:
            raise ValueError("name must be between 1 and 64 characters")
        return self._update(key_id, "UPDATE keys SET name=? WHERE id=?", name)

    def set_limit(self, key_id, size):
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("limit must be nonnegative integer")
        return self._update(key_id, "UPDATE keys SET data_limit=? WHERE id=?", size)

    def remove_limit(self, key_id):
        id_ = self._int_id(key_id)
        def do(c):
            if not c.execute("SELECT 1 FROM keys WHERE id=?", (id_,)).fetchone():
                raise KeyError(key_id)
            c.execute("UPDATE keys SET data_limit=NULL WHERE id=?", (id_,))
            return True
        return self._change(do)

    def bulk_limit(self, size=None):
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise ValueError("limit must be nonnegative integer")
        def do(c):
            c.execute("UPDATE keys SET data_limit=?", (size,))
            return True
        return self._change(do)

    def delete(self, key_id):
        return self._update(key_id, "DELETE FROM keys WHERE id=?")

    def metrics(self):
        with self._connect() as c:
            return {str(r["id"]): r["bytes_used"] for r in self._rows(c)}

    def healthy(self):
        return {"status": "ok", "metricsFresh": self._metrics_fresh(), "metricsError": bool(self.last_metrics_error)}
