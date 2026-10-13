#!/usr/bin/env python3
"""Halaman operator: drag sumber MediaMTX ke SDI DeckLink."""

from __future__ import annotations

import base64
import json
import os
import secrets
import signal
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


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def auth_enabled() -> bool:
    return all(cfg(name) for name in (
        "KEYCLOAK_URL", "KEYCLOAK_REALM", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET",
    ))


def allowed_roles() -> set[str]:
    raw = cfg("ALLOWED_ROLES", "admin,operator-feeddesk")
    return {part.strip() for part in raw.split(",") if part.strip()}


def sources() -> list[str]:
    raw = cfg("SOURCES", "p3-1,p3-2,p3-3,p3-4")
    return [part.strip() for part in raw.split(",") if part.strip()]


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
        slots[slot_id] = {"source": source, "mode": mode, "audio": audio}
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


state = {}


def save_state() -> None:
    payload = {slot_id: {key: slot[key] for key in ("source", "mode", "audio")} for slot_id, slot in state.items()}
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


_gst_cache: dict[tuple[str, str], bool] = {}


def inspect_env(binary: str) -> dict[str, str] | None:
    if binary != "gst-launch-1.0":
        return None
    env = os.environ.copy()
    for key in ("GST_PLUGIN_PATH", "GST_PLUGIN_SYSTEM_PATH", "LD_LIBRARY_PATH"):
        env.pop(key, None)
    return env


def gst_has(binary: str, element: str) -> bool:
    key = (binary, element)
    if key in _gst_cache:
        return _gst_cache[key]
    inspect = str(Path(binary).with_name("gst-inspect-1.0"))
    try:
        result = subprocess.run(
            [inspect, element],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            env=inspect_env(binary),
        )
    except (OSError, subprocess.SubprocessError):
        _gst_cache[key] = False
        return False
    _gst_cache[key] = result.returncode == 0
    return _gst_cache[key]


def preview_launch() -> str | None:
    host = cfg("GST_LAUNCH", "gst-launch-1.0")
    candidates = [host] if host != "gst-launch-1.0" else []
    candidates.append("gst-launch-1.0")
    for binary in candidates:
        if gst_has(binary, "x264enc") and gst_has(binary, "opusenc") and gst_has(binary, "rtspclientsink"):
            return binary
        if gst_has(binary, "openh264enc") and gst_has(binary, "opusenc") and gst_has(binary, "rtspclientsink"):
            return binary
    return None


def video_encoder(binary: str) -> list[str] | None:
    if gst_has(binary, "x264enc"):
        return [
            "x264enc", "tune=zerolatency", "speed-preset=ultrafast",
            "bitrate=700", "key-int-max=25", "bframes=0",
            "option-string=profile=baseline", "!",
            "h264parse", "config-interval=-1",
        ]
    if gst_has(binary, "openh264enc"):
        return [
            "openh264enc", "bitrate=700000", "complexity=low", "!",
            "h264parse", "config-interval=-1",
        ]
    return None


def preview_audio_args(mapping: str) -> list[str]:
    tail = ["audio/x-raw,rate=48000,channels=2", "!", "opusenc", "bitrate=64000"]
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
    binary = preview_launch()
    if binary is None:
        return None
    encoder = video_encoder(binary)
    if encoder is None:
        return None
    slot = state[slot_id]
    location = f"{cfg('RTSP_BASE', 'rtsp://127.0.0.1:8554').rstrip('/')}/{slot['source']}"
    publish = f"{cfg('RTSP_BASE', 'rtsp://127.0.0.1:8554').rstrip('/')}/{slot_id}"
    return [
        binary, "-e",
        "rtspsrc", f"location={location}", "latency=200", "protocols=tcp", "name=src",
        "rtspclientsink", "name=pub", f"location={publish}", "protocols=tcp",
        "src.", "!", "application/x-rtp,media=video", "!", "decodebin", "!",
        "queue", "max-size-buffers=2", "leaky=downstream", "!",
        "videoconvert", "!", "videoscale", "!", "videorate", "!",
        "video/x-raw,width=640,height=360,framerate=25/1", "!",
        *encoder, "!",
        "rtph264pay", "config-interval=-1", "pt=96", "!", "queue", "!", "pub.",
        "src.", "!", "application/x-rtp,media=audio", "!", "decodebin", "!",
        "queue", "max-size-buffers=2", "leaky=downstream", "!",
        *preview_audio_args(slot["audio"]), "!",
        "rtpopuspay", "pt=97", "!", "queue", "!", "pub.",
    ]


