#!/usr/bin/env python3
"""
haylou-s40 — Controlador nativo para auriculares Haylou S40 (GTK4/Libadwaita + CLI).
Habla el protocolo binario propietario sobre Classic Bluetooth RFCOMM (canal 10)
usando únicamente la librería estándar de Python (socket.AF_BLUETOOTH) e integra
conmutación de códecs PipeWire/BlueZ y coordinación por socket UNIX para Quickshell.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ───────────────────────── Constantes de dispositivo y runtime ─────────────────────────

DEFAULT_ADDR = os.environ.get("HAYLOU_S40_ADDR", "").strip().upper()
RFCOMM_CHANNEL = 10
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")

# Identidad del DAC USB Jieli: única fuente para detectarlo y para localizar su sink.
USB_ID = "3654:4a55"
USB_SINK_HINT = "Jieli"

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
SOCKET_PATH = RUNTIME_DIR / "haylou-s40.sock"
LOCK_PATH = RUNTIME_DIR / "haylou-s40.lock"
CACHE_PATH = RUNTIME_DIR / "haylou-s40-state.json"

# ───────────────────────── Protocolo binario Haylou S40 ─────────────────────────

HEADER = bytes([0xAA, 0xBB, 0xCC])
FOOTER = bytes([0xDD, 0xEE, 0xFF])

TYPE_WRITE = 0xC0
TYPE_READ = 0xC0

CMD_GET_INFO = 0x02
CMD_SET_INFO = 0x08
CMD_GET_RUN_INFO = 0x09
CMD_EVENT = 0x0E
CMD_SEND_EQ = 0x12
CMD_SET_CONFIG = 0xF2

# Atributos de escritura (CMD_SET_INFO)
ATTR_SHUTDOWN = 0x00
ATTR_ANC = 0x04
ATTR_GAME = 0x05
ATTR_LDAC = 0x08
ATTR_MULTI = 0x09
ATTR_SPATIAL = 0x0A
ATTR_SCENE = 0x0B
ATTR_WIND = 0x0C

# Ordinais TLV en respuesta de CMD_GET_RUN_INFO (op 0x09)
ORD_SHUTDOWN = 0x05
ORD_ANC = 0x09
ORD_GAME = 0x0B
ORD_EQ = 0x0C
ORD_MULTI = 0x0E
ORD_SPATIAL = 0x12
ORD_SCENE = 0x13
ORD_WIND = 0x14

EVT_ANC = 0x08

# Bitmask para pedir todos los estados de una vez
RUNINFO_ALL = (
    512 | 1024 | 2048 | 4096 | 32768 | 262144 | 524288 | 1048576 | 2097152
    | 0x0800 | 0x00020000 | 0x00010000 | 0x00100000 | 0x00000200 | 0x00040000 | 0x00080000
)

ANC_MODES = [
    (0, "off", "Normal (Off)", "Off", "OFF"),
    (1, "anc", "Cancelación activa (ANC)", "ANC", "ANC"),
    (2, "transp", "Sonido ambiente", "Ambiente", "AMB"),
    (4, "adaptive", "ANC adaptativo", "Adaptativo", "ADP"),
]
ANC_BY_VAL = {v: (key, full, short, badge) for v, key, full, short, badge in ANC_MODES}
ANC_ALIASES = {
    "0": 0, "off": 0, "normal": 0,
    "1": 1, "anc": 1, "on": 1,
    "2": 2, "transp": 2, "transparency": 2, "ambiente": 2,
    "4": 4, "adaptive": 4, "adaptativo": 4, "anc+": 4,
}
ANC_CYCLE_ORDER = [0, 1, 2, 4]

SPATIAL_MODES = [
    (2, "off", "Desactivado"),
    (1, "static", "Estático"),
    (0, "dynamic", "Dinámico (Head-tracking)"),
]
SPATIAL_ALIASES = {"off": 2, "2": 2, "static": 1, "estatico": 1, "1": 1, "dynamic": 0, "dinamico": 0, "0": 0}
SPATIAL_BY_VAL = {v: (key, label) for v, key, label in SPATIAL_MODES}

SCENES = [
    (0, "music", "Música"),
    (1, "exercise", "Ejercicio"),
    (2, "movie", "Película"),
]
SCENE_ALIASES = {"music": 0, "musica": 0, "0": 0, "exercise": 1, "ejercicio": 1, "1": 1, "movie": 2, "pelicula": 2, "2": 2}
SCENE_BY_VAL = {v: (key, label) for v, key, label in SCENES}

SHUTDOWN_MODES = [
    (0, "30m", "30 minutos"),
    (1, "1h", "1 hora"),
    (2, "3h", "3 horas"),
    (3, "5h", "5 horas"),
    (4, "never", "Nunca"),
]
SHUTDOWN_ALIASES = {"30m": 0, "30": 0, "0": 0, "1h": 1, "60": 1, "1": 1, "3h": 2, "180": 2, "2": 2, "5h": 3, "300": 3, "3": 3, "never": 4, "nunca": 4, "off": 4, "4": 4}
SHUTDOWN_BY_VAL = {v: (key, label) for v, key, label in SHUTDOWN_MODES}

CONFIG_EQ = 0x0007
EQ_BASIC_PRESETS = [
    ("default", "Predeterminado", 0x00),
    ("bass", "Graves (Bass)", 0x06),
    ("rock", "Rock", 0x02),
    ("soft", "Suave (Soft)", 0x07),
    ("classical", "Clásica", 0x03),
]

_SM_ACOUSTIC = bytes.fromhex(
    "00db012d322c2d322c382c342c302c32302c302e382c302c322c3136302c302e372c312c"
    "2d372c3333302c302e362c312c2d362c3635302c302e362c312c2d312e352c3830302c31"
    "2e342c312c2d362c343530302c332e302c312c2d392c363730302c312e332c312c2d362c"
    "31353030302c302e380a302c302c372c342c302c32302c302e382c312c2d342e352c3230"
    "302c302e372c312c2d31302e352c3539302c302e392c312c2d352c343530302c332e302c"
    "312c2d31302c363730302c312e332c312c2d352c31353030302c302e382c312c2d322c39"
    "30302c322e30"
)
_SM_SOFT_IMM = bytes.fromhex(
    "00f2012d322c2d322c382c342c302c32302c302e382c312c2d372c3333302c302e362c31"
    "2c2d362c3635302c302e362c312c2d312e352c3830302c312e342c312c2d372c34353030"
    "2c332e302c312c2d31312c363730302c312e332c312c2d372c31353030302c302e382c31"
    "2c2d342c37302c302e380a302c302c392c342c302c32302c302e382c312c2d342e352c32"
    "30302c302e372c312c2d31302c3539302c302e392c312c2d372c343530302c322e302c31"
    "2c2d31322c363730302c312e332c312c2d362c31353030302c302e382c312c2d332c3930"
    "302c322e302c312c2d392c39302c302e382c312c2d332c33302c302e38"
)
_SM_BASS_ENH = bytes.fromhex(
    "0117012d352c2d352c31302c342c302c32302c302e382c302c352c3136302c302e372c31"
    "2c2d372c3333302c302e362c312c2d362c3635302c302e362c312c2d312e352c3830302c"
    "312e342c312c2d392c343530302c312e352c312c2d31302c363730302c312e332c312c2d"
    "392c31353030302c302e382c312c2d392c393530302c312e302c312c2d342c323030302c"
    "302e380a302c302c392c342c302c32302c302e382c312c2d342e352c3230302c302e372c"
    "312c2d31332c3539302c302e392c312c2d31302c343530302c312e352c312c2d31342c36"
    "3730302c312e332c312c2d31332c31353030302c302e382c312c2d332c3930302c322e30"
    "2c312c2d372c393030302c312e302c312c2d342e352c323030302c302e38"
)
EQ_MARKET_PRESETS = [
    ("acoustic", "Acoustic Enhanced", _SM_ACOUSTIC),
    ("soft_imm", "Soft & Immersive", _SM_SOFT_IMM),
    ("bass_enh", "Bass Enhance", _SM_BASS_ENH),
]

EQ_ALL_OPTIONS = [
    (key, label, "basic", val) for key, label, val in EQ_BASIC_PRESETS
] + [
    (key, f"{label} (Market)", "market", payload) for key, label, payload in EQ_MARKET_PRESETS
]
EQ_BY_KEY = {item[0]: item for item in EQ_ALL_OPTIONS}
EQ_BY_BASIC_BYTE = {val: key for key, _, val in EQ_BASIC_PRESETS}

CODEC_PROFILES = [
    ("ldac", "a2dp-sink-ldac", "LDAC (Hi-Res 990 kbps)", "LDAC"),
    ("sbc_xq", "a2dp-sink-sbc_xq", "SBC-XQ (Alta tasa de bits)", "SBC-XQ"),
    ("aac", "a2dp-sink", "AAC (Alta fidelidad)", "AAC"),
    ("sbc", "a2dp-sink-sbc", "SBC Estándar", "SBC"),
]
CODEC_ALIASES = {
    "ldac": "a2dp-sink-ldac",
    "sbc-xq": "a2dp-sink-sbc_xq",
    "sbc_xq": "a2dp-sink-sbc_xq",
    "aac": "a2dp-sink",
    "sbc": "a2dp-sink-sbc",
}


def _frame(kind: int, op: int, body: bytes, sn: int = 1) -> bytes:
    body_len = len(body) + 1
    return HEADER + bytes([kind, op, (body_len >> 8) & 0xFF, body_len & 0xFF, sn]) + body + FOOTER


def build_get_battery(sn: int = 0x02) -> bytes:
    return _frame(TYPE_READ, CMD_GET_INFO, bytes([0x00, 0x00, 0x00, 0x04]), sn)


def build_get_all(sn: int = 0x09) -> bytes:
    return _frame(TYPE_READ, CMD_GET_RUN_INFO, RUNINFO_ALL.to_bytes(4, "big"), sn)


def build_set_attr(attr_id: int, value: int, sn: int = 1) -> bytes:
    return _frame(TYPE_WRITE, CMD_SET_INFO, bytes([0x02, attr_id, value]), sn)


def build_set_eq_basic(preset_byte: int, sn: int = 1) -> bytes:
    return _frame(TYPE_WRITE, CMD_SET_CONFIG, bytes([0x03, (CONFIG_EQ >> 8) & 0xFF, CONFIG_EQ & 0xFF, preset_byte]), sn)


def build_send_eq_market(payload: bytes, sn: int = 1) -> bytes:
    return _frame(TYPE_WRITE, CMD_SEND_EQ, payload, sn)


def _tlv(frame: bytes, start: int) -> tuple[dict[int, int], bool]:
    out: dict[int, int] = {}
    i = start
    end = len(frame) - 3
    while i < end:
        n = frame[i]
        if n == 0 or i + 1 + n > end:
            break
        if n == 2:
            out[frame[i + 1]] = frame[i + 2]
        i += 1 + n
    return out, i == end


def extract_frames(buf: bytearray) -> list[bytes]:
    """Extrae tramas completas HEADER..FOOTER desde un buffer de flujo."""
    frames: list[bytes] = []
    while True:
        start = buf.find(HEADER)
        if start < 0:
            buf.clear()
            break
        if start > 0:
            del buf[:start]
        end = buf.find(FOOTER, len(HEADER))
        if end < 0:
            break
        frame = bytes(buf[: end + len(FOOTER)])
        del buf[: end + len(FOOTER)]
        if len(frame) >= 11:
            frames.append(frame)
    return frames


def parse_battery(frame: bytes) -> int | None:
    if len(frame) < 11 or frame[:3] != HEADER or frame[-3:] != FOOTER:
        return None
    op = frame[4]
    if op == CMD_GET_INFO and frame[-6:-4] == b"\x02\x02":
        pct = frame[-4]
        return pct if 0 < pct <= 100 else None
    if op == CMD_EVENT:
        fields, ok = _tlv(frame, 8)
        if ok:
            pct = fields.get(0x05)
            return pct if pct is not None and 0 < pct <= 100 else None
    return None


def parse_run_info(frame: bytes) -> dict[int, int]:
    if len(frame) < 11 or frame[4] != CMD_GET_RUN_INFO:
        return {}
    out, ok = _tlv(frame, 9)
    if ok:
        return out
    fallback: dict[int, int] = {}
    i, end = 9, len(frame) - 3
    while i + 3 <= end:
        if frame[i] == 0x02:
            fallback[frame[i + 1]] = frame[i + 2]
            i += 3
        else:
            i += 1
    return fallback


def parse_event(frame: bytes) -> dict[int, int]:
    if len(frame) < 11 or frame[4] != CMD_EVENT:
        return {}
    out, ok = _tlv(frame, 8)
    return out if ok else {}


# ───────────────────────── Descubrimiento y PipeWire/BlueZ ─────────────────────────

def detect_usb_connected() -> bool:
    """Comprueba si los Haylou S40 están conectados como DAC USB (Jieli 3654:4a55)."""
    try:
        for p in Path("/proc/asound").glob("card*/usbid"):
            if p.read_text(encoding="utf-8", errors="ignore").strip().lower() == USB_ID:
                return True
        cards = Path("/proc/asound/cards")
        if cards.exists() and USB_SINK_HINT in cards.read_text(encoding="utf-8", errors="ignore"):
            return True
    except Exception:
        pass
    return False


def build_usb_state(prev_state: dict[str, Any] | None = None) -> dict[str, Any]:
    st = dict(prev_state) if prev_state else default_state(f"USB ({USB_ID})")
    rate = get_pipewire_sample_rate(USB_SINK_HINT) or 48000
    st.update({
        "connected": True,
        "transport": "usb",
        "address": f"USB ({USB_ID})",
        "battery": 0,
        "anc_mode": 0,
        "anc_key": "usb",
        "anc_label": "DAC USB",
        "anc_short": "USB",
        "anc_icon": "headphones",
        "codec": f"PCM {rate // 1000} kHz",
        "sample_rate": rate,
        "updated_at": time.time(),
        "error": None,
    })
    return st


def detect_haylou_address(cached_addr: str | None = None) -> str | None:
    """Devuelve el MAC cacheado si es válido; si no, descubre en BlueZ.

    La cache evita lanzar `bluetoothctl` en cada invocación del CLI.
    """
    if cached_addr and MAC_RE.match(cached_addr):
        return cached_addr.upper()
    try:
        res = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True, text=True, timeout=2.0,
        )
        for line in res.stdout.splitlines():
            parts = line.split()
            if "HAYLOU S40" in line.upper() and len(parts) >= 2 and MAC_RE.match(parts[1]):
                return parts[1].upper()
        if DEFAULT_ADDR:
            info = subprocess.run(
                ["bluetoothctl", "info", DEFAULT_ADDR],
                capture_output=True, text=True, timeout=2.0,
            )
            if "Connected: yes" in info.stdout:
                return DEFAULT_ADDR
    except Exception:
        pass
    return None


CODEC_LABELS = {
    "a2dp-sink": "AAC",
    "a2dp-sink-sbc_xq": "SBC-XQ",
    "a2dp-sink-sbc": "SBC",
    "a2dp-sink-ldac": "LDAC",
    "headset-head-unit": "HFP (mSBC)",
    "headset-head-unit-cvsd": "HFP (CVSD)",
}


def read_pactl_json() -> dict[str, Any]:
    """Una sola lectura estructurada de PipeWire (evita parsear texto localizado)."""
    try:
        res = subprocess.run(
            ["pactl", "-f", "json", "list"], capture_output=True, text=True, timeout=2.0
        )
        if res.returncode != 0:
            return {}
        data = json.loads(res.stdout)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_pipewire_audio_info(addr: str, pactl_data: dict[str, Any] | None = None) -> tuple[str, str, list[str]]:
    """Devuelve (active_profile, active_codec_label, available_profiles)."""
    data = pactl_data if pactl_data is not None else read_pactl_json()
    card_name = f"bluez_card.{addr.upper().replace(':', '_')}"
    card = next((c for c in data.get("cards", []) if c.get("name") == card_name), None)
    if card is None:
        return ("", "Desconectado", [])
    profiles = card.get("profiles") or {}
    avail = [
        pid for pid, prof in profiles.items()
        if prof.get("available") and pid.startswith(("a2dp-sink", "headset-head-unit"))
    ]
    active_profile = card.get("active_profile") or ""
    return (active_profile, CODEC_LABELS.get(active_profile, active_profile or "N/A"), avail)


def get_pipewire_sample_rate(node_hint: str, pactl_data: dict[str, Any] | None = None) -> int | None:
    """Frecuencia del sink cuyo nombre contiene `node_hint`, desde la salida JSON."""
    if not node_hint:
        return None
    data = pactl_data if pactl_data is not None else read_pactl_json()
    for sink in data.get("sinks", []):
        if node_hint not in (sink.get("name") or ""):
            continue
        spec = sink.get("sample_specification") or ""
        m = re.search(r"(\d+)\s*Hz", spec)
        if m:
            return int(m.group(1))
    return None


def set_pipewire_profile(addr: str, profile: str) -> tuple[bool, str]:
    card_name = f"bluez_card.{addr.upper().replace(':', '_')}"
    try:
        res = subprocess.run(
            ["pactl", "set-card-profile", card_name, profile],
            capture_output=True, text=True, timeout=3.0,
        )
        if res.returncode == 0:
            return (True, f"Perfil {profile} aplicado")
        return (False, res.stderr.strip() or f"Perfil {profile} no disponible")
    except Exception as exc:
        return (False, str(exc))


# ───────────────────────── Estado normalizado ─────────────────────────

def default_state(addr: str | None = None) -> dict[str, Any]:
    return {
        "connected": False,
        "transport": "none",
        "address": addr or DEFAULT_ADDR,
        "battery": 0,
        "anc_mode": 0,
        "anc_key": "off",
        "anc_label": "Off",
        "anc_short": "OFF",
        "anc_icon": "headphones",
        "game_mode": False,
        "wind_reduction": False,
        "multipoint": False,
        "spatial_mode": 2,
        "spatial_key": "off",
        "spatial_scene": 0,
        "shutdown_mode": 1,
        "eq_key": "default",
        "active_profile": "",
        "codec": "N/A",
        "sample_rate": 0,
        "available_profiles": [],
        "updated_at": 0.0,
        "error": None,
    }


def enrich_state(state: dict[str, Any], pactl_data: dict[str, Any] | None = None) -> dict[str, Any]:
    if state.get("transport") == "usb":
        return build_usb_state(state)

    anc = state.get("anc_mode", 0)
    key, _full, short, badge = ANC_BY_VAL.get(anc, ("off", "Off", "Off", "OFF"))
    state["anc_key"] = key
    state["anc_label"] = short
    state["anc_short"] = badge
    state["anc_icon"] = {
        0: "headphones",
        1: "noise_control_on",
        2: "hearing",
        4: "noise_aware",
    }.get(anc, "headphones")

    sp = state.get("spatial_mode", 2)
    state["spatial_key"] = SPATIAL_BY_VAL.get(sp, ("off", "Desactivado"))[0]

    addr = state.get("address") or DEFAULT_ADDR
    data = pactl_data if pactl_data is not None else read_pactl_json()
    # Una lectura fallida de pactl no es "sin tarjeta": conservar lo último conocido.
    if data:
        prof, codec, avail = get_pipewire_audio_info(addr, data)
        state["active_profile"] = prof
        state["codec"] = codec
        state["available_profiles"] = avail
        sink_hint = f"bluez_output.{addr.upper().replace(':', '_')}"
        state["sample_rate"] = get_pipewire_sample_rate(sink_hint, data) or 0
    state["updated_at"] = time.time()
    return state


def save_cache(state: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception:
        pass


def load_cache() -> dict[str, Any]:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default_state()


# ───────────────────────── Cliente RFCOMM ─────────────────────────

class HaylouRfcomm:
    """Gestiona una conexión RFCOMM con el Haylou S40 con lectura por buffer."""

    def __init__(self, addr: str = DEFAULT_ADDR) -> None:
        self.addr = addr.upper()
        self.sock: socket.socket | None = None
        self._buf = bytearray()
        self._io_lock = threading.Lock()

    def connect(self, retries: int = 2) -> None:
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
                s.settimeout(3.5)
                s.connect((self.addr, RFCOMM_CHANNEL))
                self.sock = s
                self._buf.clear()
                return
            except OSError as exc:
                last_err = exc
                try:
                    s.close()
                except Exception:
                    pass
                if attempt + 1 < retries:
                    time.sleep(0.35)
        raise OSError(f"No se pudo abrir canal RFCOMM con {self.addr}: {last_err}")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def send_and_collect(self, frame: bytes, wait_ops: set[int] | None = None, timeout: float = 1.6) -> list[bytes]:
        if self.sock is None:
            raise ConnectionError("Socket RFCOMM no conectado")
        collected: list[bytes] = []
        with self._io_lock:
            self.sock.settimeout(timeout)
            self.sock.send(frame)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = max(0.05, deadline - time.monotonic())
                self.sock.settimeout(remaining)
                try:
                    chunk = self.sock.recv(1024)
                except socket.timeout:
                    break
                if not chunk:
                    raise ConnectionError("Conexión RFCOMM cerrada por el auricular")
                self._buf.extend(chunk)
                frames = extract_frames(self._buf)
                if frames:
                    collected.extend(frames)
                    if wait_ops is None or any(f[4] in wait_ops for f in collected):
                        break
        return collected

    def read_once(self, timeout: float = 0.4) -> list[bytes]:
        if self.sock is None:
            return []
        with self._io_lock:
            self.sock.settimeout(timeout)
            try:
                chunk = self.sock.recv(1024)
            except socket.timeout:
                return []
            if not chunk:
                raise ConnectionError("Conexión RFCOMM cerrada por el auricular")
            self._buf.extend(chunk)
            return extract_frames(self._buf)


def apply_frames_to_state(frames: list[bytes], state: dict[str, Any]) -> None:
    for f in frames:
        pct = parse_battery(f)
        if pct is not None:
            state["battery"] = pct

        if f[4] == CMD_GET_RUN_INFO:
            info = parse_run_info(f)
            if ORD_SHUTDOWN in info and info[ORD_SHUTDOWN] in SHUTDOWN_BY_VAL:
                state["shutdown_mode"] = info[ORD_SHUTDOWN]
            if ORD_ANC in info and info[ORD_ANC] in ANC_BY_VAL:
                state["anc_mode"] = info[ORD_ANC]
            if ORD_GAME in info and info[ORD_GAME] in (0, 1):
                state["game_mode"] = bool(info[ORD_GAME])
            if ORD_WIND in info and info[ORD_WIND] in (0, 1):
                state["wind_reduction"] = bool(info[ORD_WIND])
            if ORD_MULTI in info and info[ORD_MULTI] in (0, 1):
                state["multipoint"] = bool(info[ORD_MULTI])
            if ORD_SPATIAL in info and info[ORD_SPATIAL] in SPATIAL_BY_VAL:
                state["spatial_mode"] = info[ORD_SPATIAL]
            if ORD_SCENE in info and info[ORD_SCENE] in SCENE_BY_VAL:
                state["spatial_scene"] = info[ORD_SCENE]
            if ORD_EQ in info and info[ORD_EQ] in EQ_BY_BASIC_BYTE:
                state["eq_key"] = EQ_BY_BASIC_BYTE[info[ORD_EQ]]
        elif f[4] == CMD_EVENT:
            ev = parse_event(f)
            if EVT_ANC in ev and ev[EVT_ANC] in ANC_BY_VAL:
                state["anc_mode"] = ev[EVT_ANC]


def query_full_state(rfcomm: HaylouRfcomm, prev_state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = dict(prev_state) if prev_state else default_state(rfcomm.addr)
    state["connected"] = True
    state["transport"] = "bluetooth"
    state["address"] = rfcomm.addr
    state["error"] = None

    f_bat = rfcomm.send_and_collect(build_get_battery(), wait_ops={CMD_GET_INFO}, timeout=1.5)
    f_all = rfcomm.send_and_collect(build_get_all(), wait_ops={CMD_GET_RUN_INFO}, timeout=1.8)
    apply_frames_to_state(f_bat + f_all, state)
    return enrich_state(state)


def execute_action_on_rfcomm(rfcomm: HaylouRfcomm | None, state: dict[str, Any], action: str, value: Any = None) -> tuple[bool, str, dict[str, Any]]:
    """Ejecuta una acción de protocolo o PipeWire y devuelve (ok, mensaje, estado_actualizado)."""
    addr = state.get("address") or DEFAULT_ADDR

    if action == "status":
        if rfcomm is None:
            return (False, "Auriculares desconectados", enrich_state(state))
        new_state = query_full_state(rfcomm, state)
        return (True, "Estado actualizado", new_state)

    if action == "codec":
        prof = CODEC_ALIASES.get(str(value).lower(), str(value))
        ok, msg = set_pipewire_profile(addr, prof)
        state = enrich_state(state)
        return (ok, msg, state)

    if rfcomm is None:
        return (False, "Haylou S40 no conectado por Bluetooth", enrich_state(state))

    # Acciones que hablan por RFCOMM
    msg = "Comando aplicado"
    if action == "anc":
        if str(value).lower() == "cycle":
            cur = state.get("anc_mode", 0)
            idx = ANC_CYCLE_ORDER.index(cur) if cur in ANC_CYCLE_ORDER else -1
            mode_val = ANC_CYCLE_ORDER[(idx + 1) % len(ANC_CYCLE_ORDER)]
        else:
            mode_val = ANC_ALIASES.get(str(value).lower())
            if mode_val is None:
                return (False, f"Modo ANC desconocido: {value}", state)
        rfcomm.send_and_collect(build_set_attr(ATTR_ANC, mode_val), wait_ops={CMD_SET_INFO}, timeout=1.2)
        state["anc_mode"] = mode_val
        msg = f"Control de ruido: {ANC_BY_VAL[mode_val][1]}"

    elif action == "game":
        new_val = (not state.get("game_mode", False)) if str(value).lower() == "toggle" else _parse_bool(value)
        rfcomm.send_and_collect(build_set_attr(ATTR_GAME, 1 if new_val else 0), wait_ops={CMD_SET_INFO}, timeout=1.2)
        state["game_mode"] = new_val
        msg = f"Modo Juego {'activado' if new_val else 'desactivado'}"

    elif action == "wind":
        new_val = (not state.get("wind_reduction", False)) if str(value).lower() == "toggle" else _parse_bool(value)
        rfcomm.send_and_collect(build_set_attr(ATTR_WIND, 1 if new_val else 0), wait_ops={CMD_SET_INFO}, timeout=1.2)
        state["wind_reduction"] = new_val
        msg = f"Reducción de viento {'activada' if new_val else 'desactivada'}"

    elif action == "multi":
        new_val = (not state.get("multipoint", False)) if str(value).lower() == "toggle" else _parse_bool(value)
        rfcomm.send_and_collect(build_set_attr(ATTR_MULTI, 1 if new_val else 0), wait_ops={CMD_SET_INFO}, timeout=1.2)
        state["multipoint"] = new_val
        msg = f"Multipunto {'activado' if new_val else 'desactivado'}"

    elif action == "spatial":
        sp_val = SPATIAL_ALIASES.get(str(value).lower())
        if sp_val is None:
            return (False, f"Modo espacial inválido: {value}", state)
        rfcomm.send_and_collect(build_set_attr(ATTR_SPATIAL, sp_val), wait_ops={CMD_SET_INFO}, timeout=1.2)
        state["spatial_mode"] = sp_val
        msg = f"Audio Espacial: {SPATIAL_BY_VAL[sp_val][1]}"

    elif action == "scene":
        sc_val = SCENE_ALIASES.get(str(value).lower())
        if sc_val is None:
            return (False, f"Escenario inválido: {value}", state)
        rfcomm.send_and_collect(build_set_attr(ATTR_SCENE, sc_val), wait_ops={CMD_SET_INFO}, timeout=1.2)
        state["spatial_scene"] = sc_val
        msg = f"Escenario espacial: {SCENE_BY_VAL[sc_val][1]}"

    elif action == "shutdown":
        sd_val = SHUTDOWN_ALIASES.get(str(value).lower())
        if sd_val is None:
            return (False, f"Tiempo de autoapagado inválido: {value}", state)
        rfcomm.send_and_collect(build_set_attr(ATTR_SHUTDOWN, sd_val), wait_ops={CMD_SET_INFO}, timeout=1.2)
        state["shutdown_mode"] = sd_val
        msg = f"Autoapagado: {SHUTDOWN_BY_VAL[sd_val][1]}"

    elif action == "eq":
        opt = EQ_BY_KEY.get(str(value).lower())
        if opt is None:
            return (False, f"Preset EQ desconocido: {value}", state)
        key, label, kind, data = opt
        if kind == "basic":
            rfcomm.send_and_collect(build_set_eq_basic(data), wait_ops={CMD_SET_CONFIG}, timeout=1.2)
        else:
            rfcomm.send_and_collect(build_send_eq_market(data), wait_ops={CMD_SEND_EQ}, timeout=1.5)
        state["eq_key"] = key
        msg = f"EQ hardware: {label}"

    elif action == "ldac":
        new_val = _parse_bool(value)
        if new_val:
            # En S40, multipunto y espacial bloquean LDAC
            rfcomm.send_and_collect(build_set_attr(ATTR_MULTI, 0), wait_ops={CMD_SET_INFO}, timeout=1.0)
            rfcomm.send_and_collect(build_set_attr(ATTR_SPATIAL, 2), wait_ops={CMD_SET_INFO}, timeout=1.0)
        rfcomm.send_and_collect(build_set_attr(ATTR_LDAC, 1 if new_val else 0), wait_ops={CMD_SET_INFO}, timeout=1.2)
        msg = f"LDAC en firmware {'habilitado (puede reiniciar los auriculares)' if new_val else 'deshabilitado'}"
        state = enrich_state(state)
        return (True, msg, state)

    elif action == "scene_preset":
        # Escenas compuestas rápidas
        preset = str(value).lower()
        if preset == "focus":
            rfcomm.send_and_collect(build_set_attr(ATTR_ANC, 1), wait_ops={CMD_SET_INFO}, timeout=1.0)
            rfcomm.send_and_collect(build_set_attr(ATTR_WIND, 1), wait_ops={CMD_SET_INFO}, timeout=1.0)
            rfcomm.send_and_collect(build_set_eq_basic(0x07), wait_ops={CMD_SET_CONFIG}, timeout=1.0)
            state["eq_key"] = "soft"
            msg = "Escena Enfoque aplicada (ANC + Antiviento + EQ Soft)"
        elif preset == "music":
            rfcomm.send_and_collect(build_set_attr(ATTR_ANC, 1), wait_ops={CMD_SET_INFO}, timeout=1.0)
            rfcomm.send_and_collect(build_set_attr(ATTR_SPATIAL, 2), wait_ops={CMD_SET_INFO}, timeout=1.0)
            rfcomm.send_and_collect(build_set_eq_basic(0x06), wait_ops={CMD_SET_CONFIG}, timeout=1.0)
            state["eq_key"] = "bass"
            msg = "Escena Música Hi-Fi aplicada (ANC + Espacial Off + EQ Bass)"
        elif preset == "gaming":
            rfcomm.send_and_collect(build_set_attr(ATTR_GAME, 1), wait_ops={CMD_SET_INFO}, timeout=1.0)
            rfcomm.send_and_collect(build_set_attr(ATTR_ANC, 0), wait_ops={CMD_SET_INFO}, timeout=1.0)
            msg = "Escena Gaming aplicada (Modo Juego On + Normal)"
        elif preset == "street":
            rfcomm.send_and_collect(build_set_attr(ATTR_ANC, 2), wait_ops={CMD_SET_INFO}, timeout=1.0)
            rfcomm.send_and_collect(build_set_attr(ATTR_WIND, 1), wait_ops={CMD_SET_INFO}, timeout=1.0)
            msg = "Escena Calle aplicada (Sonido Ambiente + Antiviento)"
        else:
            return (False, f"Escena rápida desconocida: {value}", state)

    else:
        return (False, f"Acción no soportada: {action}", state)

    # Confirmar estado real tras escritura (excepto LDAC que puede reiniciar)
    try:
        time.sleep(0.08)
        state = query_full_state(rfcomm, state)
    except Exception:
        state = enrich_state(state)
    return (True, msg, state)


def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "on", "yes", "si", "sí")


# ───────────────────────── Cliente UNIX / Modo CLI ─────────────────────────

def try_ipc_request(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Delega una petición a la instancia GUI o daemon activa vía socket UNIX."""
    if not SOCKET_PATH.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
            cli.settimeout(8.0)
            cli.connect(str(SOCKET_PATH))
            cli.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                data = cli.recv(4096)
                if not data:
                    break
                chunks.append(data)
                if b"\n" in data:
                    break
            raw = b"".join(chunks).decode("utf-8").strip()
            return json.loads(raw) if raw else None
    except Exception:
        return None


