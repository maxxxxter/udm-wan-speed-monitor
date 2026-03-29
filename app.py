import ctypes
import ipaddress
import json
import os
import ssl
import sys
import threading
import time
import tkinter as tk
import winreg
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox
from typing import Any
from urllib import error, request

import pystray
from PIL import Image as PILImage
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
HISTORY_WINDOW_SECONDS = 120
DEFAULT_POLL_SECONDS = 2.0
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
}

STARTUP_DIR = Path(os.environ.get('APPDATA', str(Path.home()))) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
AUTOSTART_BAT_PATH = STARTUP_DIR / 'UDM WAN Speed Monitor.bat'


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


crypt32 = ctypes.windll.crypt32
kernel32 = ctypes.windll.kernel32
CRYPTPROTECT_UI_FORBIDDEN = 0x01
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


@dataclass
class WanReading:
    name: str
    download_bps: float
    upload_bps: float
    source: str
    timestamp: float
    external_ip: str = '--'


@dataclass
class WanSnapshot:
    readings: list[WanReading] = field(default_factory=list)


class UdmApiError(RuntimeError):
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
            'password_encrypted': '',
        }
        if data['remember_password'] and config.get('password'):
            data['password_encrypted'] = ConfigStore._encrypt_password(config['password'])
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')

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
        handlers: list[Any] = [request.HTTPCookieProcessor(), request.HTTPSHandler(context=ssl._create_unverified_context())]
        self.opener = request.build_opener(*handlers)

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
        health_payload = self._get_json('/proxy/network/api/s/default/stat/health')
        device_payload = self._get_json('/proxy/network/api/s/default/stat/device')
        sysinfo_payload = self._get_json('/proxy/network/api/s/default/stat/sysinfo')

        readings: list[WanReading] = []
        readings.extend(self._parse_health(health_payload))
        readings.extend(self._parse_devices(device_payload))
        readings.extend(self._parse_sysinfo(sysinfo_payload))
        readings = self._dedupe_readings(readings)
        if not readings:
            raise UdmApiError('Keine WAN-Durchsatzdaten gefunden.')
        return WanSnapshot(readings=self._normalize_wan_names(readings))

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
            return self.opener.open(req, timeout=10)
        except error.HTTPError as exc:
            if exc.code == 401 and path != '/api/auth/login':
                self.logged_in = False
            body = exc.read().decode('utf-8', errors='ignore')
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
        external_ip = self._extract_external_ip(item, name)
        if download is None or upload is None:
            if name is None or external_ip in {'', '--'}:
                return None
            return WanReading(name, float(download or 0.0), float(upload or 0.0), source, time.time(), external_ip)
        return WanReading(name, download, upload, source, time.time(), external_ip)

    def _wan_name(self, item: dict[str, Any], index: int) -> str:
        return self._wan_label_from_context(item, ()) or f'WAN {index}'

    def _merge_readings(self, preferred: WanReading, other: WanReading) -> WanReading:
        external_ip = preferred.external_ip
        if self._ip_priority(other.external_ip, preferred.name) > self._ip_priority(external_ip, preferred.name):
            external_ip = other.external_ip
        return WanReading(preferred.name, preferred.download_bps, preferred.upload_bps, preferred.source, preferred.timestamp, external_ip)

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
            normalized.append(WanReading(label, reading.download_bps, reading.upload_bps, reading.source, reading.timestamp, reading.external_ip))
        return normalized

    def _extract_external_ip(self, item: dict[str, Any], name: str | None = None) -> str:
        if name == 'WAN 2':
            candidates = [
                item.get('ipaddr'), item.get('ip_address'), item.get('ip'), item.get('address'), item.get('addr'),
                item.get('wan_ip'), item.get('public_ip'), item.get('public_ip_address'), item.get('external_ip'),
            ]
        else:
            candidates = [
                item.get('public_ip'), item.get('public_ip_address'), item.get('external_ip'),
                item.get('wan_ip'), item.get('ipaddr'), item.get('ip_address'),
            ]
            if name in {'WAN 1', 'WAN 2'}:
                candidates.extend([item.get('ip'), item.get('address'), item.get('addr')])
        best = '--'
        for value in candidates:
            if self._ip_priority(value, name) > self._ip_priority(best, name):
                best = str(value).strip()
        for key, value in item.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ['public', 'external', 'wan_ip', 'ipaddr', 'ip_address']) and all(token not in lowered for token in ['gateway', 'remote', 'dns']):
                if self._ip_priority(value, name) > self._ip_priority(best, name):
                    best = str(value).strip()
        if name in {'WAN 1', 'WAN 2'}:
            for key, value in item.items():
                lowered = str(key).lower()
                if lowered in {'ip', 'address', 'addr'} and self._ip_priority(value, name) > self._ip_priority(best, name):
                    best = str(value).strip()
        return best

    def _ip_priority(self, value: Any, name: str | None = None) -> int:
        if not self._looks_like_ip(value):
            return 0
        try:
            ip = ipaddress.ip_address(str(value).strip())
        except ValueError:
            return 0
        if name == 'WAN 2' and ip.is_private:
            return 4
        if getattr(ip, 'is_global', False):
            return 3
        if not any([ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_multicast, ip.is_unspecified, ip.is_reserved]):
            return 2
        return 1

    def _looks_like_ip(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        raw = value.strip()
        if raw.count('.') == 3:
            parts = raw.split('.')
            return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)
        return ':' in raw and len(raw) >= 2

    def _extract_rate(self, item: dict[str, Any], direction: str) -> float | None:
        aliases = {
            'download': ['download_bps', 'xput_down', 'speed_down', 'rx_rate', 'rx_bytes-r', 'wan_rx_bps', 'throughput_down'],
            'upload': ['upload_bps', 'xput_up', 'speed_up', 'tx_rate', 'tx_bytes-r', 'wan_tx_bps', 'throughput_up'],
        }
        for key in aliases[direction]:
            value = self._to_bps(item.get(key), key)
            if value is not None:
                return value
        for key, raw in item.items():
            lowered = str(key).lower()
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
        self.history: deque[tuple[float, float]] = deque()
        self.title_label = tk.Label(self, text=title, fg=accent, bg=CARD)
        self.title_label.pack(anchor='w', padx=10, pady=(6, 0))
        self.value_label = tk.Label(self, textvariable=value_var, fg='#eff8ff', bg=CARD)
        self.value_label.pack(anchor='w', padx=10, pady=(4, 4))
        self.canvas = tk.Canvas(self, bg=CANVAS_BG, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=10)
        self.info_label = tk.Label(self, text='Verlauf der letzten 2 Minuten', fg='#6d7f98', bg=CARD)
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

    def add_point(self, timestamp: float, value: float) -> None:
        self.history.append((timestamp, value))
        self._trim(timestamp)
        self.redraw()

    def redraw(self) -> None:
        width = max(80, self.canvas.winfo_width())
        height = max(28, self.canvas.winfo_height())
        self.canvas.delete('all')
        self.canvas.create_rectangle(0, 0, width, height, fill=CANVAS_BG, outline='')
        for step in range(1, 3):
            y = height * step / 3
            self.canvas.create_line(0, y, width, y, fill='#142537')
        now = time.time()
        self._trim(now)
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
        self.root.geometry(f'{BASE_WIDTH}x{BASE_HEIGHT}')
        self.root.minsize(MIN_WIDTH, MIN_HEIGHT)
        self.root.configure(bg=BG)

        self.config = ConfigStore.load()
        self.client: UdmClient | None = None
        self.running = False
        self.worker: threading.Thread | None = None
        self.options_visible = bool(self.config.get('options_visible', False))
        self.scale = 1.0
        self.tray_icon: pystray.Icon | None = None
        self.tray_thread: threading.Thread | None = None
        self.settings_window: tk.Toplevel | None = None
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.resize_start_x = 0
        self.resize_start_y = 0
        self.resize_start_width = BASE_WIDTH
        self.resize_start_height = BASE_HEIGHT
        self.settings_open_in_tray = False
        self.in_tray = False
        self.exiting = False

        self.wan1_download_var = tk.StringVar(value='--')
        self.wan1_upload_var = tk.StringVar(value='--')
        self.wan2_download_var = tk.StringVar(value='--')
        self.wan2_upload_var = tk.StringVar(value='--')
        self.wan1_ip_var = tk.StringVar(value='WAN 1   --')
        self.wan2_ip_var = tk.StringVar(value='WAN 2   --')
        self.footer_var = tk.StringVar(value=f'CC BY 4.0 Maxxter {time.localtime().tm_year}')

        self.host_var = tk.StringVar(value=self.config.get('host', ''))
        self.username_var = tk.StringVar(value=self.config.get('username', ''))
        self.password_var = tk.StringVar(value=self.config.get('password', ''))
        self.remember_var = tk.BooleanVar(value=bool(self.config.get('remember_password', True)))
        self.always_on_top_var = tk.BooleanVar(value=bool(self.config.get('always_on_top', False)))
        self.show_wan2_var = tk.BooleanVar(value=bool(self.config.get('show_wan2', False)))
        self.autostart_var = tk.BooleanVar(value=bool(self.config.get('autostart', False)))
        self.minimize_to_tray_var = tk.BooleanVar(value=bool(self.config.get('minimize_to_tray', True)))

        self.scaled_widgets: list[tuple[tk.Widget, str, int, str]] = []
        self._build_ui()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', self.always_on_top_var.get())
        self._apply_wan2_visibility()
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
        icon_path = self._icon_path()
        if icon_path is None:
            return None
        return PILImage.open(icon_path)

    def _windows_apps_dark_mode(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
                return int(value) == 0
        except Exception:
            return True

    def _apply_title_bar_theme(self) -> None:
        return

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
            self.wan2_ip_var.set('WAN 2   --')
        self._layout_graphs()

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
        tray_check.grid(row=12, column=0, sticky='w', padx=10, pady=(2, 4))
        save_button = self._button(panel, 'Speichern', self.save_settings, NEON_GREEN, '#03120d')
        save_button.grid(row=13, column=0, sticky='ew', padx=10, pady=(6, 6))
        logout_button = self._button(panel, 'Abmelden', self.logout_and_clear_session, '#111827', '#dbe8f7')
        logout_button.grid(row=14, column=0, sticky='ew', padx=10, pady=(0, 8))

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
        }
        ConfigStore.save(self.config)
        self.root.attributes('-topmost', self.always_on_top_var.get())
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.attributes('-topmost', self.always_on_top_var.get())
        self._apply_wan2_visibility()
        self._apply_autostart()
        self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')
        self.stop_monitoring(silent=True)
        self.options_visible = False
        self._close_settings_window()
        self.start_monitoring()

    def start_monitoring(self) -> None:
        if self.running or not self._has_credentials(self.config):
            return
        self.client = UdmClient(self.config)
        self.running = True
        self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')
        self.worker = threading.Thread(target=self._poll_loop, daemon=True)
        self.worker.start()

    def stop_monitoring(self, silent: bool = False) -> None:
        self.running = False
        if not silent:
            self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')
        if self.client is not None:
            self.client.logout()
            self.client = None

    def logout_and_clear_session(self) -> None:
        self.stop_monitoring(silent=True)
        self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')
        self.config['options_visible'] = False
        self.config['show_wan2'] = bool(self.show_wan2_var.get())
        self.config['autostart'] = bool(self.autostart_var.get())
        self.config['minimize_to_tray'] = bool(self.minimize_to_tray_var.get())
        if self._has_credentials(self.config):
            ConfigStore.save(self.config)
        self.open_settings_window(force=True)

    def _poll_loop(self) -> None:
        while self.running and self.client is not None:
            try:
                snapshot = self.client.fetch_wan_snapshot()
                self.root.after(0, self._apply_snapshot, snapshot)
            except Exception:
                self.root.after(0, self._apply_error)
            time.sleep(DEFAULT_POLL_SECONDS)

    def _apply_snapshot(self, snapshot: WanSnapshot) -> None:
        slots = {
            'WAN 1': (self.wan1_download_var, self.wan1_upload_var, self.wan1_download_panel, self.wan1_upload_panel, self.wan1_ip_var),
            'WAN 2': (self.wan2_download_var, self.wan2_upload_var, self.wan2_download_panel, self.wan2_upload_panel, self.wan2_ip_var),
        }
        seen = set()
        for reading in snapshot.readings:
            slot = slots.get(reading.name)
            if slot is None:
                continue
            download_var, upload_var, download_panel, upload_panel, ip_var = slot
            download_var.set(self._format_rate(reading.download_bps))
            upload_var.set(self._format_rate(reading.upload_bps))
            download_panel.add_point(reading.timestamp, reading.download_bps)
            upload_panel.add_point(reading.timestamp, reading.upload_bps)
            ip_var.set(f"{reading.name}   {reading.external_ip or '--'}")
            seen.add(reading.name)
        for name, slot in slots.items():
            if name in seen:
                continue
            download_var, upload_var, _, _, ip_var = slot
            download_var.set('--')
            upload_var.set('--')
            ip_var.set(f"{name}   --")
        self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')

    def _apply_error(self) -> None:
        self.wan1_download_var.set('--')
        self.wan1_upload_var.set('--')
        self.wan2_download_var.set('--')
        self.wan2_upload_var.set('--')
        self.wan1_ip_var.set('WAN 1   --')
        self.wan2_ip_var.set('WAN 2   --')
        self.footer_var.set(f'CC BY 4.0 Maxxter {time.localtime().tm_year}')
        self.running = False
        if self.client is not None:
            self.client.logout()
            self.client = None

    def _format_rate(self, bps: float) -> str:
        units = ['bit/s', 'Kbit/s', 'Mbit/s', 'Gbit/s']
        value = float(bps)
        index = 0
        while value >= 1000 and index < len(units) - 1:
            value /= 1000.0
            index += 1
        return f'{value:.2f} {units[index]}'

    def _on_resize(self, _event=None) -> None:
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

    def toggle_always_on_top(self) -> None:
        enabled = bool(self.always_on_top_var.get())
        self.root.attributes('-topmost', enabled)
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.attributes('-topmost', enabled)
        self.config['always_on_top'] = enabled
        self.config['show_wan2'] = bool(self.show_wan2_var.get())
        self.config['autostart'] = bool(self.autostart_var.get())
        self.config['minimize_to_tray'] = bool(self.minimize_to_tray_var.get())
        if self._has_credentials(self.config):
            ConfigStore.save(self.config)

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
            TrayItem('Beenden', lambda: self.root.after(0, self._exit_from_tray)),
        )
        self.tray_icon = pystray.Icon('udm-wan-speed-monitor', image, 'UDM WAN Speed Monitor', menu)
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
        self.config['options_visible'] = self.options_visible
        self.config['always_on_top'] = bool(self.always_on_top_var.get())
        self.config['show_wan2'] = bool(self.show_wan2_var.get())
        self.config['autostart'] = bool(self.autostart_var.get())
        self.config['minimize_to_tray'] = bool(self.minimize_to_tray_var.get())
        if self._has_credentials(self.config):
            ConfigStore.save(self.config)
        if self.client is not None:
            self.client.logout()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