def pipeline_args(slot_id: str) -> list[str]:
    slot = state[slot_id]
    device = SLOTS[slot_id]["device"]
    mode = slot["mode"]
    location = f"{cfg('RTSP_BASE', 'rtsp://127.0.0.1:8554').rstrip('/')}/{slot['source']}"
    decklink_mode = "1080p60" if mode == "1080p60" else "1080i50"
    # Gambar dan suara dipisah di rtspsrc. Satu decodebin untuk keduanya
    # membuat cabang audio gagal nyambung dan waktu pipeline tetap 0.
    return [
        cfg("GST_LAUNCH", "gst-launch-1.0"), "-v", "-e",
        "rtspsrc", f"location={location}", "latency=200", "protocols=tcp", "name=src",
        "src.", "!", "application/x-rtp,media=video", "!", "decodebin", "!", "queue", "!", "video/x-raw", "!",
        *video_args(mode), "!",
        "decklinkvideosink", f"device-number={device}", f"mode={decklink_mode}", "sync=false",
        "src.", "!", "application/x-rtp,media=audio", "!", "decodebin", "!", "queue", "!",
        *audio_args(slot["audio"]), "!",
        "decklinkaudiosink", f"device-number={device}", "sync=false", "async=false",
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
    run["status"] = "starting"
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
        if proc.poll() is None and run["status"] == "starting":
            run["status"] = "playing"
            run["restarts"] = 0


def watch_pipeline(slot_id: str, generation: int, proc: subprocess.Popen) -> None:
    proc.wait()
    with lock:
        run = runtime[slot_id]
        if run["generation"] != generation or run["proc"] is not proc:
            return
        run["proc"] = None
        if state[slot_id]["source"] and run["restarts"] < 3:
            run["restarts"] += 1
            run["status"] = "starting"
            threading.Thread(target=restart_later, args=(slot_id, generation), daemon=True).start()
            return
        run["status"] = "error"
        run["error"] = read_log(slot_id) or f"GStreamer berhenti (kode {proc.returncode})"


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
            "Preview SDI butuh encoder H.264. Di server jalankan: "
            "sudo apt install gstreamer1.0-plugins-ugly"
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
            env=inspect_env(args[0]),
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
        if state[slot_id]["source"] and run.get("preview_restarts", 0) < 3:
            run["preview_restarts"] = run.get("preview_restarts", 0) + 1
            threading.Thread(target=restart_preview_later, args=(slot_id, generation), daemon=True).start()
            return
        run["preview_error"] = read_log(slot_id, "-preview", 8) or f"Preview berhenti (kode {proc.returncode})"


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


def webrtc_base() -> str:
    explicit = cfg("MEDIAMTX_WEBRTC")
    if explicit:
        return explicit.rstrip("/")
    public = cfg("MEDIAMTX_PUBLIC", "http://172.17.20.46:8889")
    return public.replace(":8888", ":8889").rstrip("/")


def path_ready() -> dict[str, bool]:
    url = cfg("MEDIAMTX_API", "http://127.0.0.1:9997").rstrip("/") + "/v3/paths/list"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return {}
    items = payload.get("items", payload if isinstance(payload, list) else [])
    return {item.get("name"): bool(item.get("ready")) for item in items if isinstance(item, dict)}


def snapshot() -> dict:
    with lock:
        slots_copy = {slot_id: dict(slot) for slot_id, slot in state.items()}
        runs_copy = {
            slot_id: {
                "status": run["status"],
                "error": run["error"],
                "preview_error": run.get("preview_error"),
            }
            for slot_id, run in runtime.items()
        }
    ready = path_ready()
    web = webrtc_base()
    routed: dict[str, list[str]] = {}
    slots_out = {}
    for slot_id, meta in SLOTS.items():
        slot = slots_copy[slot_id]
        run = runs_copy[slot_id]
        if slot["source"]:
            routed.setdefault(slot["source"], []).append(meta["label"])
        slots_out[slot_id] = {
            "label": meta["label"],
            "device": meta["device"],
            "source": slot["source"],
            "mode": slot["mode"],
            "audio": slot["audio"],
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
                "id": source,
                "label": source,
                "ready": bool(ready.get(source)),
                "whep": f"{web}/{urllib.parse.quote(source)}/whep",
                "routedTo": routed.get(source, []),
            }
            for source in sources()
        ],
        "slots": slots_out,
    }