def handle_ipc_connection(conn: socket.socket, owner: Any, run_action: Any = None, on_state: Any = None) -> None:
    """Atiende una petición IPC. `owner` expone .lock, .state y .rfcomm.

    Con `run_action` el dueño ejecuta la acción (y guarda su cache); sin él se
    usa el ejecutor directo. Compartido por el daemon y la GUI para que ambos
    respondan igual.
    """
    with conn:
        try:
            raw = conn.recv(4096).decode("utf-8").strip()
            req = json.loads(raw) if raw else {"action": "status"}
            action = req.get("action", "status")
            value = req.get("value")
            with owner.lock:
                if action == "status":
                    owner.state = enrich_state(dict(owner.state))
                    connected = bool(owner.state.get("connected"))
                    resp = {
                        "ok": connected,
                        "message": "Estado vivo" if connected else "Auricular desconectado",
                        "state": owner.state,
                    }
                elif run_action is not None:
                    ok, msg, new_st = run_action(action, value)
                    owner.state = new_st
                    resp = {"ok": ok, "message": msg, "state": new_st}
                else:
                    ok, msg, new_st = execute_action_on_rfcomm(
                        owner.rfcomm, dict(owner.state), action, value
                    )
                    owner.state = new_st
                    save_cache(owner.state)
                    resp = {"ok": ok, "message": msg, "state": new_st}
            if on_state is not None:
                on_state(dict(owner.state))
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as exc:
            try:
                conn.sendall((json.dumps({"ok": False, "message": str(exc)}) + "\n").encode("utf-8"))
            except Exception:
                pass


