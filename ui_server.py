"""Local web server for the Jarvis HUD: serves the page, streams events to it,
and accepts typed requests from it. Listens on this machine only.

The phone reaches it through Tailscale ("tailscale serve"), which passes
requests from the user's own devices on to this port. Whatever arrives that way is
remote: it is given the phone page, has to sign in with the passcode, and hears
replies on the phone instead of through the laptop."""
import hashlib
import hmac
import io
import itertools
import json
import os
import queue
import secrets
import threading
import time
import wave
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

PORT = int(os.environ.get("JARVIS_PORT", 8765))
URL = f"http://127.0.0.1:{PORT}"
ALLOWED_ORIGINS = {URL, f"http://localhost:{PORT}"}
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
FORWARDED = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Tailscale-User-Login")  # added by tailscale serve
INDEX = Path(__file__).parent / "ui" / "index.html"
PHONE = INDEX.parent / "phone.html"

MUTE_FLAG = Path(__file__).parent / "mic-muted"  # exists while the microphone is switched off, so it stays off across restarts
AUTH_FILE = Path(__file__).parent / "phone-auth.json"  # the passcode (hashed) and the phones signed in; delete it to start again
COOKIE = "jarvis_session"
MIN_PASSCODE = 4
MAX_TRIES, LOCKOUT_SECONDS = 5, 300  # wrong passcodes allowed before sign-in is refused for a while
MAX_VOICE_BYTES = 16000 * 2 * 45  # 45 seconds of the 16 kHz 16-bit speech the phone page sends
KEPT_CLIPS = 64  # pieces of speech held for the phone to fetch

commands: "queue.Queue[str]" = queue.Queue()  # requests typed into the HUD
remote: "queue.Queue[dict]" = queue.Queue()  # requests from the phone page: {"text": ...} or {"audio": 16 kHz samples}
stop = threading.Event()  # set when the HUD asks Jarvis to stop talking or working
muted = threading.Event()  # set while the HUD's microphone switch is off
unmuted = threading.Event()  # the opposite of muted, so the listener can wait for either
_clients: "set[queue.Queue]" = set()
_lock = threading.Lock()
_last_state = {"type": "state", "state": "loading"}
_clips: "dict[int, bytes]" = {}
_clip_ids = itertools.count(1)
_auth_lock = threading.Lock()
_failures = 0
_locked_until = 0.0


def set_muted(off: bool) -> None:
    """Switch the microphone off or on, and tell every open HUD."""
    if off:
        MUTE_FLAG.touch()
        unmuted.clear()
        muted.set()
    else:
        MUTE_FLAG.unlink(missing_ok=True)
        muted.clear()
        unmuted.set()
    emit("mic", muted=off)


def emit(type: str, **data) -> None:
    """Send an event to every open HUD."""
    global _last_state
    event = {"type": type, **data}
    if type == "state":
        _last_state = event
    with _lock:
        for client in _clients:
            client.put(event)