def jwt_payload(token: str) -> dict:
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def token_roles(token: str) -> set[str]:
    payload = jwt_payload(token)
    roles = set(payload.get("realm_access", {}).get("roles", []))
    client = cfg("KEYCLOAK_CLIENT_ID")
    roles.update(payload.get("resource_access", {}).get(client, {}).get("roles", []))
    return roles


def exchange_token(form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(
        keycloak_base() + "/token",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def session_from_token(token: dict) -> dict:
    access = token["access_token"]
    payload = jwt_payload(access)
    roles = token_roles(access)
    if roles.isdisjoint(allowed_roles()):
        raise PermissionError("Akun ini tidak punya role admin atau operator-feeddesk.")
    return {
        "user": payload.get("preferred_username") or payload.get("name") or "operator",
        "roles": sorted(roles & allowed_roles()),
        "access": access,
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
        return {"user": None, "roles": [], "local": True}
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


def apply_slot(slot_id: str, payload: dict) -> None:
    slot = state[slot_id]
    if "source" in payload:
        source = payload["source"]
        if source is not None and source not in sources():
            raise ValueError("Sumber tidak dikenal.")
        slot["source"] = source
    if "mode" in payload:
        if payload["mode"] not in MODES:
            raise ValueError("Mode video tidak dikenal.")
        slot["mode"] = payload["mode"]
    if "audio" in payload:
        if payload["audio"] not in AUDIO:
            raise ValueError("Peta audio tidak dikenal.")
        slot["audio"] = payload["audio"]
    runtime[slot_id]["restarts"] = 0
    runtime[slot_id]["preview_restarts"] = 0
    save_state()
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
        self.send_json(404, {"error": "Tidak ditemukan."})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        prefix = "/api/slots/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "Tidak ditemukan."})
            return
        slot_id = path[len(prefix):]
        if slot_id not in SLOTS:
            self.send_json(404, {"error": "Slot SDI tidak dikenal."})
            return
        session = current_session(self)
        if session is None:
            self.send_json(401, {"error": "Login diperlukan."})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 8000:
            self.send_json(400, {"error": "Permintaan terlalu besar."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json(400, {"error": "JSON tidak valid."})
            return
        try:
            with lock:
                apply_slot(slot_id, payload)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        body = snapshot()
        body["user"] = session.get("user")
        self.send_json(200, body)

    def handle_state(self) -> None:
        session = current_session(self)
        if session is None:
            self.send_json(401, {"error": "Login diperlukan."})
            return
        body = snapshot()
        body["user"] = session.get("user")
        self.send_json(200, body)

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
            self.send_html(400, message_page("Login gagal", message))
            return
        state_token = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        expires = oauth_states.pop(state_token, 0)
        if not code or expires < time.time():
            self.send_html(400, message_page("Login gagal", "Sesi login sudah tidak berlaku. Coba lagi."))
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
            self.send_html(403, message_page("Tidak punya akses", str(exc)))
            return
        except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError):
            self.send_html(502, message_page("Login gagal", "Keycloak tidak mengembalikan token."))
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
            self.send_json(404, {"error": "Tidak ditemukan."})
            return
        kind = "text/html" if target.suffix == ".html" else "text/css" if target.suffix == ".css" else "text/javascript"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def message_page(title: str, message: str) -> str:
    def esc(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<title>{esc(title)}</title>"
        "<body style=\"font-family:sans-serif;background:#101114;color:#eceae6;padding:2rem\">"
        f"<h1>{esc(title)}</h1><p>{esc(message)}</p><p><a style=\"color:#eceae6\" href=\"/\">Kembali</a></p>"
        "</body>"
    )


def main() -> None:
    global state
    load_env_file()
    apply_paths()
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