def run_cli_oneshot(action: str, value: Any = None) -> tuple[bool, str, dict[str, Any]]:
    # 1. Intentar delegar a GUI activa
    ipc_resp = try_ipc_request({"action": action, "value": value})
    if ipc_resp is not None:
        return (bool(ipc_resp.get("ok", True)), ipc_resp.get("message", ""), ipc_resp.get("state", {}))

    # 2. Detectar si el auricular está conectado en BlueZ o por USB
    cached = load_cache()
    addr = detect_haylou_address(cached.get("address"))
    if not addr:
        if detect_usb_connected():
            st = build_usb_state(cached)
            save_cache(st)
            if action == "status":
                return (True, "Conectado por DAC USB", st)
            return (False, "En modo USB el control RFCOMM Bluetooth está inactivo", st)

        st = default_state(cached.get("address", DEFAULT_ADDR))
        st["connected"] = False
        st["transport"] = "none"
        st["error"] = "Haylou S40 no conectado"
        save_cache(st)
        return (False, "Haylou S40 no conectado por Bluetooth ni USB", st)

    # 3. Lock exclusivo para evitar colisión entre invocaciones paralelas
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        # Acción solo PipeWire no necesita abrir RFCOMM si solo es codec
        if action == "codec":
            st = dict(cached)
            st["connected"] = True
            st["address"] = addr
            ok, msg, st = execute_action_on_rfcomm(None, st, action, value)
            save_cache(st)
            return (ok, msg, st)

        rfcomm = HaylouRfcomm(addr)
        try:
            rfcomm.connect(retries=2)
            st = dict(cached)
            st["address"] = addr
            if action == "status":
                st = query_full_state(rfcomm, st)
                ok, msg = True, "Estado leído"
            else:
                # Leer estado previo si hace toggle o cycle
                if str(value).lower() in ("toggle", "cycle"):
                    st = query_full_state(rfcomm, st)
                ok, msg, st = execute_action_on_rfcomm(rfcomm, st, action, value)
            save_cache(st)
            return (ok, msg, st)
        except Exception as exc:
            st = dict(cached, address=addr, error=str(exc))
            if detect_usb_connected():
                st = build_usb_state(st)
                save_cache(st)
                if action == "status":
                    return (True, "Conectado por DAC USB", st)
                return (False, "En modo USB el control RFCOMM Bluetooth está inactivo", st)
            st = enrich_state({**st, "connected": False, "transport": "none"})
            save_cache(st)
            return (False, str(exc), st)
        finally:
            rfcomm.close()
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def print_human_status(st: dict[str, Any]) -> None:
    if not st.get("connected"):
        print("Haylou S40: desconectado")
        return
    if st.get("transport") == "usb":
        print(f"Dispositivo      : HAYLOU S40 (USB {USB_ID})")
        print("Modo             : DAC USB Directo (Cargando)")
        print(f"Audio PipeWire   : {st.get('codec', 'PCM')} Full-Duplex (Salida Estéreo + Micrófono)")
        return
    eq_label = EQ_BY_KEY.get(st.get("eq_key", "default"), ("default", "Predeterminado"))[1]
    sp_label = SPATIAL_BY_VAL.get(st.get("spatial_mode", 2), ("off", "Desactivado"))[1]
    sc_label = SCENE_BY_VAL.get(st.get("spatial_scene", 0), ("music", "Música"))[1]
    print(f"Dispositivo      : HAYLOU S40 ({st.get('address')})")
    print(f"Batería          : {st.get('battery')}%")
    print(f"Control de ruido : {st.get('anc_label')} [{st.get('anc_short')}]")
    print(f"Códec PipeWire   : {st.get('codec')} ({st.get('active_profile')})")
    print(f"Modo Juego       : {'Activado' if st.get('game_mode') else 'Desactivado'}")
    print(f"Antiviento       : {'Activado' if st.get('wind_reduction') else 'Desactivado'}")
    print(f"Multipunto       : {'Activado' if st.get('multipoint') else 'Desactivado'}")
    print(f"Audio Espacial   : {sp_label} (Escenario: {sc_label})")
    print(f"Autoapagado      : {SHUTDOWN_BY_VAL.get(st.get('shutdown_mode', 1), ('1h', '1 hora'))[1]}")
    print(f"EQ Hardware      : {eq_label}")