def send_audio(clip: np.ndarray, rate: int) -> None:
    """Hand a piece of speech to the phone page, which fetches it and plays it."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((np.clip(clip, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    with _lock:
        number = next(_clip_ids)
        _clips[number] = buffer.getvalue()
        for old in list(_clips)[:-KEPT_CLIPS]:
            del _clips[old]
    emit("audio", id=number)


def _auth() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _hash(passcode: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", passcode.encode(), bytes.fromhex(salt), 200_000).hex()


def sign_in(passcode: str) -> "tuple[int, str | None]":
    """Check the passcode, or set it if there is none yet. Returns an HTTP status
    and, on success, the token that keeps that phone signed in."""
    global _failures, _locked_until
    with _auth_lock:
        if time.time() < _locked_until:
            return 429, None
        auth = _auth()
        if "hash" not in auth:  # first visit: this becomes the passcode
            if len(passcode) < MIN_PASSCODE:
                return 400, None
            salt = secrets.token_hex(16)
            auth = {"salt": salt, "hash": _hash(passcode, salt), "sessions": []}
        elif not hmac.compare_digest(_hash(passcode, auth["salt"]), auth["hash"]):
            _failures += 1
            if _failures >= MAX_TRIES:
                _failures, _locked_until = 0, time.time() + LOCKOUT_SECONDS
            return 401, None
        _failures = 0
        token = secrets.token_urlsafe(32)
        auth["sessions"] = (auth["sessions"] + [hashlib.sha256(token.encode()).hexdigest()])[-5:]
        AUTH_FILE.write_text(json.dumps(auth))
        return 200, token


def signed_in(token: str) -> bool:
    return hashlib.sha256(token.encode()).hexdigest() in _auth().get("sessions", [])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def is_remote(self) -> bool:
        """Whether the request came from another device rather than from the HUD on this machine."""
        return self.headers.get("Host") not in LOCAL_HOSTS or any(self.headers.get(name) for name in FORWARDED)

    def authorised(self) -> bool:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        return COOKIE in cookie and signed_in(cookie[COOKIE].value)

    def reply(self, status: int, body: bytes = b"", content_type: str = "", cookie: str = "") -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path, away = self.path.split("?")[0], self.is_remote()
        if path == "/":
            self.reply(200, (PHONE if away else INDEX).read_bytes(), "text/html; charset=utf-8")
        elif path == "/phone":  # the phone page, for trying on the laptop
            self.reply(200, PHONE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/icon.png":
            self.reply(200, (INDEX.parent / "jarvis.png").read_bytes(), "image/png")
        elif path == "/session":
            state = {"set": "hash" in _auth(), "ok": not away or self.authorised()}
            self.reply(200, json.dumps(state).encode(), "application/json")
        elif away and not self.authorised():
            self.reply(401)
        elif path == "/events":
            self.stream_events()
        elif path.startswith("/audio/"):
            clip = _clips.get(int(path[7:])) if path[7:].isdigit() else None
            self.reply(200, clip, "audio/wav") if clip else self.reply(404)
        else:
            self.reply(404)

    def stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        client: queue.Queue = queue.Queue()
        client.put(_last_state)
        client.put({"type": "mic", "muted": muted.is_set()})
        with _lock:
            _clients.add(client)
        try:
            while True:
                try:
                    line = f"data: {json.dumps(client.get(timeout=15))}\n\n"
                except queue.Empty:
                    line = ": keep-alive\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass
        finally:
            with _lock:
                _clients.discard(client)

    def do_POST(self) -> None:
        path, away = self.path, self.is_remote()
        # The phone page marks its requests with a header that other websites cannot
        # add, and its replies are spoken on the phone.
        phone = self.headers.get("X-Jarvis") == "phone"
        origin = self.headers.get("Origin")
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_VOICE_BYTES:
            self.reply(413)
            return
        if path == "/auth" and phone:
            try:
                status, token = sign_in(str(json.loads(self.rfile.read(length))["passcode"]))
            except (ValueError, KeyError, TypeError):
                status, token = 400, None
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            cookie = f"{COOKIE}={token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict{secure}" if token else ""
            self.reply(204 if token else status, cookie=cookie)
            return
        if away:
            # Only the signed-in phone page, and never the laptop's microphone switch.
            if not phone or path == "/mic":
                self.reply(403)
                return
            if not self.authorised():
                self.reply(401)
                return
        elif origin and origin not in ALLOWED_ORIGINS:
            # Only the HUD itself may submit requests, not other pages open in a browser.
            self.reply(403)
            return
        if path not in ("/ask", "/stop", "/mic", "/voice") or (path == "/voice" and not phone):
            self.reply(403)
            return
        if path == "/stop":
            stop.set()
            self.reply(204)
            return
        if path == "/voice":
            audio = self.rfile.read(length)
            if audio:
                remote.put({"audio": np.frombuffer(audio[: len(audio) // 2 * 2], dtype="<i2")})
            self.reply(204)
            return
        try:
            body = json.loads(self.rfile.read(length))
            text = "" if path == "/mic" else str(body["text"]).strip()
            if path == "/mic":
                set_muted(bool(body["muted"]))
        except (ValueError, KeyError, TypeError):
            self.reply(400)
            return
        if text and phone:
            remote.put({"text": text[:2000]})
        elif text:
            commands.put(text[:2000])
        self.reply(204)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # so a second Jarvis fails to start instead of sharing the port
    daemon_threads = True


def start() -> None:
    (muted if MUTE_FLAG.exists() else unmuted).set()
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        raise SystemExit(f"Jarvis is already running (port {PORT} is in use). Close the other one first.")
    threading.Thread(target=server.serve_forever, daemon=True).start()
