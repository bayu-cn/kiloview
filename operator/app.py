#!/usr/bin/env python3
"""Halaman operator: drag sumber MediaMTX ke SDI DeckLink."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
STATE_PATH = ROOT / "state.json"
LOG_DIR = ROOT / "logs"

SLOTS = {
    "sdi1": {"label": "SDI 1", "device": 0},
    # DeckLink 4K Extreme hanya punya satu kanal putar (device 0).
    # Device 1 adalah sisi masukan. Soket SDI lain membawa gambar yang sama.
    "sdi2": {"label": "SDI 2", "device": 1},
}
MODES = ("1080p60", "1080i50")
AUDIO = ("1-2", "1-1", "2-2")
MATRICES = {
    "1-1": "<<(double)1.0,(double)0.0>,<(double)1.0,(double)0.0>>",
    "2-2": "<<(double)0.0,(double)1.0>,<(double)0.0,(double)1.0>>",
}

lock = threading.Lock()
runtime = {}
sessions = {}
oauth_states = {}
matrix_prop = None


def load_env_file() -> None:
    path = ROOT / "config.env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def apply_paths() -> None:
    global STATE_PATH, LOG_DIR
    base = Path(cfg("DATA_DIR") or str(ROOT))
    base.mkdir(parents=True, exist_ok=True)
    STATE_PATH = base / "state.json"
    LOG_DIR = base / "logs"
    for slot_id, env_name in (("sdi1", "SDI1_DEVICE"), ("sdi2", "SDI2_DEVICE")):
        raw = cfg(env_name)
        if raw.isdigit():
            SLOTS[slot_id]["device"] = int(raw)
    for slot_id, env_name in (("sdi1", "SDI1_PERSISTENT_ID"), ("sdi2", "SDI2_PERSISTENT_ID")):
        parsed = parse_persistent_id(cfg(env_name))
        if parsed is not None:
            SLOTS[slot_id]["persistent_id"] = parsed


def parse_persistent_id(raw: str) -> int | None:
    text = raw.strip().lower()
    if text.startswith("p"):
        text = text[1:]
    if text.startswith("0x"):
        text = text[2:]
    if not text:
        return None
    try:
        base = 16 if any(ch in text for ch in "abcdef") else 10
        return int(text, base)
    except ValueError:
        return None


def sink_device_args(slot_id: str) -> list[str]:
    persistent = SLOTS[slot_id].get("persistent_id")
    if persistent is not None:
        return [f"persistent-id={persistent}"]
    return [f"device-number={SLOTS[slot_id]['device']}"]


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def auth_enabled() -> bool:
    return all(cfg(name) for name in (
        "KEYCLOAK_URL", "KEYCLOAK_REALM", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET",
    ))


def names_from(*env_names: str, default: str = "") -> set[str]:
    found = set()
    seen = False
    for name in env_names:
        if name not in os.environ:
            continue
        seen = True
        found.update(part.strip().lower() for part in os.environ.get(name, "").split(",") if part.strip())
    if not seen:
        found.update(part.strip().lower() for part in default.split(",") if part.strip())
    return found


def allowed_roles() -> set[str]:
    return names_from("ALLOWED_ROLES", default="admin,operator-feeddesk")


def admin_roles() -> set[str]:
    return names_from("ADMIN_ROLES", default="admin")


def operator_roles() -> set[str]:
    return names_from("OPERATOR_ROLES", default="operator-feeddesk")


def admin_groups() -> set[str]:
    return names_from("ADMIN_GROUP", "ADMIN_GROUPS", default="IT")


def operator_groups() -> set[str]:
    return names_from("OPERATOR_GROUP", "OPERATOR_GROUPS", "ALLOWED_GROUP", "ALLOWED_GROUPS", default="feeddesk")


def sources() -> list[str]:
    return [item["id"] for item in catalog]


def sources_path() -> Path:
    return STATE_PATH.parent / "sources.json"


SOURCE_SCHEMES = {"srt", "rtsp", "rtsps", "rtmp", "rtmps", "udp", "http", "https"}
LOCAL_HOSTS = {"", "0.0.0.0", "127.0.0.1", "localhost", "::1"}
BLOCKED_PORTS = {80, 8090, 8189, 8554, 8888, 8889, 8890, 9997, 1935}
listeners: dict[str, dict] = {}


def legacy_listener_url(port: int) -> str:
    return f"srt://127.0.0.1:{port}?mode=listener"


def source_url(item: dict) -> str | None:
    raw = str(item.get("url") or "").strip()
    if raw:
        return raw
    try:
        port = int(item.get("port"))
    except (TypeError, ValueError):
        return None
    if 1024 <= port <= 65535:
        return legacy_listener_url(port)
    return None


def valid_source_url(url: str) -> bool:
    if not url or len(url) > 300 or any(char.isspace() or ord(char) < 32 for char in url):
        return False
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in SOURCE_SCHEMES and bool(parsed.netloc)


def local_port(url: str) -> int | None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in LOCAL_HOSTS and parsed.port:
        return parsed.port
    return None


def yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def source_identity(item: dict) -> tuple[str, str] | None:
    source_id = str(item.get("id", "")).strip()
    label = str(item.get("label") or source_id).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", source_id):
        return None
    if source_id in SLOTS or source_id.lower() in {"sdi1", "sdi2"}:
        return None
    if not label or len(label) > 48:
        return None
    return source_id, label


def listen_port(item: dict) -> int | None:
    try:
        port = int(item.get("port"))
    except (TypeError, ValueError):
        return None
    if 1024 <= port <= 65535:
        return port
    return None


def normalize_source(item: dict) -> dict | None:
    identity = source_identity(item)
    if identity is None:
        return None
    source_id, label = identity
    mode = str(item.get("mode") or "caller").strip().lower()
    if mode == "listener":
        port = listen_port(item)
        if port is None:
            return None
        return {"id": source_id, "label": label, "mode": "listener", "port": port, "url": ""}
    if mode != "caller":
        return None
    url = source_url(item)
    if url is None or not valid_source_url(url):
        return None
    return {"id": source_id, "label": label, "mode": "caller", "port": None, "url": url}


def seed_sources() -> list[dict]:
    parsed = parse_mediamtx_sources()
    if parsed:
        return parsed
    names = [part.strip() for part in cfg("SOURCES", "p3-1,p3-2,p3-3,p3-4").split(",") if part.strip()]
    return [{"id": name, "label": name, "mode": "caller", "url": legacy_listener_url(3001 + index)} for index, name in enumerate(names)]


def parse_mediamtx_sources() -> list[dict]:
    path = mediamtx_config_path()
    if path is None or not path.exists():
        return []
    found = []
    current: dict | None = None
    in_paths = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("paths:"):
            in_paths = True
            continue
        if not in_paths:
            continue
        if re.match(r"^  [^ #\s].*:$", line):
            name = line.strip()[:-1]
            current = None if name in SLOTS else {"id": name, "label": name}
            if current:
                found.append(current)
            continue
        if current and re.match(r"^\s+source:\s+", line):
            value = re.sub(r"^\s+source:\s+", "", line).strip().strip("'\"")
            if value and value != "publisher":
                current["url"] = value
    return [item for item in (normalize_source(item) for item in found) if item]


def load_catalog() -> None:
    global catalog
    path = sources_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = []
        catalog = [item for item in (normalize_source(item) for item in raw if isinstance(item, dict)) if item]
        if isinstance(raw, list) and any(isinstance(item, dict) and not item.get("url") for item in raw):
            save_catalog()
        return
    catalog = seed_sources()
    save_catalog()


def save_catalog() -> None:
    path = sources_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    tmp.replace(path)


def mediamtx_config_path() -> Path | None:
    raw = cfg("MEDIAMTX_CONFIG")
    return Path(raw) if raw else None


def mediamtx_source(item: dict) -> str:
    if item.get("mode") == "listener":
        return "publisher"
    return item["url"]


def write_mediamtx() -> None:
    path = mediamtx_config_path()
    if path is None or not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    head = text.split("paths:", 1)[0].rstrip() if "paths:" in text else text.rstrip()
    lines = ["paths:"]
    for item in catalog:
        source = mediamtx_source(item)
        rendered = source if source == "publisher" else yaml_quote(source)
        lines.extend([
            f"  {item['id']}:",
            f"    source: {rendered}",
            "    sourceOnDemand: false",
            "",
        ])
    lines.extend([
        "  # Preview operator. Diisi GStreamer, bukan SRT.",
        "  sdi1:",
        "    source: publisher",
        "",
        "  sdi2:",
        "    source: publisher",
        "",
    ])
    path.write_text(head + "\n" + "\n".join(lines), encoding="utf-8")


def mediamtx_call(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    url = cfg("MEDIAMTX_API", "http://127.0.0.1:9997").rstrip("/") + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return 0, str(exc)


def publish_source(item: dict, replace: bool = True) -> str | None:
    payload = {
        "source": mediamtx_source(item),
        "sourceOnDemand": False,
    }
    quoted = urllib.parse.quote(item["id"], safe="")
    code, detail = mediamtx_call("POST", f"/v3/config/paths/add/{quoted}", payload)
    if code in (200, 201, 204):
        return None
    if not replace and "exist" in detail.lower():
        return None
    code, detail = mediamtx_call("POST", f"/v3/config/paths/replace/{quoted}", payload)
    if code in (200, 201, 204):
        return None
    return f"MediaMTX did not accept {item['id']}: {detail or code}"


def ensure_sources() -> None:
    for item in catalog:
        publish_source(item, replace=False)


def stop_listener(source_id: str) -> None:
    run = listeners.get(source_id)
    if not run:
        return
    run["generation"] += 1
    proc = run.get("proc")
    run["proc"] = None
    if proc is not None and proc.poll() is None:
        kill_process(proc)
    listeners.pop(source_id, None)


def listener_args(item: dict) -> list[str] | None:
    binary = "/usr/bin/ffmpeg"
    if not Path(binary).is_file():
        return None
    base = cfg("RTSP_BASE", "rtsp://127.0.0.1:8554").rstrip("/")
    port = int(item["port"])
    return [
        "nice", "-n", "12",
        binary, "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-i", f"srt://0.0.0.0:{port}?mode=listener&transtype=live&latency=120000",
        "-c", "copy",
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        f"{base}/{item['id']}",
    ]


def start_listener(source_id: str) -> str | None:
    item = next((entry for entry in catalog if entry["id"] == source_id), None)
    if item is None or item.get("mode") != "listener":
        stop_listener(source_id)
        return None
    run = listeners.setdefault(source_id, {"proc": None, "generation": 0, "error": None})
    proc = run.get("proc")
    if proc is not None and proc.poll() is None:
        kill_process(proc)
    run["proc"] = None
    run["generation"] += 1
    generation = run["generation"]
    args = listener_args(item)
    if args is None:
        run["error"] = "Listener needs FFmpeg in the operator image. Rebuild the operator container."
        return run["error"]
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{source_id}-listen.log"
    log = open(log_path, "w", encoding="utf-8")
    log.write(" ".join(args) + "\n\n")
    log.flush()
    try:
        proc = subprocess.Popen(
            args,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        log.write(f"\nListener failed to start: {exc}\n")
        log.close()
        run["error"] = str(exc)
        return str(exc)
    log.close()
    run["proc"] = proc
    run["error"] = None
    run["started"] = time.monotonic()
    threading.Thread(target=watch_listener, args=(source_id, generation, proc), daemon=True).start()
    return None


def watch_listener(source_id: str, generation: int, proc: subprocess.Popen) -> None:
    proc.wait()
    with lock:
        run = listeners.get(source_id)
        if not run or run["generation"] != generation or run.get("proc") is not proc:
            return
        run["proc"] = None
        lived = time.monotonic() - float(run.get("started") or 0)
        if lived < 3:
            run["error"] = useful_log(source_id, "-listen") or f"Listener stopped (code {proc.returncode})"
        else:
            run["error"] = None
        threading.Thread(target=restart_listener_later, args=(source_id, generation), daemon=True).start()


def restart_listener_later(source_id: str, generation: int) -> None:
    time.sleep(2)
    with lock:
        run = listeners.get(source_id)
        if not run or run["generation"] != generation:
            return
        if not any(item["id"] == source_id and item.get("mode") == "listener" for item in catalog):
            return
        start_listener(source_id)


def sync_listener(previous: dict | None, item: dict) -> str | None:
    was_listener = bool(previous and previous.get("mode") == "listener")
    now_listener = item.get("mode") == "listener"
    port_changed = was_listener and now_listener and previous.get("port") != item.get("port")
    if was_listener and (not now_listener or port_changed):
        stop_listener(item["id"])
    if now_listener and (not was_listener or port_changed):
        return start_listener(item["id"])
    return None


def occupied_ports(except_id: str | None = None) -> set[int]:
    found: set[int] = set()
    for other in catalog:
        if other["id"] == except_id:
            continue
        if other.get("mode") == "listener" and other.get("port"):
            found.add(int(other["port"]))
        parsed = local_port(other.get("url") or "")
        if parsed:
            found.add(parsed)
    return found


def validate_source(payload: dict, current_id: str | None = None) -> dict:
    mode = str(payload.get("mode") or "caller").strip().lower()
    item = normalize_source({
        "id": current_id if current_id else payload.get("id"),
        "label": payload.get("label"),
        "mode": mode,
        "url": payload.get("url"),
        "port": payload.get("port"),
    })
    if item is None:
        if mode == "listener":
            raise ValueError("Name, path ID, or listen port is invalid.")
        raise ValueError("Name, path ID, or source URL is invalid.")
    if current_id is None and any(other["id"] == item["id"] for other in catalog):
        raise ValueError("That path ID already exists.")
    for other in catalog:
        if other["id"] == item["id"]:
            continue
        if item["url"] and other.get("url") == item["url"]:
            raise ValueError(f"That source URL is already used by {other['label']}.")
    if item["mode"] == "listener":
        port = int(item["port"])
        if port in BLOCKED_PORTS:
            raise ValueError(f"Port {port} is used by another service.")
        if port in occupied_ports(current_id):
            raise ValueError(f"Port {port} is already used.")
    else:
        port = local_port(item["url"])
        if port in BLOCKED_PORTS:
            raise ValueError(f"Port {port} is used by another service.")
        if port and port in occupied_ports(current_id):
            raise ValueError(f"Port {port} is already used.")
    return item


def public_url() -> str:
    return cfg("PUBLIC_URL", "http://172.17.20.46:8090").rstrip("/")


def keycloak_base() -> str:
    realm = urllib.parse.quote(cfg("KEYCLOAK_REALM"), safe="")
    return f"{cfg('KEYCLOAK_URL').rstrip('/')}/realms/{realm}/protocol/openid-connect"


def load_state() -> dict:
    slots = {}
    saved = {}
    if STATE_PATH.exists():
        try:
            saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = {}
    for slot_id, meta in SLOTS.items():
        item = saved.get(slot_id, {})
        source = item.get("source")
        if source not in sources():
            source = None
        mode = item.get("mode") if item.get("mode") in MODES else "1080i50"
        audio = item.get("audio") if item.get("audio") in AUDIO else "1-2"
        slots[slot_id] = {
            "source": source,
            "mode": mode,
            "audio": audio,
            "locked": bool(item.get("locked")),
        }
        runtime.setdefault(slot_id, {
            "proc": None,
            "preview": None,
            "preview_generation": 0,
            "preview_restarts": 0,
            "preview_error": None,
            "generation": 0,
            "restarts": 0,
            "status": "empty" if source is None else "starting",
            "error": None,
        })
    return slots


catalog: list[dict] = []
state = {}


def save_state() -> None:
    payload = {
        slot_id: {key: slot[key] for key in ("source", "mode", "audio", "locked")}
        for slot_id, slot in state.items()
    }
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def matrix_property() -> str:
    global matrix_prop
    if matrix_prop:
        return matrix_prop
    matrix_prop = "matrix"
    try:
        out = subprocess.check_output(
            ["gst-inspect-1.0", "audiomixmatrix"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return matrix_prop
    if "mixing-matrix" in out and "\nmatrix " not in out and " matrix:" not in out:
        matrix_prop = "mixing-matrix"
    return matrix_prop


def video_args(mode: str) -> list[str]:
    if mode == "1080p60":
        return [
            "videoconvert", "!", "videoscale", "!", "videorate", "!",
            "video/x-raw,format=UYVY,width=1920,height=1080,framerate=60/1",
        ]
    return [
        "videoconvert", "!", "videoscale", "!", "videorate", "!",
        "video/x-raw,width=1920,height=1080,framerate=50/1", "!",
        "interlace", "field-pattern=2:2", "top-field-first=true", "!",
        "video/x-raw,format=UYVY,interlace-mode=interleaved",
    ]


def audio_args(mapping: str) -> list[str]:
    # DeckLink menolak jadwal frame kalau audio-nya S16LE.
    tail = ["audio/x-raw,format=S32LE,rate=48000,channels=2"]
    if mapping == "1-2":
        return ["audioconvert", "!", "audioresample", "!", *tail]
    prop = matrix_property()
    return [
        "audioconvert", "!", "audio/x-raw,channels=2", "!",
        "audiomixmatrix", "mode=manual", "in-channels=2", "out-channels=2",
        f'{prop}="{MATRICES[mapping]}"', "!",
        "audioconvert", "!", "audioresample", "!",
        *tail,
    ]


def preview_args(slot_id: str) -> list[str] | None:
    binary = "/usr/bin/ffmpeg"
    if not Path(binary).is_file():
        return None
    slot = state[slot_id]
    base = cfg("RTSP_BASE", "rtsp://127.0.0.1:8554").rstrip("/")
    # Preview terpisah dari kartu SDI. Gambar diperkecil, audio mengikuti peta
    # yang sama, lalu dipublish supaya browser bisa mendengarnya.
    filters = {
        "1-1": "pan=stereo|c0=c0|c1=c0",
        "2-2": "pan=stereo|c0=c1|c1=c1",
    }
    args = [
        "nice", "-n", "12",
        binary, "-nostdin", "-hide_banner", "-loglevel", "warning", "-threads", "2",
        "-rtsp_transport", "tcp",
        "-i", f"{base}/{slot['source']}",
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-vf", "scale=640:360,fps=25",
    ]
    if slot["audio"] in filters:
        args.extend(["-af", filters[slot["audio"]]])
    args.extend([
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-profile:v", "baseline",
        "-bf", "0",
        "-g", "25",
        "-pix_fmt", "yuv420p",
        "-b:v", "800k",
        "-c:a", "libopus",
        "-b:a", "64k",
        "-ar", "48000",
        "-ac", "2",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        f"{base}/{slot_id}",
    ])
    return args


def pipeline_args(slot_id: str) -> list[str]:
    slot = state[slot_id]
    mode = slot["mode"]
    location = f"{cfg('RTSP_BASE', 'rtsp://127.0.0.1:8554').rstrip('/')}/{slot['source']}"
    decklink_mode = "1080p60" if mode == "1080p60" else "1080i50"
    # Gambar dan suara dipisah di rtspsrc. Satu decodebin untuk keduanya
    # membuat cabang audio gagal nyambung dan waktu pipeline tetap 0.
    return [
        cfg("GST_LAUNCH", "gst-launch-1.0"), "-e",
        "rtspsrc", f"location={location}", "latency=200", "protocols=tcp", "name=src",
        "src.", "!", "application/x-rtp,media=video", "!", "decodebin", "!", "queue", "!", "video/x-raw", "!",
        *video_args(mode), "!",
        "decklinkvideosink", *sink_device_args(slot_id), f"mode={decklink_mode}", "sync=false",
        "src.", "!", "application/x-rtp,media=audio", "!", "decodebin", "!", "queue", "!",
        *audio_args(slot["audio"]), "!",
        "decklinkaudiosink", *sink_device_args(slot_id), "sync=false", "async=false",
    ]


def kill_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGINT)
        else:
            proc.send_signal(signal.SIGINT)
    except OSError:
        proc.kill()
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass
        proc.wait(timeout=2)


def read_log(slot_id: str, suffix: str = "", limit: int = 40) -> str:
    path = LOG_DIR / f"{slot_id}{suffix}.log"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def signal_lost(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in ("404", "not found", "no such stream", "end-of-file", "end of file"))


def explain_sdi_error(text: str, device: int) -> str:
    if signal_lost(text):
        return text
    if "Failed to start" in text or "doesn't want to preroll" in text:
        return (
            "This card did not start. Check the config IDs: SDI 1 and SDI 2 must be two different cards.\n"
            + text
        )
    return text


def useful_log(slot_id: str, suffix: str = "") -> str:
    text = read_log(slot_id, suffix, limit=80)
    keys = ("ERROR", "Error", "Failed", "failed", "no element", "Gagal", "Schedule", "errno")
    picked = [line for line in text.splitlines() if any(key in line for key in keys)]
    if picked:
        return "\n".join(picked[-4:])
    return "\n".join(text.splitlines()[-6:])


def start_pipeline(slot_id: str) -> None:
    run = runtime[slot_id]
    proc = run["proc"]
    if proc is not None and proc.poll() is None:
        kill_process(proc)
    run["proc"] = None
    run["generation"] += 1
    generation = run["generation"]
    slot = state[slot_id]
    if not slot["source"]:
        stop_preview(run)
        run["status"] = "empty"
        run["error"] = None
        run["restarts"] = 0
        run["preview_error"] = None
        run["preview_restarts"] = 0
        return

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{slot_id}.log"
    args = pipeline_args(slot_id)
    log = open(log_path, "w", encoding="utf-8")
    log.write(" ".join(args) + "\n\n")
    log.flush()
    try:
        proc = subprocess.Popen(
            args,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        log.write(f"\nGagal menjalankan GStreamer: {exc}\n")
        log.close()
        run["status"] = "error"
        run["error"] = str(exc)
        start_preview(slot_id)
        return
    log.close()
    run["proc"] = proc
    run["status"] = "waiting" if run["status"] == "waiting" else "starting"
    run["error"] = None
    threading.Thread(target=watch_pipeline, args=(slot_id, generation, proc), daemon=True).start()
    threading.Thread(target=mark_playing, args=(slot_id, generation, proc), daemon=True).start()
    start_preview(slot_id)


def mark_playing(slot_id: str, generation: int, proc: subprocess.Popen) -> None:
    time.sleep(1.2)
    with lock:
        run = runtime[slot_id]
        if run["generation"] != generation or run["proc"] is not proc:
            return
        if proc.poll() is None and run["status"] in {"starting", "waiting"}:
            run["status"] = "playing"
            run["restarts"] = 0


def watch_pipeline(slot_id: str, generation: int, proc: subprocess.Popen) -> None:
    proc.wait()
    with lock:
        run = runtime[slot_id]
        if run["generation"] != generation or run["proc"] is not proc:
            return
        run["proc"] = None
        if state[slot_id]["source"] and signal_lost(read_log(slot_id)):
            run["status"] = "waiting"
            run["error"] = None
            run["preview_error"] = None
            run["restarts"] = 0
            threading.Thread(target=restart_later, args=(slot_id, generation), daemon=True).start()
            return
        if state[slot_id]["source"] and run["restarts"] < 3:
            run["restarts"] += 1
            run["status"] = "starting"
            threading.Thread(target=restart_later, args=(slot_id, generation), daemon=True).start()
            return
        run["status"] = "error"
        run["error"] = explain_sdi_error(useful_log(slot_id), SLOTS[slot_id]["device"]) or f"GStreamer stopped (code {proc.returncode})"


def restart_later(slot_id: str, generation: int) -> None:
    time.sleep(2)
    with lock:
        run = runtime[slot_id]
        if run["generation"] != generation or not state[slot_id]["source"]:
            return
        start_pipeline(slot_id)


def stop_preview(run: dict) -> None:
    run["preview_generation"] = run.get("preview_generation", 0) + 1
    proc = run.get("preview")
    run["preview"] = None
    if proc is not None and proc.poll() is None:
        kill_process(proc)


def start_preview(slot_id: str) -> None:
    run = runtime[slot_id]
    stop_preview(run)
    if not state[slot_id]["source"]:
        run["preview_error"] = None
        return
    args = preview_args(slot_id)
    if args is None:
        run["preview_error"] = (
            "SDI preview needs FFmpeg in the operator image. "
            "On the server run: docker compose up -d --build"
        )
        return
    generation = run["preview_generation"]
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{slot_id}-preview.log"
    log = open(log_path, "w", encoding="utf-8")
    log.write(" ".join(args) + "\n\n")
    log.flush()
    try:
        proc = subprocess.Popen(
            args,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        log.write(f"\nGagal menjalankan preview: {exc}\n")
        log.close()
        run["preview_error"] = str(exc)
        return
    log.close()
    run["preview"] = proc
    run["preview_error"] = None
    threading.Thread(target=watch_preview, args=(slot_id, generation, proc), daemon=True).start()


def watch_preview(slot_id: str, generation: int, proc: subprocess.Popen) -> None:
    proc.wait()
    with lock:
        run = runtime[slot_id]
        if run.get("preview_generation") != generation or run.get("preview") is not proc:
            return
        run["preview"] = None
        if state[slot_id]["source"] and signal_lost(read_log(slot_id, "-preview")):
            run["preview_error"] = None
            threading.Thread(target=restart_preview_later, args=(slot_id, generation), daemon=True).start()
            return
        if state[slot_id]["source"] and run.get("preview_restarts", 0) < 3:
            run["preview_restarts"] = run.get("preview_restarts", 0) + 1
            threading.Thread(target=restart_preview_later, args=(slot_id, generation), daemon=True).start()
            return
        run["preview_error"] = read_log(slot_id, "-preview", 8) or f"Preview stopped (code {proc.returncode})"


def restart_preview_later(slot_id: str, generation: int) -> None:
    time.sleep(2)
    with lock:
        run = runtime[slot_id]
        if run.get("preview_generation") != generation or not state[slot_id]["source"]:
            return
        start_preview(slot_id)


def stop_all() -> None:
    with lock:
        for slot_id in SLOTS:
            run = runtime[slot_id]
            run["generation"] += 1
            proc = run["proc"]
            run["proc"] = None
            preview = run.get("preview")
            run["preview"] = None
            run["preview_generation"] = run.get("preview_generation", 0) + 1
            if proc is not None:
                kill_process(proc)
            if preview is not None:
                kill_process(preview)
        for source_id in list(listeners):
            stop_listener(source_id)


def webrtc_base() -> str:
    explicit = cfg("MEDIAMTX_WEBRTC")
    if explicit:
        return explicit.rstrip("/")
    public = cfg("MEDIAMTX_PUBLIC", "http://172.17.20.46:8889")
    return public.replace(":8888", ":8889").rstrip("/")


def path_ready() -> dict[str, bool] | None:
    url = cfg("MEDIAMTX_API", "http://127.0.0.1:9997").rstrip("/") + "/v3/paths/list"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None
    items = payload.get("items", payload if isinstance(payload, list) else [])
    return {item.get("name"): bool(item.get("ready")) for item in items if isinstance(item, dict)}


def snapshot() -> dict:
    ready = path_ready() or {}
    with lock:
        slots_copy = {slot_id: dict(slot) for slot_id, slot in state.items()}
        runs_copy = {}
        for slot_id, run in runtime.items():
            proc = run.get("proc")
            if proc is not None and proc.poll() is None and run["status"] == "error":
                run["status"] = "playing"
                run["error"] = None
            runs_copy[slot_id] = {
                "status": run["status"],
                "error": run["error"],
                "preview_error": run.get("preview_error"),
            }
    web = webrtc_base()
    routed: dict[str, list[str]] = {}
    slots_out = {}
    for slot_id, meta in SLOTS.items():
        slot = slots_copy[slot_id]
        run = runs_copy[slot_id]
        if slot["source"]:
            routed.setdefault(slot["source"], []).append(meta["label"])
        pid = meta.get("persistent_id")
        slots_out[slot_id] = {
            "label": meta["label"],
            "device": meta["device"],
            "card": f"P{pid:08x}" if pid else f"device {meta['device']}",
            "source": slot["source"],
            "mode": slot["mode"],
            "audio": slot["audio"],
            "locked": bool(slot.get("locked")),
            "status": run["status"],
            "error": run["error"] if run["status"] == "error" else None,
            "whep": f"{web}/{slot_id}/whep" if slot["source"] else None,
            "previewError": run["preview_error"],
        }
    return {
        "auth": auth_enabled(),
        "user": None,
        "sources": [
            {
                "id": source["id"],
                "label": source["label"],
                "mode": source.get("mode") or "caller",
                "url": source.get("url") or "",
                "port": source.get("port"),
                "ingestError": (listeners.get(source["id"]) or {}).get("error"),
                "ready": bool(ready.get(source["id"])),
                "whep": f"{web}/{urllib.parse.quote(source['id'])}/whep",
                "routedTo": routed.get(source["id"], []),
            }
            for source in catalog
        ],
        "slots": slots_out,
    }


def jwt_payload(token: str) -> dict:
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def token_roles(payload: dict) -> set[str]:
    roles = set(payload.get("realm_access", {}).get("roles", []))
    client = cfg("KEYCLOAK_CLIENT_ID")
    roles.update(payload.get("resource_access", {}).get(client, {}).get("roles", []))
    return {role.lower() for role in roles if isinstance(role, str)}


def group_names(raw: object) -> set[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    names = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        leaf = item.strip().strip("/").split("/")[-1].lower()
        if leaf:
            names.add(leaf)
    return names


def token_groups(payload: dict) -> set[str]:
    names = set()
    for key, value in payload.items():
        if "group" in str(key).lower():
            names |= group_names(value)
    return names


def membership(payloads: list[dict]) -> tuple[bool, bool, set[str]]:
    roles: set[str] = set()
    groups: set[str] = set()
    for payload in payloads:
        roles |= token_roles(payload)
        groups |= token_groups(payload)
    in_admin_group = bool(groups & admin_groups())
    in_operator_group = bool(groups & operator_groups())
    if in_admin_group:
        is_admin = True
    elif in_operator_group:
        is_admin = False
    else:
        is_admin = bool(roles & admin_roles())
    is_operator = in_operator_group or bool(roles & operator_roles() or roles & (allowed_roles() - admin_roles()))
    return is_admin, is_admin or is_operator, groups


def keycloak_ssl_context() -> ssl.SSLContext:
    verify = cfg("KEYCLOAK_SSL_VERIFY", "true").lower()
    if verify in {"0", "false", "no"}:
        return ssl._create_unverified_context()
    cafile = cfg("KEYCLOAK_CA") or os.environ.get("SSL_CERT_FILE") or None
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    strict = getattr(ssl, "VERIFY_X509_STRICT", None)
    if strict:
        ctx.verify_flags &= ~strict
    return ctx


def exchange_token(form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(
        keycloak_base() + "/token",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=10, context=keycloak_ssl_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def token_payloads(token: dict) -> list[dict]:
    payloads = [jwt_payload(token["access_token"])]
    raw = token.get("id_token")
    if raw:
        try:
            payloads.append(jwt_payload(raw))
        except (IndexError, json.JSONDecodeError, ValueError, TypeError):
            pass
    return payloads


def session_from_token(token: dict) -> dict:
    payloads = token_payloads(token)
    payload = payloads[0]
    is_admin, allowed, groups = membership(payloads)
    if not allowed:
        names = ", ".join(sorted(admin_groups() | operator_groups()))
        seen = ", ".join(sorted(groups)) or "none"
        raise PermissionError(f"This account is not in an allowed group ({names}). Token groups: {seen}.")
    return {
        "user": payload.get("preferred_username") or payload.get("name") or "operator",
        "admin": is_admin,
        "access": token["access_token"],
        "refresh": token.get("refresh_token"),
        "id_token": token.get("id_token"),
        "exp": time.time() + int(token.get("expires_in", 60)) - 20,
    }


def refresh_session(session: dict) -> bool:
    if time.time() < session["exp"] or not session.get("refresh"):
        return time.time() < session["exp"]
    try:
        token = exchange_token({
            "grant_type": "refresh_token",
            "refresh_token": session["refresh"],
            "client_id": cfg("KEYCLOAK_CLIENT_ID"),
            "client_secret": cfg("KEYCLOAK_CLIENT_SECRET"),
        })
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError):
        return False
    try:
        updated = session_from_token(token)
    except PermissionError:
        return False
    session.update(updated)
    return True


def current_session(handler: BaseHTTPRequestHandler) -> dict | None:
    if not auth_enabled():
        return {"user": None, "roles": [], "local": True, "admin": True}
    cookie = handler.headers.get("Cookie", "")
    sid = ""
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "op_session":
            sid = value
            break
    session = sessions.get(sid)
    if not session:
        return None
    if not refresh_session(session):
        sessions.pop(sid, None)
        return None
    return session


def add_source(payload: dict) -> str | None:
    with lock:
        item = validate_source(payload)
        catalog.append(item)
        save_catalog()
    write_mediamtx()
    warning = publish_source(item, replace=True)
    with lock:
        listen_error = sync_listener(None, item)
    return warning or listen_error


def update_source(source_id: str, payload: dict) -> str | None:
    with lock:
        previous = next((item for item in catalog if item["id"] == source_id), None)
        if previous is None:
            raise ValueError("Unknown source.")
        item = validate_source(payload, current_id=source_id)
        for index, other in enumerate(catalog):
            if other["id"] == source_id:
                catalog[index] = item
                break
        save_catalog()
        source_changed = mediamtx_source(previous) != mediamtx_source(item)
    warning = None
    if source_changed:
        write_mediamtx()
        warning = publish_source(item, replace=True)
    with lock:
        listen_error = sync_listener(previous, item)
    return warning or listen_error


def delete_source(source_id: str) -> None:
    with lock:
        if any(slot["source"] == source_id for slot in state.values()):
            raise ValueError("Clear this source from SDI before deleting it.")
        if source_id not in sources():
            raise ValueError("Unknown source.")
        stop_listener(source_id)
        catalog[:] = [item for item in catalog if item["id"] != source_id]
        save_catalog()
    write_mediamtx()
    quoted = urllib.parse.quote(source_id, safe="")
    mediamtx_call("DELETE", f"/v3/config/paths/delete/{quoted}")


def apply_slot(slot_id: str, payload: dict, is_admin: bool = False) -> None:
    slot = state[slot_id]
    if slot.get("locked") and not is_admin:
        raise ValueError("This output is locked.")
    if "locked" in payload:
        if not is_admin:
            raise ValueError("Only the IT group can lock an output.")
        slot["locked"] = bool(payload["locked"])
    routed = False
    if "source" in payload:
        source = payload["source"]
        if source is not None and source not in sources():
            raise ValueError("Unknown source.")
        routed = routed or slot["source"] != source
        slot["source"] = source
    if "mode" in payload:
        if payload["mode"] not in MODES:
            raise ValueError("Unknown video mode.")
        routed = routed or slot["mode"] != payload["mode"]
        slot["mode"] = payload["mode"]
    if "audio" in payload:
        if payload["audio"] not in AUDIO:
            raise ValueError("Unknown audio map.")
        routed = routed or slot["audio"] != payload["audio"]
        slot["audio"] = payload["audio"]
    save_state()
    if routed:
        runtime[slot_id]["restarts"] = 0
        runtime[slot_id]["preview_restarts"] = 0
        start_pipeline(slot_id)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, code: int, payload: dict, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, code: int, html: str, headers: dict | None = None) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, headers: dict | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def cookie(self, sid: str = "", clear: bool = False) -> str:
        secure = "; Secure" if public_url().startswith("https://") else ""
        if clear:
            return f"op_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0{secure}"
        return f"op_session={sid}; HttpOnly; SameSite=Lax; Path=/{secure}"

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/login":
            self.handle_login()
            return
        if path == "/callback":
            self.handle_callback()
            return
        if path == "/logout":
            self.handle_logout()
            return
        if path in ("/", "/index.html"):
            session = current_session(self)
            if auth_enabled() and session is None:
                self.redirect("/login")
                return
            self.serve_static("index.html")
            return
        if path.startswith("/static/"):
            self.serve_static(path[len("/static/"):])
            return
        if path == "/api/state":
            self.handle_state()
            return
        self.send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/sources" or path.startswith("/api/sources/"):
            self.handle_source(path)
            return
        prefix = "/api/slots/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "Not found."})
            return
        slot_id = path[len(prefix):]
        if slot_id not in SLOTS:
            self.send_json(404, {"error": "Unknown SDI slot."})
            return
        session = current_session(self)
        if session is None:
            self.send_json(401, {"error": "Login required."})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 8000:
            self.send_json(400, {"error": "Request is too large."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json(400, {"error": "Invalid JSON."})
            return
        try:
            with lock:
                apply_slot(slot_id, payload, bool(session.get("admin")))
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        self.send_json(200, with_session(snapshot(), session))

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 8000:
            raise ValueError("Request is too large.")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Invalid JSON.")
        return payload

    def handle_source(self, path: str) -> None:
        session = current_session(self)
        if session is None:
            self.send_json(401, {"error": "Login required."})
            return
        if not session.get("admin"):
            self.send_json(403, {"error": "Only the IT group can edit inputs."})
            return
        try:
            if path.endswith("/delete"):
                source_id = urllib.parse.unquote(path[len("/api/sources/"):-len("/delete")])
                delete_source(source_id)
                warning = None
            else:
                payload = self.read_json()
                if path == "/api/sources":
                    warning = add_source(payload)
                else:
                    source_id = urllib.parse.unquote(path[len("/api/sources/"):])
                    warning = update_source(source_id, payload)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        except OSError as exc:
            self.send_json(500, {"error": f"Could not save the MediaMTX config: {exc}"})
            return
        body = with_session(snapshot(), session)
        if warning:
            body["warning"] = warning
        self.send_json(200, body)

    def handle_state(self) -> None:
        session = current_session(self)
        if session is None:
            self.send_json(401, {"error": "Login required."})
            return
        self.send_json(200, with_session(snapshot(), session))

    def handle_login(self) -> None:
        if not auth_enabled():
            self.redirect("/")
            return
        state_token = secrets.token_urlsafe(24)
        oauth_states[state_token] = time.time() + 600
        query = urllib.parse.urlencode({
            "client_id": cfg("KEYCLOAK_CLIENT_ID"),
            "response_type": "code",
            "scope": "openid profile",
            "redirect_uri": public_url() + "/callback",
            "state": state_token,
        })
        self.redirect(keycloak_base() + "/auth?" + query)

    def handle_callback(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if query.get("error"):
            message = query.get("error_description", query["error"])[0]
            self.send_html(400, message_page("Login failed", message))
            return
        state_token = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        expires = oauth_states.pop(state_token, 0)
        if not code or expires < time.time():
            self.send_html(400, message_page("Login failed", "The login session expired. Try again."))
            return
        try:
            token = exchange_token({
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": public_url() + "/callback",
                "client_id": cfg("KEYCLOAK_CLIENT_ID"),
                "client_secret": cfg("KEYCLOAK_CLIENT_SECRET"),
            })
            session = session_from_token(token)
        except PermissionError as exc:
            self.send_html(403, message_page("Access denied", str(exc)))
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            self.send_html(502, message_page("Login failed", f"Keycloak rejected the login ({exc.code}). {detail}"))
            return
        except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
            self.send_html(502, message_page("Login failed", f"Keycloak did not return a token. {exc}"))
            return
        sid = secrets.token_urlsafe(32)
        sessions[sid] = session
        self.redirect("/", {"Set-Cookie": self.cookie(sid)})

    def handle_logout(self) -> None:
        sid = ""
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "op_session":
                sid = value
        session = sessions.pop(sid, None)
        location = "/"
        if session and session.get("id_token"):
            query = urllib.parse.urlencode({
                "id_token_hint": session["id_token"],
                "post_logout_redirect_uri": public_url() + "/",
                "client_id": cfg("KEYCLOAK_CLIENT_ID"),
            })
            location = keycloak_base() + "/logout?" + query
        self.redirect(location, {"Set-Cookie": self.cookie(clear=True)})

    def serve_static(self, name: str) -> None:
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            self.send_json(404, {"error": "Not found."})
            return
        kind = "text/html" if target.suffix == ".html" else "text/css" if target.suffix == ".css" else "text/javascript"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def first_name(*env_names: str, default: str) -> str:
    for name in env_names:
        raw = os.environ.get(name, "")
        part = raw.split(",")[0].strip()
        if part:
            return part
    return default


def with_session(body: dict, session: dict) -> dict:
    body["user"] = session.get("user")
    body["admin"] = bool(session.get("admin"))
    if session.get("admin"):
        body["group"] = first_name("ADMIN_GROUP", "ADMIN_GROUPS", default="IT")
    else:
        body["group"] = first_name("OPERATOR_GROUP", "OPERATOR_GROUPS", "ALLOWED_GROUP", "ALLOWED_GROUPS", default="feeddesk")
    return body


def message_page(title: str, message: str) -> str:
    def esc(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<title>{esc(title)}</title>"
        "<body style=\"font-family:sans-serif;background:#101114;color:#eceae6;padding:2rem\">"
        f"<h1>{esc(title)}</h1><p>{esc(message)}</p><p><a style=\"color:#eceae6\" href=\"/\">Back</a></p>"
        "</body>"
    )


def main() -> None:
    global state
    load_env_file()
    apply_paths()
    load_catalog()
    try:
        write_mediamtx()
    except OSError as exc:
        print(f"MediaMTX config tidak tertulis: {exc}", flush=True)
    ensure_sources()
    with lock:
        for item in catalog:
            if item.get("mode") == "listener":
                start_listener(item["id"])
    state = load_state()
    with lock:
        for slot_id, slot in state.items():
            if slot["source"]:
                start_pipeline(slot_id)
    signal.signal(signal.SIGTERM, lambda *_: stop_and_exit())
    signal.signal(signal.SIGINT, lambda *_: stop_and_exit())
    host = cfg("BIND", "0.0.0.0")
    port = int(cfg("PORT", "8090") or "8090")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Operator siap di {public_url()}", flush=True)
    server.serve_forever()


def stop_and_exit() -> None:
    stop_all()
    raise SystemExit(0)


if __name__ == "__main__":
    main()