# ───────────────────────── Daemon headless (systemd --user) ─────────────────────────

class HaylouDaemon:
    """Mantiene el RFCOMM abierto y sirve el socket UNIX sin necesidad de GTK."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = load_cache()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.rfcomm: HaylouRfcomm | None = None

    def serve(self) -> int:
        # El socket puede estar ocupado por una GUI que se está cerrando: reintentar.
        for _ in range(5):
            if try_ipc_request({"action": "status"}) is None:
                break
            time.sleep(0.6)
        else:
            print("haylou-s40: ya hay una instancia activa", file=sys.stderr)
            return 1
        ipc = threading.Thread(target=self._ipc_worker, daemon=True)
        ipc.start()
        self._poll_loop()
        self.shutdown()
        ipc.join(timeout=1.5)
        self._cleanup_socket()
        return 0

    def _cleanup_socket(self) -> None:
        if SOCKET_PATH.exists():
            try:
                SOCKET_PATH.unlink()
            except Exception:
                pass

    def _poll_loop(self) -> None:
        last_poll = 0.0
        while not self.stop_event.is_set():
            if self.rfcomm is None:
                addr = detect_haylou_address(self.state.get("address"))
                if addr:
                    rf = HaylouRfcomm(addr)
                    try:
                        rf.connect(retries=2)
                        with self.lock:
                            self.rfcomm = rf
                            self.state = query_full_state(rf, self.state)
                            save_cache(self.state)
                        last_poll = time.monotonic()
                    except Exception:
                        rf.close()
                if self.rfcomm is None:
                    with self.lock:
                        if detect_usb_connected():
                            self.state = build_usb_state(self.state)
                        else:
                            self.state = {**self.state, "connected": False, "transport": "none"}
                        save_cache(self.state)
                    time.sleep(2.0)
                    continue

            try:
                if time.monotonic() - last_poll > 15.0:
                    with self.lock:
                        if self.rfcomm is not None:
                            self.state = query_full_state(self.rfcomm, self.state)
                            save_cache(self.state)
                    last_poll = time.monotonic()

                with self.lock:
                    if self.rfcomm is not None:
                        frames = self.rfcomm.read_once(timeout=0.15)
                        if frames:
                            apply_frames_to_state(frames, self.state)
                            self.state = enrich_state(self.state)
                            save_cache(self.state)
            except Exception:
                with self.lock:
                    if self.rfcomm is not None:
                        self.rfcomm.close()
                        self.rfcomm = None
                    self.state = {**self.state, "connected": False, "transport": "none"}
                    save_cache(self.state)

            time.sleep(0.25)

    def _ipc_worker(self) -> None:
        try:
            if SOCKET_PATH.exists():
                SOCKET_PATH.unlink()
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(SOCKET_PATH))
            srv.listen(8)
            srv.settimeout(0.5)
        except Exception:
            return

        while not self.stop_event.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            handle_ipc_connection(conn, self)

        try:
            srv.close()
        except Exception:
            pass
        if SOCKET_PATH.exists():
            try:
                SOCKET_PATH.unlink()
            except Exception:
                pass

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.rfcomm is not None:
                self.rfcomm.close()
                self.rfcomm = None


def serve_forever() -> int:
    daemon = HaylouDaemon()

    def _handle_signal(*_args: Any) -> None:
        daemon.shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

    print("haylou-s40: daemon activo", file=sys.stderr, flush=True)
    return daemon.serve()


# ───────────────────────── Interfaz Gráfica (GTK4 + Libadwaita) ─────────────────────────

def launch_gui() -> int:
    # Si hay un daemon (o GUI) activo, esta instancia actúa como cliente por IPC:
    # no abre RFCOMM y no toca el socket, así ambos conviven sin colisiones.
    remote = try_ipc_request({"action": "status"}) is not None

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk

    class HaylouWindow(Adw.ApplicationWindow):
        def __init__(self, remote: bool = False, **kwargs) -> None:
            super().__init__(**kwargs)
            self.remote = remote
            self.state: dict[str, Any] = load_cache()
            self.rfcomm: HaylouRfcomm | None = None
            self._rf_lock = threading.RLock()
            self.lock = self._rf_lock  # alias usado por handle_ipc_connection
            self._stop_event = threading.Event()
            self._syncing_ui = False

            self.set_title("Haylou S40")
            self.set_default_size(540, 720)
            self.set_size_request(340, 420)

            self._build_ui()
            self.connect("close-request", self._on_close_request)

            # En modo remoto el daemon es dueño del socket y del RFCOMM.
            if not self.remote:
                threading.Thread(target=self._ipc_worker, daemon=True).start()
            threading.Thread(target=self._rfcomm_worker, daemon=True).start()

        def _build_ui(self) -> None:
            self.toast_overlay = Adw.ToastOverlay()
            self.set_content(self.toast_overlay)

            main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.toast_overlay.set_child(main_box)

            header = Adw.HeaderBar()
            self.win_title = Adw.WindowTitle(
                title="Haylou S40",
                subtitle="Conectando por RFCOMM...",
            )
            header.set_title_widget(self.win_title)

            refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
            refresh_btn.set_tooltip_text("Actualizar estado")
            refresh_btn.connect("clicked", lambda *_: self._dispatch_action("status", None))
            header.pack_start(refresh_btn)

            self.spinner = Gtk.Spinner()
            self.spinner.set_visible(False)
            header.pack_end(self.spinner)

            main_box.append(header)

            self.usb_banner = Adw.Banner(
                title="Modo DAC USB activo (48 kHz Full-Duplex) · Ajustes RFCOMM disponibles por Bluetooth",
                revealed=False,
            )
            main_box.append(self.usb_banner)

            scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            main_box.append(scroller)

            clamp = Adw.Clamp(maximum_size=520, tightening_threshold=420)
            scroller.set_child(clamp)

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            content.set_margin_top(18)
            content.set_margin_bottom(24)
            content.set_margin_start(16)
            content.set_margin_end(16)
            clamp.set_child(content)

            # ── Grupo 1: Escenas rápidas (pills) ──
            self.scenes_group = Adw.PreferencesGroup(
                title="Escenas rápidas",
                description="Combos instantáneos de cancelación de ruido, latencia y ecualización",
            )
            scenes_flow = Gtk.FlowBox()
            scenes_flow.set_selection_mode(Gtk.SelectionMode.NONE)
            scenes_flow.set_max_children_per_line(4)
            scenes_flow.set_min_children_per_line(2)
            scenes_flow.set_column_spacing(8)
            scenes_flow.set_row_spacing(8)
            scenes_flow.set_homogeneous(True)

            quick_scenes = [
                ("Enfoque", "focus"),
                ("Música Hi-Fi", "music"),
                ("Gaming", "gaming"),
                ("Calle / Amb.", "street"),
            ]
            for label, key in quick_scenes:
                btn = Gtk.Button(label=label)
                btn.add_css_class("pill")
                btn.connect("clicked", lambda _, k=key: self._dispatch_action("scene_preset", k))
                scenes_flow.append(btn)

            self.scenes_group.add(scenes_flow)
            content.append(self.scenes_group)

            # ── Grupo 2: Control de ruido ──
            self.anc_group = Adw.PreferencesGroup(
                title="Control de ruido",
                description="Modos de cancelación activa y transparencia",
            )
            anc_card = Gtk.FlowBox()
            anc_card.set_selection_mode(Gtk.SelectionMode.NONE)
            anc_card.set_max_children_per_line(4)
            anc_card.set_min_children_per_line(2)
            anc_card.set_column_spacing(6)
            anc_card.set_row_spacing(6)
            anc_card.set_homogeneous(True)
            anc_card.set_margin_bottom(8)

            self.anc_buttons: dict[int, Gtk.ToggleButton] = {}
            anc_icons = {
                0: "audio-speakers-symbolic",
                1: "audio-headphones-symbolic",
                2: "audio-input-microphone-symbolic",
                4: "weather-windy-symbolic",
            }
            for mode_val, _k, _full, short_name, _badge in ANC_MODES:
                tbtn = Gtk.ToggleButton()
                tbtn.set_cursor_from_name("pointer")
                inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                inner.set_margin_top(8)
                inner.set_margin_bottom(8)
                icon = Gtk.Image.new_from_icon_name(anc_icons[mode_val])
                icon.set_pixel_size(18)
                lbl = Gtk.Label(label=short_name)
                lbl.add_css_class("caption-heading")
                inner.append(icon)
                inner.append(lbl)
                tbtn.set_child(inner)
                tbtn.connect("toggled", self._on_anc_button_toggled, mode_val)
                self.anc_buttons[mode_val] = tbtn
                anc_card.append(tbtn)

            self.anc_group.add(anc_card)

            self.wind_row = Adw.SwitchRow(
                title="Reducción de ruido de viento",
                subtitle="Atenúa el flujo de aire en exteriores sobre los micrófonos",
            )
            self.wind_row.connect("notify::active", self._on_switch_toggled, "wind")
            self.anc_group.add(self.wind_row)
            content.append(self.anc_group)

            # ── Grupo 3: Acústica y baja latencia ──
            self.audio_group = Adw.PreferencesGroup(
                title="Acústica y Baja Latencia",
                description="Ajustes DSP en el hardware de los auriculares y perfil PipeWire",
            )

            self.game_row = Adw.SwitchRow(
                title="Modo Juego (Baja latencia)",
                subtitle="Minimiza el retardo Bluetooth para gaming y video",
            )
            self.game_row.connect("notify::active", self._on_switch_toggled, "game")
            self.audio_group.add(self.game_row)

            self.eq_keys = [opt[0] for opt in EQ_ALL_OPTIONS]
            self.eq_row = Adw.ComboRow(
                title="Ecualizador Hardware (DSP)",
                subtitle="Perfiles nativos + presets paramétricos Sound Market",
                model=Gtk.StringList.new([opt[1] for opt in EQ_ALL_OPTIONS]),
            )
            self.eq_row.connect("notify::selected", self._on_eq_selected)
            self.audio_group.add(self.eq_row)

            self.spatial_vals = [m[0] for m in SPATIAL_MODES]
            self.spatial_row = Adw.ComboRow(
                title="Audio Espacial 360°",
                subtitle="Incompatibles simultáneamente con LDAC",
                model=Gtk.StringList.new([m[2] for m in SPATIAL_MODES]),
            )
            self.spatial_row.connect("notify::selected", self._on_spatial_selected)
            self.audio_group.add(self.spatial_row)

            self.scene_vals = [s[0] for s in SCENES]
            self.scene_row = Adw.ComboRow(
                title="Escenario Espacial",
                model=Gtk.StringList.new([s[1] for s in SCENES]),
            )
            self.scene_row.connect("notify::selected", self._on_scene_selected)
            self.audio_group.add(self.scene_row)

            self.codec_profiles: list[tuple[str, str]] = []
            self.codec_row = Adw.ComboRow(
                title="Códec Bluetooth en Linux (PipeWire)",
                subtitle="Controlado directamente por el perfil BlueZ de esta PC",
                model=Gtk.StringList.new(["Cargando..."]),
            )
            self.codec_row.connect("notify::selected", self._on_codec_selected)
            self.audio_group.add(self.codec_row)

            content.append(self.audio_group)

            # ── Grupo 4: Conectividad del dispositivo ──
            self.conn_group = Adw.PreferencesGroup(
                title="Conectividad del dispositivo",
                description="Multipunto y códec de alta resolución en el firmware",
            )

            self.multi_row = Adw.SwitchRow(
                title="Conexión Multipunto (Dual-Device)",
                subtitle="Permite enlace simultáneo con PC y teléfono",
            )
            self.multi_row.connect("notify::active", self._on_switch_toggled, "multi")
            self.conn_group.add(self.multi_row)

            self.shutdown_vals = [m[0] for m in SHUTDOWN_MODES]
            self.shutdown_row = Adw.ComboRow(
                title="Temporizador de autoapagado",
                subtitle="Apaga los auriculares tras permanecer sin conexión",
                model=Gtk.StringList.new([m[2] for m in SHUTDOWN_MODES]),
            )
            self.shutdown_row.connect("notify::selected", self._on_shutdown_selected)
            self.conn_group.add(self.shutdown_row)

            ldac_row = Adw.ActionRow(
                title="Modo Hi-Res LDAC en Firmware",
                subtitle="Desactiva multipunto y audio espacial; puede reiniciar los auriculares",
            )
            ldac_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            ldac_box.set_valign(Gtk.Align.CENTER)
            btn_ldac_on = Gtk.Button(label="Activar LDAC")
            btn_ldac_on.add_css_class("suggested-action")
            btn_ldac_on.connect("clicked", lambda *_: self._dispatch_action("ldac", True))
            btn_ldac_off = Gtk.Button(label="Apagar")
            btn_ldac_off.connect("clicked", lambda *_: self._dispatch_action("ldac", False))
            ldac_box.append(btn_ldac_on)
            ldac_box.append(btn_ldac_off)
            ldac_row.add_suffix(ldac_box)
            self.conn_group.add(ldac_row)

            content.append(self.conn_group)
            self._bt_groups = [self.scenes_group, self.anc_group, self.audio_group, self.conn_group]

            # Sincronización inicial con cache si existe
            self._sync_ui_from_state()

        def _toast(self, message: str) -> None:
            if message:
                self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=2))

        def _sync_ui_from_state(self, state_snapshot: dict[str, Any] | None = None) -> bool:
            self._syncing_ui = True
            try:
                if state_snapshot is not None:
                    st = state_snapshot
                else:
                    with self._rf_lock:
                        st = dict(self.state)
                is_usb = st.get("connected") and st.get("transport") == "usb"
                is_bt = bool(st.get("connected")) and not is_usb
                self.usb_banner.set_revealed(bool(is_usb))
                for grp in self._bt_groups:
                    grp.set_sensitive(is_bt)

                if is_usb:
                    self.win_title.set_subtitle(
                        "Conectado por USB • PCM 48kHz Full-Duplex • Cargando"
                    )
                elif st.get("connected"):
                    self.win_title.set_subtitle(
                        f"Conectado • Batería {st.get('battery', 0)}% • Códec {st.get('codec', 'N/A')}"
                    )
                else:
                    self.win_title.set_subtitle("Desconectado — esperando conexión Bluetooth o USB")

                anc_val = st.get("anc_mode", 0)
                for val, btn in self.anc_buttons.items():
                    active = val == anc_val
                    btn.set_active(active)
                    if active:
                        btn.add_css_class("suggested-action")
                    else:
                        btn.remove_css_class("suggested-action")

                self.wind_row.set_active(bool(st.get("wind_reduction", False)))
                self.game_row.set_active(bool(st.get("game_mode", False)))
                self.multi_row.set_active(bool(st.get("multipoint", False)))

                eq_key = st.get("eq_key", "default")
                if eq_key in self.eq_keys:
                    self.eq_row.set_selected(self.eq_keys.index(eq_key))

                sp_val = st.get("spatial_mode", 2)
                if sp_val in self.spatial_vals:
                    self.spatial_row.set_selected(self.spatial_vals.index(sp_val))
                self.scene_row.set_visible(sp_val != 2)

                sc_val = st.get("spatial_scene", 0)
                if sc_val in self.scene_vals:
                    self.scene_row.set_selected(self.scene_vals.index(sc_val))

                sd_val = st.get("shutdown_mode", 1)
                if sd_val in self.shutdown_vals:
                    self.shutdown_row.set_selected(self.shutdown_vals.index(sd_val))

                self.codec_row.set_visible(not is_usb)

                avail = st.get("available_profiles", [])
                active_prof = st.get("active_profile", "")
                options: list[tuple[str, str]] = []
                for _k, prof_id, label, _short in CODEC_PROFILES:
                    if prof_id in avail or prof_id == active_prof:
                        options.append((prof_id, label))
                if not options:
                    options = [("a2dp-sink", "AAC (Alta fidelidad)"), ("a2dp-sink-sbc_xq", "SBC-XQ"), ("a2dp-sink-sbc", "SBC")]

                if options != self.codec_profiles:
                    self.codec_profiles = options
                    self.codec_row.set_model(Gtk.StringList.new([lbl for _p, lbl in options]))

                prof_ids = [p for p, _l in self.codec_profiles]
                if active_prof in prof_ids:
                    self.codec_row.set_selected(prof_ids.index(active_prof))
            finally:
                self._syncing_ui = False
            return False

        # ── Callbacks UI ──

        def _on_anc_button_toggled(self, btn: Gtk.ToggleButton, mode_val: int) -> None:
            if self._syncing_ui:
                return
            if not btn.get_active():
                # Impedir desactivar manualmente todos los toggle; forzar estado actual
                self._sync_ui_from_state()
                return
            key = ANC_BY_VAL[mode_val][0]
            self._dispatch_action("anc", key)

        def _on_switch_toggled(self, row: Adw.SwitchRow, _pspec: Any, action: str) -> None:
            if self._syncing_ui:
                return
            self._dispatch_action(action, row.get_active())

        def _on_eq_selected(self, row: Adw.ComboRow, _pspec: Any) -> None:
            if self._syncing_ui:
                return
            idx = row.get_selected()
            if 0 <= idx < len(self.eq_keys):
                self._dispatch_action("eq", self.eq_keys[idx])

        def _on_spatial_selected(self, row: Adw.ComboRow, _pspec: Any) -> None:
            if self._syncing_ui:
                return
            idx = row.get_selected()
            if 0 <= idx < len(self.spatial_vals):
                key = SPATIAL_BY_VAL[self.spatial_vals[idx]][0]
                self._dispatch_action("spatial", key)

        def _on_scene_selected(self, row: Adw.ComboRow, _pspec: Any) -> None:
            if self._syncing_ui:
                return
            idx = row.get_selected()
            if 0 <= idx < len(self.scene_vals):
                key = SCENE_BY_VAL[self.scene_vals[idx]][0]
                self._dispatch_action("scene", key)

        def _on_shutdown_selected(self, row: Adw.ComboRow, _pspec: Any) -> None:
            if self._syncing_ui:
                return
            idx = row.get_selected()
            if 0 <= idx < len(self.shutdown_vals):
                key = SHUTDOWN_BY_VAL[self.shutdown_vals[idx]][0]
                self._dispatch_action("shutdown", key)

        def _on_codec_selected(self, row: Adw.ComboRow, _pspec: Any) -> None:
            if self._syncing_ui:
                return
            idx = row.get_selected()
            if 0 <= idx < len(self.codec_profiles):
                prof_id = self.codec_profiles[idx][0]
                self._dispatch_action("codec", prof_id)

        def _dispatch_action(self, action: str, value: Any) -> None:
            self.spinner.set_visible(True)
            self.spinner.start()
            def run() -> None:
                ok, msg, new_st = self._run_locked_action(action, value)
                GLib.idle_add(self._on_action_done, ok, msg, new_st)

            threading.Thread(target=run, daemon=True).start()

        def _on_action_done(self, ok: bool, msg: str, new_st: dict[str, Any]) -> bool:
            self.spinner.stop()
            self.spinner.set_visible(False)
            self.state = new_st
            self._sync_ui_from_state(new_st)
            if msg and (not ok or msg != "Estado actualizado"):
                self._toast(msg)
            return False

        def _run_locked_action(self, action: str, value: Any) -> tuple[bool, str, dict[str, Any]]:
            if self.remote:
                resp = try_ipc_request({"action": action, "value": value})
                if resp is None:
                    return (False, "El daemon no responde", dict(self.state))
                new_st = resp.get("state") or dict(self.state)
                with self._rf_lock:
                    self.state = new_st
                return (bool(resp.get("ok", True)), resp.get("message", ""), dict(new_st))
            with self._rf_lock:
                if self.rfcomm is None and detect_usb_connected():
                    self.state = build_usb_state(self.state)
                    save_cache(self.state)
                    if action == "status":
                        return (True, "Estado actualizado", dict(self.state))
                    return (False, "En modo USB el control RFCOMM Bluetooth está inactivo", dict(self.state))
                ok, msg, new_st = execute_action_on_rfcomm(self.rfcomm, dict(self.state), action, value)
                self.state = new_st
                save_cache(self.state)
                return (ok, msg, dict(self.state))

        # ── Hilos de RFCOMM y servidor UNIX IPC ──

        def _remote_worker(self) -> None:
            """Modo cliente: refleja el estado del daemon sin abrir RFCOMM."""
            misses = 0
            while not self._stop_event.is_set():
                resp = try_ipc_request({"action": "status"})
                state = resp.get("state") if isinstance(resp, dict) else None
                if isinstance(state, dict):
                    misses = 0
                    with self._rf_lock:
                        self.state = state
                    GLib.idle_add(self._sync_ui_from_state, dict(state))
                else:
                    misses += 1
                    if misses == 3:
                        with self._rf_lock:
                            self.state = {**self.state, "connected": False, "transport": "none"}
                        GLib.idle_add(self._sync_ui_from_state, dict(self.state))
                time.sleep(2.0)

        def _rfcomm_worker(self) -> None:
            if self.remote:
                self._remote_worker()
                return
            last_poll = 0.0
            while not self._stop_event.is_set():
                if self.rfcomm is None:
                    addr = detect_haylou_address(self.state.get("address"))
                    if addr:
                        snap = None
                        with self._rf_lock:
                            rf = HaylouRfcomm(addr)
                            try:
                                rf.connect(retries=2)
                                self.rfcomm = rf
                                self.state = query_full_state(rf, self.state)
                                save_cache(self.state)
                                snap = dict(self.state)
                                last_poll = time.monotonic()
                            except Exception:
                                rf.close()
                                self.rfcomm = None
                        if snap is not None:
                            GLib.idle_add(self._sync_ui_from_state, snap)
                    if self.rfcomm is None:
                        snap = None
                        if detect_usb_connected():
                            with self._rf_lock:
                                if not self.state.get("connected") or self.state.get("transport") != "usb":
                                    self.state = build_usb_state(self.state)
                                    save_cache(self.state)
                                    snap = dict(self.state)
                        elif self.state.get("connected"):
                            with self._rf_lock:
                                self.state["connected"] = False
                                self.state["transport"] = "none"
                                save_cache(self.state)
                                snap = dict(self.state)
                        if snap is not None:
                            GLib.idle_add(self._sync_ui_from_state, snap)
                        time.sleep(2.0)
                        continue

                # Escuchar avisos espontáneos (botón físico) y refrescar cada 15s
                try:
                    if time.monotonic() - last_poll > 15.0:
                        snap = None
                        with self._rf_lock:
                            if self.rfcomm is not None:
                                self.state = query_full_state(self.rfcomm, self.state)
                                save_cache(self.state)
                                snap = dict(self.state)
                        if snap is not None:
                            GLib.idle_add(self._sync_ui_from_state, snap)
                        last_poll = time.monotonic()

                    # Polling corto no bloqueante largo para no retener el lock
                    snap = None
                    with self._rf_lock:
                        if self.rfcomm is not None:
                            frames = self.rfcomm.read_once(timeout=0.15)
                            if frames:
                                apply_frames_to_state(frames, self.state)
                                self.state = enrich_state(self.state)
                                save_cache(self.state)
                                snap = dict(self.state)
                    if snap is not None:
                        GLib.idle_add(self._sync_ui_from_state, snap)
                except Exception:
                    with self._rf_lock:
                        if self.rfcomm is not None:
                            self.rfcomm.close()
                            self.rfcomm = None
                        self.state["connected"] = False
                        self.state["transport"] = "none"
                        save_cache(self.state)
                        snap = dict(self.state)
                    GLib.idle_add(self._sync_ui_from_state, snap)

                time.sleep(0.25)

        def _ipc_worker(self) -> None:
            try:
                if SOCKET_PATH.exists():
                    SOCKET_PATH.unlink()
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(str(SOCKET_PATH))
                srv.listen(8)
                srv.settimeout(0.5)
            except Exception:
                return

            while not self._stop_event.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                handle_ipc_connection(
                    conn,
                    self,
                    run_action=self._run_locked_action,
                    on_state=lambda st: GLib.idle_add(self._sync_ui_from_state, st),
                )

            try:
                srv.close()
            except Exception:
                pass
            if SOCKET_PATH.exists():
                try:
                    SOCKET_PATH.unlink()
                except Exception:
                    pass

        def _on_close_request(self, *_args: Any) -> bool:
            self._stop_event.set()
            with self._rf_lock:
                if self.rfcomm is not None:
                    self.rfcomm.close()
                    self.rfcomm = None
            return False

    class HaylouApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="com.diego.HaylouS40", flags=Gio.ApplicationFlags.FLAGS_NONE)
            self.win: HaylouWindow | None = None

        def do_activate(self) -> None:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
            if self.win is None:
                self.win = HaylouWindow(remote=remote, application=self)
            self.win.present()

    app = HaylouApp()
    return app.run([])


# ───────────────────────── CLI Parser ─────────────────────────

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Imprimir el estado resultante en formato JSON")

    p = argparse.ArgumentParser(
        prog="haylou-s40",
        description="Controlador nativo para auriculares Haylou S40 (GUI GTK4 + CLI RFCOMM)",
        parents=[common],
    )
    p.add_argument("--gui", action="store_true", help="Abrir interfaz gráfica GTK4/Libadwaita")
    p.add_argument("--serve", action="store_true", help="Ejecutar daemon headless (socket UNIX, sin GTK)")

    sub = p.add_subparsers(dest="command")
    sub.add_parser("status", parents=[common], help="Consultar estado del auricular")

    p_anc = sub.add_parser("anc", parents=[common], help="Cambiar control de ruido")
    p_anc.add_argument("value", choices=["off", "normal", "anc", "transp", "ambiente", "adaptive", "adaptativo", "cycle"])

    p_game = sub.add_parser("game", parents=[common], help="Modo juego (baja latencia)")
    p_game.add_argument("value", choices=["on", "off", "toggle"])

    p_wind = sub.add_parser("wind", parents=[common], help="Reducción de ruido de viento")
    p_wind.add_argument("value", choices=["on", "off", "toggle"])

    p_multi = sub.add_parser("multi", parents=[common], help="Conexión multipunto")
    p_multi.add_argument("value", choices=["on", "off", "toggle"])

    p_sp = sub.add_parser("spatial", parents=[common], help="Audio espacial 360°")
    p_sp.add_argument("value", choices=["off", "static", "dynamic"])

    p_sc = sub.add_parser("scene", parents=[common], help="Escenario espacial")
    p_sc.add_argument("value", choices=["music", "exercise", "movie"])

    p_sd = sub.add_parser("shutdown", parents=[common], help="Temporizador de autoapagado")
    p_sd.add_argument("value", choices=["30m", "1h", "3h", "5h", "never"])

    p_eq = sub.add_parser("eq", parents=[common], help="Preset de ecualizador hardware")
    p_eq.add_argument("value", choices=[opt[0] for opt in EQ_ALL_OPTIONS])

    p_codec = sub.add_parser("codec", parents=[common], help="Cambiar perfil de códec PipeWire")
    p_codec.add_argument("value", choices=["aac", "sbc_xq", "sbc-xq", "sbc", "ldac", "a2dp-sink", "a2dp-sink-sbc_xq", "a2dp-sink-sbc", "a2dp-sink-ldac"])

    p_ldac = sub.add_parser("ldac", parents=[common], help="Activar o desactivar LDAC en firmware")
    p_ldac.add_argument("value", choices=["on", "off"])

    p_preset = sub.add_parser("preset", parents=[common], help="Aplicar escena rápida")
    p_preset.add_argument("value", choices=["focus", "music", "gaming", "street"])

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    as_json = bool(getattr(args, "json", False))

    # Sin subcomando ni --json -> lanzar GUI por defecto (ideal para lanzador .desktop)
    if args.serve:
        return serve_forever()
    if args.gui or (args.command is None and not as_json):
        return launch_gui()

    if args.command is None and as_json:
        action, value = "status", None
    elif args.command == "status":
        action, value = "status", None
    elif args.command == "preset":
        action, value = "scene_preset", args.value
    else:
        action, value = args.command, args.value

    ok, msg, st = run_cli_oneshot(action, value)
    if as_json:
        print(json.dumps({"ok": ok, "message": msg, **st}, ensure_ascii=False))
    else:
        if not ok:
            print(f"Error: {msg}", file=sys.stderr)
        elif action != "status":
            print(msg)
        print_human_status(st)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
