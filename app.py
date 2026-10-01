import csv
import ctypes
import hashlib
import http.client
import json
import os
import ssl
import sys
import threading
import time
import tkinter as tk
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox
from typing import Any
from urllib import error, request

import pystray
from PIL import Image as PILImage
from PIL import ImageDraw
from pystray import MenuItem as TrayItem

APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else APP_DIR
APPDATA_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'UDM WAN Speed Monitor'
APPDATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = APPDATA_DIR / 'config.json'
LEGACY_APPDATA_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'WesternMGs' / 'UDM WAN Monitor'
LEGACY_CONFIG_PATHS = [
    APP_DIR / 'config.json',
    RUNTIME_DIR / 'config.json',
    LEGACY_APPDATA_DIR / 'config.json',
]
ICON_CANDIDATES = [
    RUNTIME_DIR / 'udm-wan-speed-monitor-v2.ico',
    APP_DIR / 'udm-wan-speed-monitor-v2.ico',
    RUNTIME_DIR / 'udm-wan-speed-monitor.ico',
    APP_DIR / 'udm-wan-speed-monitor.ico',
]
HISTORY_WINDOW_SECONDS = 300
DEFAULT_POLL_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_CONSECUTIVE_ERRORS = 3
LOGOUT_TIMEOUT_SECONDS = 1.5
EVENT_LOG_PATH = APPDATA_DIR / 'ereignisse.csv'
GATEWAY_DEVICE_TYPES = {'udm', 'ugw', 'uxg', 'ucg'}
TOP_CLIENTS_EVERY_N_POLLS = 3
TOP_CLIENTS_COUNT = 3
BUFFERBLOAT_ALERT_MS = 30.0
MIN_WIDTH = 220
MIN_HEIGHT = 120
BASE_WIDTH = 1180
BASE_HEIGHT = 320
NEON_GREEN = '#39ff14'
NEON_BLUE = '#00e5ff'
BG = '#05070c'
CARD = '#0f1724'
CANVAS_BG = '#09111a'

DEFAULT_CONFIG: dict[str, Any] = {
    'host': '',
    'username': '',
    'password_encrypted': '',
    'remember_password': True,
    'options_visible': False,
    'always_on_top': False,
    'show_wan2': False,
    'autostart': False,
    'minimize_to_tray': True,
    'window_width': BASE_WIDTH,
    'window_height': BASE_HEIGHT,
    'window_x': 120,
    'window_y': 120,
    'cert_host': '',
    'cert_fingerprint': '',
    'show_top_clients': True,
}

STARTUP_DIR = Path(os.environ.get('APPDATA', str(Path.home()))) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
AUTOSTART_BAT_PATH = STARTUP_DIR / 'UDM WAN Speed Monitor.bat'


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


crypt32 = ctypes.windll.crypt32
kernel32 = ctypes.windll.kernel32
CRYPTPROTECT_UI_FORBIDDEN = 0x01


@dataclass(slots=True)
class WanReading:
    name: str
    download_bps: float
    upload_bps: float
    source: str
    timestamp: float
    ping_ms: str = '--'
    online: bool | None = None


@dataclass(slots=True)
class WanSnapshot:
    readings: list[WanReading] = field(default_factory=list)
    # None heisst: in dieser Runde nicht abgefragt, die letzte Anzeige bleibt stehen.
    top_clients: list[tuple[str, float]] | None = None


class UdmApiError(RuntimeError):
    pass


class AuthenticationError(UdmApiError):
    """Login abgelehnt: weitere Versuche wuerden nur das Konto sperren."""


class CertificateMismatchError(UdmApiError):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__('Das TLS-Zertifikat der UDM hat sich geaendert.')
        self.expected = expected
        self.actual = actual


def format_rate(bps: float) -> str:
    units = ['bit/s', 'Kbit/s', 'Mbit/s', 'Gbit/s']
    value = float(bps)
    index = 0
    while value >= 1000 and index < len(units) - 1:
        value /= 1000.0
        index += 1
    return f'{value:.2f} {units[index]}'


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours} h {minutes} min'
    if minutes:
        return f'{minutes} min {secs} s'
    return f'{secs} s'


def fingerprint_label(fingerprint: str) -> str:
    return ':'.join(fingerprint[i:i + 2] for i in range(0, min(len(fingerprint), 32), 2)).upper() + '...'


def ping_to_float(ping_ms: str | None) -> float | None:
    try:
        return float(str(ping_ms).split()[0])
    except (ValueError, IndexError):
        return None


def bufferbloat_grade(extra_ms: float) -> str:
    """Bewertung der Zusatzlatenz unter Last, angelehnt an gaengige Bufferbloat-Tests."""
    if extra_ms < 5:
        return 'A+'
    if extra_ms < 30:
        return 'A'
    if extra_ms < 60:
        return 'B'
    if extra_ms < 200:
        return 'C'
    return 'D'


class BufferbloatTracker:
    """Vergleicht den Ping bei ruhiger Leitung mit dem Ping unter Last.

    Steigt die Latenz unter Last deutlich an, puffert der Router oder das Modem zu viel (Bufferbloat).
    Videocalls und Spiele ruckeln dann, sobald jemand etwas herunterlaedt. Smart Queues auf der UDM helfen dagegen.
    """

    IDLE_LOAD = 0.2
    BUSY_LOAD = 0.6
    MIN_BUSY_BPS = 5_000_000.0
    MIN_IDLE_SAMPLES = 5
    MIN_BUSY_SAMPLES = 3

    def __init__(self, window_seconds: float = HISTORY_WINDOW_SECONDS) -> None:
        self.window_seconds = window_seconds
        self.samples: deque[tuple[float, float, float]] = deque()

    def add(self, timestamp: float, ping_ms: float | None, throughput_bps: float) -> None:
        if ping_ms is None:
            return
        self.samples.append((timestamp, ping_ms, throughput_bps))
        while self.samples and timestamp - self.samples[0][0] > self.window_seconds:
            self.samples.popleft()

    def extra_latency_ms(self) -> float | None:
        if not self.samples:
            return None
        peak = max(sample[2] for sample in self.samples)
        if peak < self.MIN_BUSY_BPS:
            return None
        idle = sorted(ping for _, ping, load in self.samples if load <= peak * self.IDLE_LOAD)
        busy = sorted(ping for _, ping, load in self.samples if load >= peak * self.BUSY_LOAD and load >= self.MIN_BUSY_BPS)
        if len(idle) < self.MIN_IDLE_SAMPLES or len(busy) < self.MIN_BUSY_SAMPLES:
            return None
        return max(0.0, busy[len(busy) // 2] - idle[len(idle) // 2])


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Prueft den Zertifikats-Fingerprint direkt nach dem TLS-Handshake, bevor Daten (z.B. das Passwort) gesendet werden."""

    def __init__(self, *args, verify_fingerprint=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._verify_fingerprint = verify_fingerprint

    def connect(self) -> None:
        super().connect()
        if self._verify_fingerprint is None:
            return
        der = self.sock.getpeercert(binary_form=True) or b''
        try:
            self._verify_fingerprint(hashlib.sha256(der).hexdigest())
        except Exception:
            self.close()
            raise


class PinnedHTTPSHandler(request.HTTPSHandler):
    """UDMs nutzen selbstsignierte Zertifikate. Statt TLS komplett blind zu vertrauen, wird das Zertifikat beim ersten Kontakt gemerkt (Trust on First Use)."""

    def __init__(self, verify_fingerprint) -> None:
        super().__init__(context=ssl._create_unverified_context())
        self._verify_fingerprint = verify_fingerprint

    def https_open(self, req):
        def factory(host, **kwargs):
            return PinnedHTTPSConnection(host, verify_fingerprint=self._verify_fingerprint, **kwargs)
        return self.do_open(factory, req, context=self._context)


class EventLog:
    """Schreibt Ausfaelle und Wiederherstellungen als CSV (Excel-freundlich mit Semikolon)."""

    HEADER = ['Zeitpunkt', 'Ereignis', 'Leitung', 'Details']

    def __init__(self, path: Path = EVENT_LOG_PATH) -> None:
        self.path = path

    def write(self, event: str, wan: str, detail: str = '') -> None:
        try:
            new_file = not self.path.exists()
            with self.path.open('a', encoding='utf-8-sig' if new_file else 'utf-8', newline='') as handle:
                writer = csv.writer(handle, delimiter=';')
                if new_file:
                    writer.writerow(self.HEADER)
                writer.writerow([time.strftime('%Y-%m-%d %H:%M:%S'), event, wan, detail])
        except OSError:
            pass


class ConfigStore:
    @staticmethod
    def _migrate_legacy_config() -> None:
        if CONFIG_PATH.exists():
            return
        for legacy_path in LEGACY_CONFIG_PATHS:
            if legacy_path.exists() and legacy_path != CONFIG_PATH:
                try:
                    CONFIG_PATH.write_text(legacy_path.read_text(encoding='utf-8'), encoding='utf-8')
                    return
                except Exception:
                    pass

    @staticmethod
    def load() -> dict[str, Any]:
        ConfigStore._migrate_legacy_config()
        if not CONFIG_PATH.exists():
            config = DEFAULT_CONFIG.copy()
            config['password'] = ''
            return config
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        except Exception:
            loaded = {}
        config = {**DEFAULT_CONFIG, **loaded}
        encrypted = config.get('password_encrypted', '')
        config['password'] = ConfigStore._decrypt_password(encrypted) if encrypted else ''
        return config

    @staticmethod
    def save(config: dict[str, Any]) -> None:
        data = {
            'host': config.get('host', '').strip(),
            'username': config.get('username', '').strip(),
            'remember_password': bool(config.get('remember_password', True)),
            'options_visible': bool(config.get('options_visible', False)),
            'always_on_top': bool(config.get('always_on_top', False)),
            'show_wan2': bool(config.get('show_wan2', False)),
            'autostart': bool(config.get('autostart', False)),
            'minimize_to_tray': bool(config.get('minimize_to_tray', True)),
            'window_width': int(config.get('window_width', BASE_WIDTH)),
            'window_height': int(config.get('window_height', BASE_HEIGHT)),
            'window_x': int(config.get('window_x', 120)),
            'window_y': int(config.get('window_y', 120)),
            'cert_host': str(config.get('cert_host', '')),
            'cert_fingerprint': str(config.get('cert_fingerprint', '')),
            'show_top_clients': bool(config.get('show_top_clients', True)),
            'password_encrypted': '',
        }
        if data['remember_password'] and config.get('password'):
            data['password_encrypted'] = ConfigStore._encrypt_password(config['password'])
        # Erst in eine Temp-Datei schreiben, damit ein Absturz beim Speichern die Konfiguration nicht zerstoert.
        temp_path = CONFIG_PATH.with_suffix('.tmp')
        temp_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        os.replace(temp_path, CONFIG_PATH)

    @staticmethod
    def _encrypt_password(password: str) -> str:
        raw = password.encode('utf-8')
        in_buffer = ctypes.create_string_buffer(raw, len(raw))
        in_blob = DATA_BLOB(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
        out_blob = DATA_BLOB()
        if not crypt32.CryptProtectData(ctypes.byref(in_blob), 'UDM WAN Speed Monitor', None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData).hex()
        finally:
            kernel32.LocalFree(out_blob.pbData)

    @staticmethod
    def _decrypt_password(value: str) -> str:
        try:
            encrypted = bytes.fromhex(value)
        except ValueError:
            return ''
        in_buffer = ctypes.create_string_buffer(encrypted, len(encrypted))
        in_blob = DATA_BLOB(len(encrypted), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
        out_blob = DATA_BLOB()
        if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)):
            return ''
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode('utf-8')
        finally:
            kernel32.LocalFree(out_blob.pbData)


class UdmClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.csrf_token = ''
        self.logged_in = False
        self.gateway_mac = ''
        self.poll_count = 0
        self.observed_fingerprint = ''
        host = config.get('host', '')
        self.pinned_fingerprint = config.get('cert_fingerprint', '') if config.get('cert_host') == host else ''
        handlers: list[Any] = [request.HTTPCookieProcessor(), PinnedHTTPSHandler(self._check_fingerprint)]
        self.opener = request.build_opener(*handlers)

    def _check_fingerprint(self, fingerprint: str) -> None:
        self.observed_fingerprint = fingerprint
        if self.pinned_fingerprint and fingerprint != self.pinned_fingerprint:
            raise CertificateMismatchError(self.pinned_fingerprint, fingerprint)

    def login(self) -> None:
        payload = json.dumps({'username': self.config['username'], 'password': self.config['password']}).encode('utf-8')
        response = self._request('/api/auth/login', data=payload, headers={'Content-Type': 'application/json'})
        self.csrf_token = response.headers.get('X-CSRF-Token', '')
        response.read()
        self.logged_in = True

    def logout(self) -> None:
        if not self.logged_in:
            return
        try:
            response = self._request('/api/auth/logout', data=b'{}', headers={'Content-Type': 'application/json'})
            response.read()
        except Exception:
            pass
        self.logged_in = False
        self.csrf_token = ''

    def fetch_wan_snapshot(self) -> WanSnapshot:
        if not self.logged_in:
            self.login()
        readings: list[WanReading] = []
        readings.extend(self._parse_health(self._get_json('/proxy/network/api/s/default/stat/health')))
        readings.extend(self._parse_devices(self._fetch_gateway_devices()))
        readings = self._dedupe_readings(readings)
        if not readings:
            # sysinfo liefert nur selten WAN-Daten, deshalb nur als Rueckfall abfragen.
            readings = self._parse_sysinfo(self._get_json('/proxy/network/api/s/default/stat/sysinfo'))
        if not readings:
            raise UdmApiError('Keine WAN-Durchsatzdaten gefunden.')
        top_clients = None
        if self.config.get('show_top_clients', True) and self.poll_count % TOP_CLIENTS_EVERY_N_POLLS == 0:
            top_clients = self.fetch_top_clients()
        self.poll_count += 1
        return WanSnapshot(readings=self._normalize_wan_names(readings), top_clients=top_clients)

    def fetch_top_clients(self, limit: int = TOP_CLIENTS_COUNT) -> list[tuple[str, float]] | None:
        """Die Geraete mit dem hoechsten aktuellen Durchsatz (Down + Up). Fehler hier stoppen das WAN-Monitoring nicht."""
        try:
            payload = self._get_json('/proxy/network/api/s/default/stat/sta')
        except AuthenticationError:
            raise
        except UdmApiError:
            return None
        clients: list[tuple[str, float]] = []
        for item in payload.get('data', []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            # Kabelgebundene Clients melden ihre Raten mit wired-Praefix, WLAN-Clients ohne.
            wired_keys = ('wired-rx_bytes-r', 'wired-tx_bytes-r')
            keys = wired_keys if any(key in item for key in wired_keys) else ('rx_bytes-r', 'tx_bytes-r')
            rate = 0.0
            for key in keys:
                value = item.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    rate += float(value) * 8.0
            if rate <= 0:
                continue
            name = str(item.get('name') or item.get('hostname') or item.get('ip') or item.get('mac') or '?').strip()
            clients.append((name, rate))
        clients.sort(key=lambda entry: entry[1], reverse=True)
        return clients[:limit]

    def _fetch_gateway_devices(self) -> Any:
        """stat/device liefert alle Access Points und Switches. Nach dem ersten Abruf wird nur noch das Gateway geholt."""
        if self.gateway_mac:
            try:
                return self._get_json(f'/proxy/network/api/s/default/stat/device/{self.gateway_mac}')
            except AuthenticationError:
                raise
            except UdmApiError:
                if not self.logged_in:
                    raise
                self.gateway_mac = ''
        payload = self._get_json('/proxy/network/api/s/default/stat/device')
        if not isinstance(payload, dict):
            return payload
        devices = payload.get('data', [])
        gateways = [item for item in devices if isinstance(item, dict) and self._is_gateway(item)]
        if len(gateways) == 1 and gateways[0].get('mac'):
            self.gateway_mac = str(gateways[0]['mac']).lower()
        return {**payload, 'data': gateways} if gateways else payload

    @staticmethod
    def _is_gateway(item: dict[str, Any]) -> bool:
        return str(item.get('type', '')).lower() in GATEWAY_DEVICE_TYPES or 'wan1' in item

    def _get_json(self, path: str) -> Any:
        response = self._request(path)
        try:
            return json.loads(response.read().decode('utf-8'))
        except json.JSONDecodeError as exc:
            raise UdmApiError(f'Antwort von {path} war kein JSON.') from exc

    def _request(self, path: str, data: bytes | None = None, headers: dict[str, str] | None = None):
        url = self.config['host'].rstrip('/') + path
        req_headers = {'Accept': 'application/json'}
        if self.csrf_token:
            req_headers['X-CSRF-Token'] = self.csrf_token
        if headers:
            req_headers.update(headers)
        req = request.Request(url, data=data, headers=req_headers, method='POST' if data is not None else 'GET')
        try:
            return self.opener.open(req, timeout=REQUEST_TIMEOUT_SECONDS)
        except error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore')
            if path == '/api/auth/login' and exc.code in (400, 401, 403):
                raise AuthenticationError('Anmeldung abgelehnt. Bitte Benutzername und Passwort pruefen.') from exc
            if exc.code == 401:
                self.logged_in = False
            raise UdmApiError(f'HTTP {exc.code} bei {path}: {body[:160]}') from exc
        except error.URLError as exc:
            raise UdmApiError(f'Verbindung zu {url} fehlgeschlagen: {exc.reason}') from exc

    def _parse_health(self, payload: Any) -> list[WanReading]:
        readings: list[WanReading] = []
        for item in payload.get('data', []) if isinstance(payload, dict) else []:
            if str(item.get('subsystem', '')).lower() != 'wan':
                continue
            nested = self._extract_readings(item, 'stat/health')
            if nested:
                readings.extend(nested)
                continue
            reading = self._reading_from_object(item, 'stat/health', self._wan_name(item, len(readings) + 1))
            if reading is not None:
                readings.append(reading)
        return self._dedupe_readings(readings)

    def _parse_devices(self, payload: Any) -> list[WanReading]:
        readings: list[WanReading] = []
        for item in payload.get('data', []) if isinstance(payload, dict) else []:
            readings.extend(self._extract_readings(item, 'stat/device'))
        return self._dedupe_readings(readings)

    def _parse_sysinfo(self, payload: Any) -> list[WanReading]:
        readings: list[WanReading] = []
        for item in payload.get('data', []) if isinstance(payload, dict) else []:
            readings.extend(self._extract_readings(item, 'stat/sysinfo'))
        return self._dedupe_readings(readings)

    def _extract_readings(self, value: Any, source: str, path: tuple[str, ...] = ()) -> list[WanReading]:
        readings: list[WanReading] = []
        if isinstance(value, dict):
            current_label = self._wan_label_from_context(value, path)
            reading = self._reading_from_object(value, source, current_label) if current_label else None
            if reading is not None:
                readings.append(reading)
            for key, child in value.items():
                child_path = path + (str(key),)
                readings.extend(self._extract_readings(child, source, child_path))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                readings.extend(self._extract_readings(child, source, path + (str(index),)))
        return readings

    def _wan_label_from_context(self, item: dict[str, Any], path: tuple[str, ...]) -> str | None:
        direct_candidates = [
            item.get('wan_name'), item.get('name'), item.get('display_name'), item.get('ifname'), item.get('interface'),
            item.get('port_name'), item.get('network'), item.get('role'), item.get('target'), item.get('key'),
            item.get('uplink'), item.get('uplink_name'), item.get('link_name'), item.get('wan_role'), item.get('type'),
        ]
        for value in direct_candidates:
            label = str(value).strip().lower() if value is not None else ''
            compact = label.replace('_', '').replace(' ', '').replace('-', '')
            if compact in {'wan1', 'internet1', 'uplink1'}:
                return 'WAN 1'
            if compact in {'wan2', 'internet2', 'uplink2'}:
                return 'WAN 2'
        if path:
            tail = str(path[-1]).strip().lower().replace('_', '').replace(' ', '').replace('-', '')
            if tail in {'wan1', 'internet1', 'uplink1'}:
                return 'WAN 1'
            if tail in {'wan2', 'internet2', 'uplink2'}:
                return 'WAN 2'
        return None

    def _reading_from_object(self, item: dict[str, Any], source: str, name: str | None) -> WanReading | None:
        download = self._extract_rate(item, 'download')
        upload = self._extract_rate(item, 'upload')
        ping_ms = self._extract_ping_ms(item)
        online = self._extract_online(item)
        if download is None or upload is None:
            if name is None or (ping_ms in {'', '--'} and online is None):
                return None
            return WanReading(name, float(download or 0.0), float(upload or 0.0), source, time.time(), ping_ms, online)
        return WanReading(name, download, upload, source, time.time(), ping_ms, online)

    @staticmethod
    def _extract_online(item: dict[str, Any]) -> bool | None:
        up = item.get('up')
        if isinstance(up, bool):
            return up
        status = item.get('status')
        if isinstance(status, str) and status:
            return status.strip().lower() in {'ok', 'online', 'connected', 'up'}
        return None

    def _wan_name(self, item: dict[str, Any], index: int) -> str:
        return self._wan_label_from_context(item, ()) or f'WAN {index}'

    def _merge_readings(self, preferred: WanReading, other: WanReading) -> WanReading:
        ping_ms = preferred.ping_ms if preferred.ping_ms not in {'', '--'} else other.ping_ms
        if ping_ms in {'', '--'}:
            ping_ms = '--'
        online = preferred.online if preferred.online is not None else other.online
        return WanReading(preferred.name, preferred.download_bps, preferred.upload_bps, preferred.source, preferred.timestamp, ping_ms, online)

    def _dedupe_readings(self, readings: list[WanReading]) -> list[WanReading]:
        deduped: dict[str, WanReading] = {}
        for reading in readings:
            current = deduped.get(reading.name)
            if current is None:
                deduped[reading.name] = reading
                continue
            if (reading.download_bps + reading.upload_bps) > (current.download_bps + current.upload_bps):
                deduped[reading.name] = self._merge_readings(reading, current)
            else:
                deduped[reading.name] = self._merge_readings(current, reading)
        return list(deduped.values())

    def _normalize_wan_names(self, readings: list[WanReading]) -> list[WanReading]:
        explicit: dict[str, WanReading] = {}
        fallback: list[WanReading] = []
        for reading in readings:
            if reading.name in {'WAN 1', 'WAN 2'}:
                current = explicit.get(reading.name)
                if current is None:
                    explicit[reading.name] = reading
                elif (reading.download_bps + reading.upload_bps) > (current.download_bps + current.upload_bps):
                    explicit[reading.name] = self._merge_readings(reading, current)
                else:
                    explicit[reading.name] = self._merge_readings(current, reading)
            else:
                fallback.append(reading)

        normalized: list[WanReading] = []
        for index in (1, 2):
            label = f'WAN {index}'
            reading = explicit.get(label)
            if reading is None and not explicit and fallback:
                reading = fallback.pop(0)
            if reading is None:
                continue
            normalized.append(WanReading(label, reading.download_bps, reading.upload_bps, reading.source, reading.timestamp, reading.ping_ms, reading.online))
        return normalized

    def _extract_ping_ms(self, item: dict[str, Any]) -> str:
        candidates = [
            item.get('latency'), item.get('latency_ms'), item.get('avg_latency'), item.get('average_latency'),
            item.get('ping'), item.get('ping_ms'), item.get('avg_ping'), item.get('rtt'), item.get('rtt_ms'),
            item.get('response_time'), item.get('response_time_ms'), item.get('wan_latency'), item.get('uplink_latency'),
        ]
        best_value = None
        for value in candidates:
            parsed = self._parse_ping_value(value)
            if parsed is not None:
                best_value = parsed
                break
        if best_value is None:
            for key, value in item.items():
                lowered = str(key).lower()
                if any(token in lowered for token in ('latency', 'ping', 'rtt', 'response_time')):
                    parsed = self._parse_ping_value(value)
                    if parsed is not None:
                        best_value = parsed
                        break
        return self._format_ping(best_value)

    def _parse_ping_value(self, value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value) if value >= 0 else None
        if isinstance(value, str):
            cleaned = value.strip().lower().replace('ms', '').strip()
            try:
                parsed = float(cleaned)
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
        if isinstance(value, dict):
            for key in ('avg', 'average', 'value', 'latency', 'ping', 'rtt', 'ms'):
                if key in value:
                    parsed = self._parse_ping_value(value.get(key))
                    if parsed is not None:
                        return parsed
        return None

    def _format_ping(self, value: float | None) -> str:
        if value is None:
            return '--'
        if value >= 100:
            return f'{value:.0f} ms'
        if value >= 10:
            return f'{value:.1f} ms'
        return f'{value:.2f} ms'

    NON_RATE_KEY_PARTS = ('xput', 'uptime', 'packets', 'errors', 'dropped', 'retries', 'speedtest', 'max_speed', 'uplink', 'update', 'group', 'multicast', 'broadcast', 'duplex')

    def _extract_rate(self, item: dict[str, Any], direction: str) -> float | None:
        # xput_* sind Speedtest-Ergebnisse in Mbit/s, keine Live-Werte, und bleiben deshalb aussen vor.
        aliases = {
            'download': ['download_bps', 'speed_down', 'rx_rate', 'rx_bytes-r', 'wan_rx_bps', 'throughput_down'],
            'upload': ['upload_bps', 'speed_up', 'tx_rate', 'tx_bytes-r', 'wan_tx_bps', 'throughput_up'],
        }
        for key in aliases[direction]:
            value = self._to_bps(item.get(key), key)
            if value is not None:
                return value
        for key, raw in item.items():
            lowered = str(key).lower()
            if any(part in lowered for part in self.NON_RATE_KEY_PARTS):
                continue
            if direction == 'download' and any(part in lowered for part in ['download', 'down', 'rx']):
                value = self._to_bps(raw, lowered)
                if value is not None:
                    return value
            if direction == 'upload' and any(part in lowered for part in ['upload', 'up', 'tx']):
                value = self._to_bps(raw, lowered)
                if value is not None:
                    return value
        return None

    def _to_bps(self, value: Any, key: str) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                return None
        if not isinstance(value, (int, float)) or value < 0:
            return None
        lowered = key.lower()
        if 'bytes-r' in lowered:
            return float(value) * 8.0
        if 'kbps' in lowered:
            return float(value) * 1000.0
        if 'mbps' in lowered:
            return float(value) * 1000000.0
        if lowered.endswith('_bytes'):
            return None
        return float(value)


class GraphPanel(tk.Frame):
    def __init__(self, parent: tk.Widget, title: str, accent: str, value_var: tk.StringVar) -> None:
        super().__init__(parent, bg=CARD, highlightbackground=accent, highlightthickness=1)
        self.accent = accent
        self.compact_mode = False
        max_points = max(16, int((HISTORY_WINDOW_SECONDS / DEFAULT_POLL_SECONDS) + 8))
        self.history: deque[tuple[float, float]] = deque(maxlen=max_points)
        self.title_label = tk.Label(self, text=title, fg=accent, bg=CARD)
        self.title_label.pack(anchor='w', padx=10, pady=(6, 0))
        self.value_label = tk.Label(self, textvariable=value_var, fg='#eff8ff', bg=CARD)
        self.value_label.pack(anchor='w', padx=10, pady=(4, 4))
        self.canvas = tk.Canvas(self, bg=CANVAS_BG, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=10)
        self.info_label = tk.Label(self, text='Verlauf der letzten 5 Minuten', fg='#6d7f98', bg=CARD)
        self.info_label.pack(anchor='w', padx=10, pady=(4, 6))
        self.update_scale(1.0)

    def set_compact(self, compact: bool) -> None:
        if self.compact_mode == compact:
            return
        self.compact_mode = compact
        if compact:
            self.title_label.pack_forget()
            self.canvas.pack_forget()
            self.info_label.pack_forget()
            self.value_label.pack_configure(anchor='center', padx=10, pady=(10, 10))
        else:
            self.title_label.pack(anchor='w', padx=10, pady=(6, 0), before=self.value_label)
            self.value_label.pack_configure(anchor='w', padx=10, pady=(4, 4))
            self.canvas.pack(fill='both', expand=True, padx=10)
            self.info_label.pack(anchor='w', padx=10, pady=(4, 6))
        self.redraw()

    def update_scale(self, scale: float) -> None:
        self.title_label.configure(font=('Consolas', max(8, int(12 * scale)), 'bold'))
        self.value_label.configure(font=('Consolas', max(10, int(22 * scale)), 'bold'))
        self.info_label.configure(font=('Segoe UI', max(6, int(8 * scale))))
        self.redraw()

    def add_point(self, timestamp: float, value: float, redraw: bool = True) -> None:
        self.history.append((timestamp, value))
        self._trim(timestamp)
        if redraw:
            self.redraw()

    def peak(self) -> float:
        return max((value for _, value in self.history), default=0.0)

    def _update_stats(self) -> None:
        if not self.history:
            text = 'Verlauf der letzten 5 Minuten'
        else:
            average = sum(value for _, value in self.history) / len(self.history)
            text = f'5 Min   \u00d8 {format_rate(average)}   Spitze {format_rate(self.peak())}'
        if self.info_label.cget('text') != text:
            self.info_label.configure(text=text)

    def redraw(self) -> None:
        now = time.time()
        self._trim(now)
        self._update_stats()
        if self.compact_mode or not self.canvas.winfo_ismapped():
            return
        width = max(80, self.canvas.winfo_width())
        height = max(28, self.canvas.winfo_height())
        self.canvas.delete('all')
        self.canvas.create_rectangle(0, 0, width, height, fill=CANVAS_BG, outline='')
        for step in range(1, 3):
            y = height * step / 3
            self.canvas.create_line(0, y, width, y, fill='#142537')
        if len(self.history) < 2:
            return
        max_value = max(point[1] for point in self.history) or 1.0
        points: list[float] = []
        for timestamp, value in self.history:
            age = max(0.0, now - timestamp)
            x = width - (age / HISTORY_WINDOW_SECONDS) * width
            y = height - (value / max_value) * (height - 10) - 5
            points.extend([x, y])
        if len(points) >= 4:
            self.canvas.create_line(*points, fill=self.accent, width=max(1.0, width / 260), smooth=True)

    def _trim(self, now: float) -> None:
        while self.history and now - self.history[0][0] > HISTORY_WINDOW_SECONDS:
            self.history.popleft()


class MonitorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title('UDM WAN Speed Monitor')
        self.config = ConfigStore.load()
        initial_width = max(MIN_WIDTH, int(self.config.get('window_width', BASE_WIDTH)))
        initial_height = max(MIN_HEIGHT, int(self.config.get('window_height', BASE_HEIGHT)))
        initial_x = int(self.config.get('window_x', 120))
        initial_y = int(self.config.get('window_y', 120))
        self.root.geometry(f'{initial_width}x{initial_height}+{initial_x}+{initial_y}')
        self.root.minsize(MIN_WIDTH, MIN_HEIGHT)
        self.root.configure(bg=BG)

        self.client: UdmClient | None = None
        self.running = False
        self.worker: threading.Thread | None = None
        self.monitor_generation = 0
        self.consecutive_errors = 0
        self.options_visible = bool(self.config.get('options_visible', False))
        self.scale = 1.0
        self.tray_icon: pystray.Icon | None = None
        self.tray_thread: threading.Thread | None = None
        self.settings_window: tk.Toplevel | None = None
        self.resize_after_id: str | None = None
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.resize_start_x = 0
        self.resize_start_y = 0
        self.resize_start_width = BASE_WIDTH
        self.resize_start_height = BASE_HEIGHT
        self.settings_open_in_tray = False
        self.in_tray = False
        self.exiting = False
        self.event_log = EventLog()
        self.wan_online: dict[str, bool] = {}
        self.wan_offline_since: dict[str, float] = {}
        self.connection_lost_since: float | None = None
        self.tray_history: deque[tuple[float, float]] = deque(maxlen=int(HISTORY_WINDOW_SECONDS / DEFAULT_POLL_SECONDS))
        self.tray_tooltip = 'UDM WAN Speed Monitor'
        self.bufferbloat = {'WAN 1': BufferbloatTracker(), 'WAN 2': BufferbloatTracker()}
        self.bufferbloat_alerted: set[str] = set()
        self.last_window_geometry = {
            'window_width': initial_width,
            'window_height': initial_height,
            'window_x': initial_x,
            'window_y': initial_y,
        }

        self.wan1_download_var = tk.StringVar(value='--')
        self.wan1_upload_var = tk.StringVar(value='--')
        self.wan2_download_var = tk.StringVar(value='--')
        self.wan2_upload_var = tk.StringVar(value='--')
        self.wan1_ip_var = tk.StringVar(value='WAN 1   Ping --')
        self.wan2_ip_var = tk.StringVar(value='WAN 2   Ping --')
        self.footer_var = tk.StringVar(value=f'CC BY 4.0 Maxxter {time.localtime().tm_year}')

        self.host_var = tk.StringVar(value=self.config.get('host', ''))
        self.username_var = tk.StringVar(value=self.config.get('username', ''))
        self.password_var = tk.StringVar(value=self.config.get('password', ''))
        self.remember_var = tk.BooleanVar(value=bool(self.config.get('remember_password', True)))
        self.always_on_top_var = tk.BooleanVar(value=bool(self.config.get('always_on_top', False)))
        self.show_wan2_var = tk.BooleanVar(value=bool(self.config.get('show_wan2', False)))
        self.autostart_var = tk.BooleanVar(value=bool(self.config.get('autostart', False)))
        self.minimize_to_tray_var = tk.BooleanVar(value=bool(self.config.get('minimize_to_tray', True)))
        self.show_top_clients_var = tk.BooleanVar(value=bool(self.config.get('show_top_clients', True)))
        self.top_clients_var = tk.StringVar(value='')

        self.scaled_widgets: list[tuple[tk.Widget, str, int, str]] = []
        self._build_ui()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', self.always_on_top_var.get())
        self._apply_wan2_visibility()
        self._apply_top_clients_visibility()
        self._apply_autostart()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.bind('<Configure>', self._on_resize)
        self.root.bind('<Unmap>', self._on_unmap)
        self.root.after(150, self._bootstrap)

    def _icon_path(self) -> Path | None:
        for candidate in ICON_CANDIDATES:
            if candidate.exists():
                return candidate
        return None

    def _tray_image(self):
        if self.tray_history:
            return self._render_tray_image()
        icon_path = self._icon_path()
        if icon_path is None:
            return None
        return PILImage.open(icon_path)

    def _render_tray_image(self):
        """Zeichnet Down- und Uplink als zwei Balken ins Tray-Symbol, skaliert auf die Spitze der letzten 5 Minuten."""
        size = 64
        image = PILImage.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        offline = any(state is False for state in self.wan_online.values()) or self.connection_lost_since is not None
        draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=10, fill=BG, outline='#ff4d4f' if offline else '#1f2937', width=4 if offline else 2)
        down, up = self.tray_history[-1]
        peak_down = max((point[0] for point in self.tray_history), default=0.0) or 1.0
        peak_up = max((point[1] for point in self.tray_history), default=0.0) or 1.0
        inner_top, inner_bottom = 8, size - 8
        for left, value, peak, color in ((10, down, peak_down, NEON_GREEN), (36, up, peak_up, NEON_BLUE)):
            height = max(3, int((inner_bottom - inner_top) * min(1.0, value / peak)))
            draw.rectangle((left, inner_bottom - height, left + 17, inner_bottom), fill=color)
        return image

    def _update_tray(self) -> None:
        if self.tray_icon is None:
            return
        try:
            self.tray_icon.title = self.tray_tooltip
            if self.tray_history:
                self.tray_icon.icon = self._render_tray_image()
        except Exception:
            pass

    def _start_window_drag(self, event) -> None:
        self.drag_offset_x = event.x_root - self.root.winfo_x()
        self.drag_offset_y = event.y_root - self.root.winfo_y()

    def _drag_window(self, event) -> None:
        x = event.x_root - self.drag_offset_x
        y = event.y_root - self.drag_offset_y
        self.root.geometry(f'+{x}+{y}')

    def _start_window_resize(self, event) -> None:
        self.resize_start_x = event.x_root
        self.resize_start_y = event.y_root
        self.resize_start_width = self.root.winfo_width()
        self.resize_start_height = self.root.winfo_height()

    def _resize_window(self, event) -> None:
        width = max(MIN_WIDTH, self.resize_start_width + (event.x_root - self.resize_start_x))
        height = max(MIN_HEIGHT, self.resize_start_height + (event.y_root - self.resize_start_y))
        self.root.geometry(f'{width}x{height}')

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=BG, highlightbackground='#1f2937', highlightthickness=1)
        shell.pack(fill='both', expand=True, padx=2, pady=2)
        shell.rowconfigure(1, weight=1)
        shell.columnconfigure(0, weight=1)

        header = tk.Frame(shell, bg='#0b1118', height=32)
        header.grid(row=0, column=0, sticky='ew')
        header.columnconfigure(2, weight=1)
        header.bind('<ButtonPress-1>', self._start_window_drag)
        header.bind('<B1-Motion>', self._drag_window)

        file_button = tk.Menubutton(header, text='Datei', bg='#0b1118', fg='#ffffff', activebackground='#182433', activeforeground='#ffffff', relief='flat', cursor='hand2')
        file_menu = tk.Menu(file_button, tearoff=False, bg='#111827', fg='#ffffff', activebackground='#24405a', activeforeground='#ffffff', font=('Segoe UI', 14))
        file_menu.add_command(label='Ereignisprotokoll oeffnen', command=self.open_event_log)
        file_menu.add_command(label='Beenden', command=self._on_close)
        file_button.configure(menu=file_menu)
        file_button.grid(row=0, column=0, sticky='w', padx=(6, 2), pady=2)
        self.scaled_widgets.append((file_button, 'Segoe UI', 14, 'normal'))

        settings_button = tk.Button(header, text='Einstellungen', command=self.open_settings_window, bg='#0b1118', fg='#ffffff', activebackground='#182433', activeforeground='#ffffff', relief='flat', cursor='hand2')
        settings_button.grid(row=0, column=1, sticky='w', padx=(2, 8), pady=2)
        self.scaled_widgets.append((settings_button, 'Segoe UI', 14, 'normal'))

        title_label = tk.Label(header, text='UDM WAN Speed Monitor', bg='#0b1118', fg='#ffffff', anchor='center')
        title_label.place(relx=0.5, rely=0.5, anchor='center')
        title_label.bind('<ButtonPress-1>', self._start_window_drag)
        title_label.bind('<B1-Motion>', self._drag_window)
        self.scaled_widgets.append((title_label, 'Segoe UI', 14, 'bold'))

        minimize_button = tk.Button(header, text='_', command=self._hide_to_tray, bg='#0b1118', fg='#ffffff', activebackground='#182433', activeforeground='#ffffff', relief='flat', cursor='hand2', bd=0, padx=8)
        minimize_button.grid(row=0, column=3, sticky='e', padx=(0, 2), pady=1)
        self.scaled_widgets.append((minimize_button, 'Segoe UI', 14, 'bold'))

        close_button = tk.Button(header, text='X', command=self._on_close, bg='#0b1118', fg='#ffffff', activebackground='#3a1218', activeforeground='#ffffff', relief='flat', cursor='hand2', bd=0, padx=8)
        close_button.grid(row=0, column=4, sticky='e', padx=(0, 4), pady=1)
        self.scaled_widgets.append((close_button, 'Segoe UI', 14, 'bold'))

        shell.rowconfigure(1, weight=1)

        self.content = tk.Frame(shell, bg=BG)
        self.content.grid(row=1, column=0, sticky='nsew', padx=8, pady=(8, 0))
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.content.columnconfigure(1, weight=1)

        self.wan1_panel = tk.Frame(self.content, bg=BG)
        self.wan1_panel.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        self.wan1_panel.rowconfigure(1, weight=1)
        self.wan1_panel.rowconfigure(2, weight=1)
        self.wan1_panel.columnconfigure(0, weight=1)
        self.wan1_title = self._var_label(self.wan1_panel, self.wan1_ip_var, NEON_GREEN, BG, 'Consolas', 24, 'bold')
        self.wan1_title.grid(row=0, column=0, sticky='w', padx=4, pady=(0, 6))
        self.wan1_download_panel = GraphPanel(self.wan1_panel, 'DOWNLINK', NEON_GREEN, self.wan1_download_var)
        self.wan1_download_panel.grid(row=1, column=0, sticky='nsew', pady=(0, 8))
        self.wan1_upload_panel = GraphPanel(self.wan1_panel, 'UPLINK', NEON_BLUE, self.wan1_upload_var)
        self.wan1_upload_panel.grid(row=2, column=0, sticky='nsew')

        self.wan2_panel = tk.Frame(self.content, bg=BG)
        self.wan2_panel.grid(row=0, column=1, sticky='nsew', padx=(6, 0))
        self.wan2_panel.rowconfigure(1, weight=1)
        self.wan2_panel.rowconfigure(2, weight=1)
        self.wan2_panel.columnconfigure(0, weight=1)
        self.wan2_title = self._var_label(self.wan2_panel, self.wan2_ip_var, NEON_GREEN, BG, 'Consolas', 24, 'bold')
        self.wan2_title.grid(row=0, column=0, sticky='w', padx=4, pady=(0, 6))
        self.wan2_download_panel = GraphPanel(self.wan2_panel, 'DOWNLINK', NEON_GREEN, self.wan2_download_var)
        self.wan2_download_panel.grid(row=1, column=0, sticky='nsew', pady=(0, 8))
        self.wan2_upload_panel = GraphPanel(self.wan2_panel, 'UPLINK', NEON_BLUE, self.wan2_upload_var)
        self.wan2_upload_panel.grid(row=2, column=0, sticky='nsew')

        self.top_clients_label = self._var_label(self.content, self.top_clients_var, '#9fb3c8', BG, 'Consolas', 12, 'normal')
        self.top_clients_label.grid(row=1, column=0, columnspan=2, sticky='ew', padx=4, pady=(6, 0))

        footer = tk.Frame(shell, bg=BG)
        footer.grid(row=2, column=0, sticky='ew', pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        self.footer_label = self._var_label(footer, self.footer_var, '#6c7e97', BG, 'Segoe UI', 8, 'normal', wraplength=900)
        self.footer_label.grid(row=0, column=0, sticky='w')
        resize_grip = tk.Label(footer, text='//', bg=BG, fg='#50637f', cursor='size_nw_se')
        resize_grip.grid(row=0, column=1, sticky='se', padx=(8, 2))
        resize_grip.bind('<ButtonPress-1>', self._start_window_resize)
        resize_grip.bind('<B1-Motion>', self._resize_window)
        self.scaled_widgets.append((resize_grip, 'Consolas', 10, 'bold'))

        self._apply_scale(1.0)

    def _label(self, parent: tk.Widget, text: str, fg: str, bg: str, family: str, size: int, weight: str, wraplength: int = 0) -> tk.Label:
        label = tk.Label(parent, text=text, fg=fg, bg=bg, justify='left', anchor='w', wraplength=wraplength)
        self.scaled_widgets.append((label, family, size, weight))
        return label

    def _var_label(self, parent: tk.Widget, variable: tk.StringVar, fg: str, bg: str, family: str, size: int, weight: str, wraplength: int = 0) -> tk.Label:
        label = tk.Label(parent, textvariable=variable, fg=fg, bg=bg, justify='left', anchor='w', wraplength=wraplength)
        self.scaled_widgets.append((label, family, size, weight))
        return label

    def _entry_field(self, parent: tk.Widget, row: int, label: str, variable: tk.StringVar, secret: bool) -> None:
        field_label = self._label(parent, label, '#c7d2e0', CARD, 'Segoe UI', 14, 'bold')
        field_label.grid(row=row, column=0, sticky='w', padx=10, pady=(6, 2))
        entry = tk.Entry(parent, textvariable=variable, show='*' if secret else '', bg='#07111b', fg='#eef6ff', insertbackground=NEON_GREEN, relief='flat', highlightthickness=1, highlightbackground='#203040', highlightcolor=NEON_GREEN, bd=0)
        entry.grid(row=row + 1, column=0, sticky='ew', padx=10, ipady=10)
        self.scaled_widgets.append((entry, 'Consolas', 18, 'normal'))

    def _check(self, parent: tk.Widget, text: str, variable: tk.BooleanVar) -> tk.Checkbutton:
        check = tk.Checkbutton(parent, text=text, variable=variable, fg='#d9e3f0', bg=CARD, activebackground=CARD, activeforeground=NEON_GREEN, selectcolor='#07111b')
        self.scaled_widgets.append((check, 'Segoe UI', 24, 'normal'))
        return check

    def _button(self, parent: tk.Widget, text: str, command, bg: str, fg: str) -> tk.Button:
        button = tk.Button(parent, text=text, command=command, bg=bg, fg=fg, activebackground=NEON_GREEN if bg == NEON_GREEN else '#1d3042', activeforeground=fg, relief='flat', cursor='hand2')
        self.scaled_widgets.append((button, 'Segoe UI', 14, 'bold'))
        return button

    def _apply_wan2_visibility(self) -> None:
        if self.show_wan2_var.get():
            self.wan2_panel.grid()
            self.wan1_panel.grid_configure(column=0, padx=(0, 6))
            self.content.columnconfigure(0, weight=1)
            self.content.columnconfigure(1, weight=1)
        else:
            self.wan2_panel.grid_remove()
            self.wan1_panel.grid_configure(column=0, padx=(0, 0))
            self.content.columnconfigure(0, weight=1)
            self.content.columnconfigure(1, weight=0)
            self.wan2_download_var.set('--')
            self.wan2_upload_var.set('--')
            self.wan2_ip_var.set('WAN 2   Ping --')
        self._layout_graphs()

    def _apply_top_clients_visibility(self) -> None:
        if self.show_top_clients_var.get():
            self.top_clients_label.grid()
        else:
            self.top_clients_label.grid_remove()
            self.top_clients_var.set('')

    def _autostart_command(self) -> str:
        if getattr(sys, 'frozen', False):
            return f'@echo off\r\nstart "" "{sys.executable}"\r\n'
        python_exe = sys.executable
        app_script = APP_DIR / 'app.py'
        return f'@echo off\r\ncd /d "{APP_DIR}"\r\nstart "" "{python_exe}" "{app_script}"\r\n'

    def _apply_autostart(self) -> None:
        try:
            STARTUP_DIR.mkdir(parents=True, exist_ok=True)
            if self.autostart_var.get():
                AUTOSTART_BAT_PATH.write_text(self._autostart_command(), encoding='utf-8')
            elif AUTOSTART_BAT_PATH.exists():
                AUTOSTART_BAT_PATH.unlink()
        except Exception:
            pass

    def _bootstrap(self) -> None:
        self.wan1_download_panel.redraw()
        self.wan1_upload_panel.redraw()
        self.wan2_download_panel.redraw()
        self.wan2_upload_panel.redraw()
        if self._has_credentials(self.config):
            self.start_monitoring()
        else:
            self.open_settings_window(force=True)

    def open_settings_window(self, force: bool = False) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.attributes('-topmost', self.always_on_top_var.get())
            self.settings_window.lift()
            self.settings_window.focus_force()
            self.options_visible = True
            return

        window = tk.Toplevel(self.root, bg=CARD)
        self.settings_window = window
        self.options_visible = True
        window.title('Einstellungen')
        window.transient(self.root)
        window.geometry('620x620')
        window.configure(bg=CARD)
        window.attributes('-topmost', self.always_on_top_var.get())
        window.columnconfigure(0, weight=1)
        try:
            icon_path = self._icon_path()
            if icon_path is not None:
                window.iconbitmap(default=str(icon_path))
        except Exception:
            pass

        panel = tk.Frame(window, bg=CARD, highlightbackground=NEON_GREEN, highlightthickness=1)
        panel.grid(row=0, column=0, sticky='nsew', padx=10, pady=10)
        panel.columnconfigure(0, weight=1)
        title = self._label(panel, 'EINSTELLUNGEN', NEON_GREEN, CARD, 'Consolas', 26, 'bold')
        title.grid(row=0, column=0, sticky='w', padx=10, pady=(8, 2))
        hint = self._label(panel, 'Speichern uebernimmt die Daten und startet das Monitoring direkt.', '#8ea0b8', CARD, 'Segoe UI', 10, 'normal', wraplength=320)
        hint.grid(row=1, column=0, sticky='w', padx=10, pady=(0, 6))

        self._entry_field(panel, 2, 'Router-IP oder URL', self.host_var, False)
        self._entry_field(panel, 4, 'Benutzername', self.username_var, False)
        self._entry_field(panel, 6, 'Passwort', self.password_var, True)
        remember_check = self._check(panel, 'Passwort dauerhaft speichern', self.remember_var)
        remember_check.grid(row=8, column=0, sticky='w', padx=10, pady=(8, 2))
        show_wan2_check = self._check(panel, 'WAN 2 anzeigen', self.show_wan2_var)
        show_wan2_check.grid(row=9, column=0, sticky='w', padx=10, pady=(2, 2))
        autostart_check = self._check(panel, 'Mit Windows starten', self.autostart_var)
        autostart_check.grid(row=10, column=0, sticky='w', padx=10, pady=(2, 2))
        topmost_check = self._check(panel, 'Immer im Vordergrund', self.always_on_top_var)
        topmost_check.grid(row=11, column=0, sticky='w', padx=10, pady=(2, 2))
        tray_check = self._check(panel, 'Beim Minimieren in Tray', self.minimize_to_tray_var)
        tray_check.grid(row=12, column=0, sticky='w', padx=10, pady=(2, 2))
        top_clients_check = self._check(panel, 'Top-Verbraucher anzeigen', self.show_top_clients_var)
        top_clients_check.grid(row=13, column=0, sticky='w', padx=10, pady=(2, 4))
        save_button = self._button(panel, 'Speichern', self.save_settings, NEON_GREEN, '#03120d')
        save_button.grid(row=14, column=0, sticky='ew', padx=10, pady=(6, 6))
        logout_button = self._button(panel, 'Abmelden', self.logout_and_clear_session, '#111827', '#dbe8f7')
        logout_button.grid(row=15, column=0, sticky='ew', padx=10, pady=(0, 8))

        self.options_hint = hint
        self.save_button = save_button
        self.logout_button = logout_button

        window.protocol('WM_DELETE_WINDOW', self._close_settings_window)
        window.bind('<Destroy>', self._on_settings_window_destroy)
        self._apply_scale(self.scale if not force else max(0.75, self.scale))
        window.update_idletasks()
        required_width = max(620, panel.winfo_reqwidth() + 40)
        required_height = max(620, panel.winfo_reqheight() + 40)
        window.minsize(required_width, required_height)
        window.geometry(f'{required_width}x{required_height}')
        window.lift()
        window.focus_force()

    def _close_settings_window(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.destroy()

    def _on_settings_window_destroy(self, _event=None) -> None:
        self.settings_window = None
        self.options_visible = False

    def _has_credentials(self, config: dict[str, Any]) -> bool:
        return bool(config.get('host') and config.get('username') and config.get('password'))

    def _layout_graphs(self) -> None:
        self.root.update_idletasks()
        panel_height = max(self.wan1_panel.winfo_height(), self.wan2_panel.winfo_height(), 120)
        graph_height = max(28, (panel_height - 30) // 2)
        compact = graph_height < 48
        for panel in [self.wan1_download_panel, self.wan1_upload_panel, self.wan2_download_panel, self.wan2_upload_panel]:
            panel.set_compact(compact)
            panel.canvas.configure(height=graph_height)
            panel.redraw()

    def save_settings(self) -> None:
        host = self.host_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not host or not username or not password:
            parent = self.settings_window if self.settings_window is not None and self.settings_window.winfo_exists() else self.root
            messagebox.showerror('Fehlende Angaben', 'Router-IP, Benutzername und Passwort sind erforderlich.', parent=parent)
            return
        if not host.startswith('http://') and not host.startswith('https://'):
            host = 'https://' + host
        self.config = {
            'host': host,
            'username': username,
            'password': password,
            'remember_password': bool(self.remember_var.get()),
            'options_visible': False,
            'always_on_top': bool(self.always_on_top_var.get()),
            'show_wan2': bool(self.show_wan2_var.get()),
            'autostart': bool(self.autostart_var.get()),
            'minimize_to_tray': bool(self.minimize_to_tray_var.get()),
            'show_top_clients': bool(self.show_top_clients_var.get()),
            'cert_host': self.config.get('cert_host', ''),
            'cert_fingerprint': self.config.get('cert_fingerprint', ''),
            **self.last_window_geometry,
        }
        ConfigStore.save(self.config)
        self.root.attributes('-topmost', self.always_on_top_var.get())
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.attributes('-topmost', self.always_on_top_var.get())
        self._apply_wan2_visibility()
        self._apply_top_clients_visibility()
        if not self.show_wan2_var.get():
            self.wan_online.pop('WAN 2', None)
            self.wan_offline_since.pop('WAN 2', None)
        self._apply_autostart()
        self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')
        self.stop_monitoring(silent=True)
        self.options_visible = False
        self._close_settings_window()
        self.start_monitoring()

    def start_monitoring(self) -> None:
        if self.running or not self._has_credentials(self.config):
            return
        self.monitor_generation += 1
        generation = self.monitor_generation
        client = UdmClient(self.config)
        self.client = client
        self.running = True
        self.consecutive_errors = 0
        self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')
        self.worker = threading.Thread(target=self._poll_loop, args=(generation, client), daemon=True)
        self.worker.start()

    def stop_monitoring(self, silent: bool = False) -> None:
        client = self.client
        self.monitor_generation += 1
        self.running = False
        self.client = None
        if not silent:
            self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')
        if client is not None:
            threading.Thread(target=client.logout, daemon=True).start()

    def logout_and_clear_session(self) -> None:
        self.stop_monitoring(silent=True)
        self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')
        self.options_visible = False
        self._persist_config()
        self.open_settings_window(force=True)

    def _persist_config(self) -> None:
        self.config['options_visible'] = self.options_visible
        self.config['always_on_top'] = bool(self.always_on_top_var.get())
        self.config['show_wan2'] = bool(self.show_wan2_var.get())
        self.config['autostart'] = bool(self.autostart_var.get())
        self.config['minimize_to_tray'] = bool(self.minimize_to_tray_var.get())
        self.config['show_top_clients'] = bool(self.show_top_clients_var.get())
        self.config.update(self.last_window_geometry)
        if self._has_credentials(self.config):
            ConfigStore.save(self.config)

    def _poll_loop(self, generation: int, client: UdmClient) -> None:
        consecutive_errors = 0
        while self.running and generation == self.monitor_generation:
            try:
                snapshot = client.fetch_wan_snapshot()
                consecutive_errors = 0
                if not client.pinned_fingerprint and client.observed_fingerprint:
                    client.pinned_fingerprint = client.observed_fingerprint
                    self.root.after(0, self._remember_fingerprint, generation, client.observed_fingerprint)
                self.root.after(0, self._apply_snapshot, generation, snapshot)
                delay = DEFAULT_POLL_SECONDS
            except CertificateMismatchError as exc:
                self.root.after(0, self._handle_certificate_change, generation, exc.expected, exc.actual)
                return
            except AuthenticationError as exc:
                self.root.after(0, self._handle_auth_failure, generation, str(exc))
                return
            except Exception as exc:
                consecutive_errors += 1
                client.logged_in = False
                self.root.after(0, self._apply_error, generation, consecutive_errors, str(exc))
                delay = max(DEFAULT_POLL_SECONDS, min(30.0, DEFAULT_POLL_SECONDS * consecutive_errors))
            self._sleep_poll_interval(generation, delay)

    def _sleep_poll_interval(self, generation: int, delay: float) -> None:
        deadline = time.time() + delay
        while self.running and generation == self.monitor_generation and time.time() < deadline:
            time.sleep(min(0.25, deadline - time.time()))

    def _apply_snapshot(self, generation: int, snapshot: WanSnapshot) -> None:
        if generation != self.monitor_generation:
            return
        self.consecutive_errors = 0
        if self.connection_lost_since is not None:
            self._report_event('UDM wieder erreichbar', '-', f'Unterbrechung {format_duration(time.time() - self.connection_lost_since)}')
            self.connection_lost_since = None
        slots = {
            'WAN 1': (self.wan1_download_var, self.wan1_upload_var, self.wan1_download_panel, self.wan1_upload_panel, self.wan1_ip_var, self.wan1_title),
            'WAN 2': (self.wan2_download_var, self.wan2_upload_var, self.wan2_download_panel, self.wan2_upload_panel, self.wan2_ip_var, self.wan2_title),
        }
        seen = set()
        dirty_panels: set[GraphPanel] = set()
        total_down = total_up = 0.0
        tooltip_lines = ['UDM WAN Speed Monitor']
        for reading in snapshot.readings:
            slot = slots.get(reading.name)
            if slot is None:
                continue
            download_var, upload_var, download_panel, upload_panel, ip_var, title_label = slot
            download_var.set(format_rate(reading.download_bps))
            upload_var.set(format_rate(reading.upload_bps))
            download_panel.add_point(reading.timestamp, reading.download_bps, redraw=False)
            upload_panel.add_point(reading.timestamp, reading.upload_bps, redraw=False)
            dirty_panels.add(download_panel)
            dirty_panels.add(upload_panel)
            if reading.name == 'WAN 1' or self.show_wan2_var.get():
                self._track_wan_state(reading.name, reading.online)
            extra_latency = self._track_bufferbloat(reading)
            if reading.online is False:
                ip_var.set(f'{reading.name}   OFFLINE')
                title_label.configure(fg='#ff4d4f')
            elif extra_latency is not None and extra_latency >= BUFFERBLOAT_ALERT_MS:
                ip_var.set(f"{reading.name}   Ping {reading.ping_ms or '--'}   Last +{extra_latency:.0f} ms ({bufferbloat_grade(extra_latency)})")
                title_label.configure(fg='#ffb020')
            else:
                ip_var.set(f"{reading.name}   Ping {reading.ping_ms or '--'}")
                title_label.configure(fg=NEON_GREEN)
            total_down += reading.download_bps
            total_up += reading.upload_bps
            status = 'offline' if reading.online is False else reading.ping_ms
            tooltip_lines.append(f'{reading.name}: \u2193{format_rate(reading.download_bps)} \u2191{format_rate(reading.upload_bps)} {status}')
            seen.add(reading.name)
        for name, slot in slots.items():
            if name in seen:
                continue
            download_var, upload_var, _, _, ip_var, _ = slot
            download_var.set('--')
            upload_var.set('--')
            ip_var.set(f"{name}   Ping --")
        for panel in dirty_panels:
            panel.redraw()
        if snapshot.top_clients is not None and self.show_top_clients_var.get():
            parts = [f'{name} {format_rate(rate)}' for name, rate in snapshot.top_clients]
            self.top_clients_var.set('Top   ' + '   \u00b7   '.join(parts) if parts else 'Top   keine aktiven Geraete')
        self.tray_history.append((total_down, total_up))
        # Windows kuerzt Tray-Tooltips auf 127 Zeichen.
        self.tray_tooltip = '\n'.join(tooltip_lines)[:127]
        self._update_tray()
        self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')

    def _track_bufferbloat(self, reading: WanReading) -> float | None:
        tracker = self.bufferbloat.get(reading.name)
        if tracker is None:
            return None
        tracker.add(reading.timestamp, ping_to_float(reading.ping_ms), reading.download_bps + reading.upload_bps)
        extra = tracker.extra_latency_ms()
        if extra is None:
            return None
        if extra >= BUFFERBLOAT_ALERT_MS and reading.name not in self.bufferbloat_alerted:
            self.bufferbloat_alerted.add(reading.name)
            self._report_event('Bufferbloat erkannt', reading.name, f'Ping unter Last +{extra:.0f} ms (Note {bufferbloat_grade(extra)}). Tipp: Smart Queues in der UDM aktivieren')
        elif extra < BUFFERBLOAT_ALERT_MS / 2:
            self.bufferbloat_alerted.discard(reading.name)
        return extra

    def _track_wan_state(self, name: str, online: bool | None) -> None:
        if online is None:
            return
        previous = self.wan_online.get(name)
        self.wan_online[name] = online
        if previous == online:
            return
        if not online:
            self.wan_offline_since[name] = time.time()
            self._report_event('Leitung offline', name, 'beim Start' if previous is None else '', notify=previous is not None)
        elif previous is False:
            since = self.wan_offline_since.pop(name, None)
            self._report_event('Leitung wieder online', name, f'Ausfall {format_duration(time.time() - since)}' if since else '')

    def _report_event(self, event: str, wan: str, detail: str = '', notify: bool = True) -> None:
        self.event_log.write(event, wan, detail)
        if not notify or self.tray_icon is None:
            return
        message = f'{wan}: {event}' if wan != '-' else event
        try:
            self.tray_icon.notify(f'{message} {detail}'.strip(), 'UDM WAN Speed Monitor')
        except Exception:
            pass

    def open_event_log(self) -> None:
        if not EVENT_LOG_PATH.exists():
            self.event_log.write('Protokoll angelegt', '-', '')
        try:
            os.startfile(str(EVENT_LOG_PATH))
        except OSError as exc:
            messagebox.showerror('Ereignisprotokoll', f'Datei konnte nicht geoeffnet werden:\n{EVENT_LOG_PATH}\n\n{exc}', parent=self.root)

    def _remember_fingerprint(self, generation: int, fingerprint: str) -> None:
        if generation != self.monitor_generation or not fingerprint:
            return
        self.config['cert_host'] = self.config.get('host', '')
        self.config['cert_fingerprint'] = fingerprint
        self._persist_config()

    def _bring_to_front(self) -> None:
        if self.in_tray:
            self._restore_from_tray()

    def _handle_certificate_change(self, generation: int, expected: str, actual: str) -> None:
        if generation != self.monitor_generation:
            return
        self.stop_monitoring(silent=True)
        self.event_log.write('Zertifikat geaendert', '-', f'bisher {expected} jetzt {actual}')
        self._bring_to_front()
        trust = messagebox.askyesno(
            'Zertifikat der UDM geaendert',
            'Die UDM meldet sich mit einem anderen TLS-Zertifikat als bisher.\n\n'
            f'Bisher: {fingerprint_label(expected)}\nJetzt:  {fingerprint_label(actual)}\n\n'
            'Das ist normal nach einem Firmware-Update, Zuruecksetzen oder neuem Zertifikat. '
            'Kommt es unerwartet, koennte jemand die Verbindung abfangen. Bis zur Bestaetigung wurde kein Passwort gesendet.\n\n'
            'Dem neuen Zertifikat vertrauen und weiter ueberwachen?',
            icon='warning',
            parent=self.root,
        )
        if trust:
            self.config['cert_host'] = self.config.get('host', '')
            self.config['cert_fingerprint'] = actual
            self._persist_config()
            self.start_monitoring()
        else:
            self.footer_var.set('Monitoring gestoppt: Zertifikat nicht bestaetigt')

    def _handle_auth_failure(self, generation: int, message: str) -> None:
        if generation != self.monitor_generation:
            return
        self.stop_monitoring(silent=True)
        self.footer_var.set('Anmeldung abgelehnt, Monitoring angehalten')
        self._bring_to_front()
        messagebox.showerror('Anmeldung fehlgeschlagen', f'{message}\n\nDas Monitoring wurde angehalten, damit die UDM das Konto nicht wegen wiederholter Fehlversuche sperrt.', parent=self.root)
        self.open_settings_window(force=True)

    def _apply_error(self, generation: int, consecutive_errors: int, message: str) -> None:
        if generation != self.monitor_generation:
            return
        self.consecutive_errors = consecutive_errors
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            # Einzelne Aussetzer lassen die letzten Werte stehen, erst danach wird geleert.
            self.wan1_download_var.set('--')
            self.wan1_upload_var.set('--')
            self.wan2_download_var.set('--')
            self.wan2_upload_var.set('--')
            self.wan1_ip_var.set('WAN 1   Ping --')
            self.wan2_ip_var.set('WAN 2   Ping --')
            if self.connection_lost_since is None:
                self.connection_lost_since = time.time()
                self._report_event('UDM nicht erreichbar', '-', message[:160])
                self.tray_tooltip = 'UDM WAN Speed Monitor\nUDM nicht erreichbar'
                self._update_tray()
            detail = message[:120] if message else 'UDM nicht erreichbar'
            self.footer_var.set(f'Verbindung unterbrochen, retry laeuft: {detail}')
        else:
            self.footer_var.set(f'Verbindung wird erneut aufgebaut ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})')

    def _remember_window_geometry(self) -> None:
        try:
            width = max(MIN_WIDTH, self.root.winfo_width())
            height = max(MIN_HEIGHT, self.root.winfo_height())
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            self.last_window_geometry = {
                'window_width': width,
                'window_height': height,
                'window_x': x,
                'window_y': y,
            }
        except tk.TclError:
            pass

    def _on_resize(self, _event=None) -> None:
        self._remember_window_geometry()
        if self.resize_after_id is not None:
            self.root.after_cancel(self.resize_after_id)
        self.resize_after_id = self.root.after(60, self._apply_pending_resize)

    def _apply_pending_resize(self) -> None:
        self.resize_after_id = None
        width = max(MIN_WIDTH, self.root.winfo_width())
        height = max(MIN_HEIGHT, self.root.winfo_height())
        scale = max(0.45, min(1.25, min(width / BASE_WIDTH, height / BASE_HEIGHT)))
        if abs(scale - self.scale) > 0.02:
            self._apply_scale(scale)
        else:
            self._layout_graphs()

    def _apply_scale(self, scale: float) -> None:
        self.scale = scale
        pad = max(3, int(10 * scale))
        active_widgets: list[tuple[tk.Widget, str, int, str]] = []
        for widget, family, size, weight in self.scaled_widgets:
            if widget.winfo_exists():
                widget.configure(font=(family, max(6, int(size * scale)), weight))
                active_widgets.append((widget, family, size, weight))
        self.scaled_widgets = active_widgets
        for button_name in ['save_button', 'logout_button']:
            button = getattr(self, button_name, None)
            if button is not None and button.winfo_exists():
                button.configure(padx=pad, pady=max(3, int(8 * scale)))
        self.wan1_download_panel.update_scale(scale)
        self.wan1_upload_panel.update_scale(scale)
        self.wan2_download_panel.update_scale(scale)
        self.wan2_upload_panel.update_scale(scale)
        self._layout_graphs()
        if hasattr(self, 'options_hint') and self.options_hint is not None and self.options_hint.winfo_exists():
            self.options_hint.configure(wraplength=max(120, int(220 * scale)))
        self.footer_label.configure(wraplength=max(100, int(self.root.winfo_width() * 0.55)))

    def _on_unmap(self, _event=None) -> None:
        if self.exiting:
            return
        try:
            if self.root.state() == 'iconic' and not self.in_tray and self.minimize_to_tray_var.get():
                self._hide_to_tray()
        except tk.TclError:
            pass

    def _hide_to_tray(self) -> None:
        if self.in_tray:
            return
        image = self._tray_image()
        if image is None:
            return
        self.in_tray = True
        self.root.withdraw()
        if self.settings_window is not None and self.settings_window.winfo_exists() and self.settings_window.state() != 'withdrawn':
            self.settings_open_in_tray = True
            self.settings_window.withdraw()
        else:
            self.settings_open_in_tray = False
        menu = pystray.Menu(
            TrayItem('Oeffnen', lambda: self.root.after(0, self._restore_from_tray), default=True),
            TrayItem('Ereignisprotokoll oeffnen', lambda: self.root.after(0, self.open_event_log)),
            TrayItem('Beenden', lambda: self.root.after(0, self._exit_from_tray)),
        )
        self.tray_icon = pystray.Icon('udm-wan-speed-monitor', image, self.tray_tooltip, menu)
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()

    def _restore_from_tray(self) -> None:
        self._stop_tray_icon()
        self.root.deiconify()
        self.root.state('normal')
        self.root.attributes('-topmost', self.always_on_top_var.get())
        self.root.lift()
        self.root.focus_force()
        if self.settings_open_in_tray and self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.attributes('-topmost', self.always_on_top_var.get())
            self.settings_window.lift()
            self.settings_window.focus_force()
        self.settings_open_in_tray = False

    def _stop_tray_icon(self) -> None:
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.in_tray = False

    def _exit_from_tray(self) -> None:
        self.exiting = True
        self._stop_tray_icon()
        self._on_close()

    def _on_close(self) -> None:
        self.exiting = True
        self.running = False
        self._stop_tray_icon()
        self._persist_config()
        if self.client is not None:
            # Logout mit kurzem Zeitlimit, damit das Schliessen bei nicht erreichbarer UDM nicht haengt.
            logout_thread = threading.Thread(target=self.client.logout, daemon=True)
            logout_thread.start()
            logout_thread.join(LOGOUT_TIMEOUT_SECONDS)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
