"""Hands-free Jarvis: say "Hey Jarvis", speak a request, hear the reply.

Wake word (openWakeWord), speech-to-text (Whisper), the voice (Kokoro) and the
model that answers (Ollama, see brain.py) all run on this machine. Opens the
HUD in a window; pass --no-browser to skip that. Say "Hey Jarvis" or press Esc
in the HUD to interrupt; Ctrl+Alt+J starts listening without the wake word.

The HUD's microphone switch closes the microphone altogether: while it is off
nothing is captured, so the wake word does nothing and only typed requests work.
The switch follows the headphones: on when a pair connects, off when it goes.
"""
import ctypes
import os
import queue
import sys
import threading
import time
import winreg
from pathlib import Path

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from openwakeword.model import Model as WakeModel

import brain
import desktop
import learning
import study
import ui_server
import settings
from owner import USER
from speak import _kokoro
from speak_hook import to_speech
from ui_server import emit

ROOT = Path(__file__).parent
RESTART_REQUEST = ROOT / "restart-requested"  # left by Claude's tune-up after changing the program
RATE = 16000
FRAME = 1280  # 80 ms, the chunk size openWakeWord expects
MAX_SECONDS = 30
VOICE_RATE = 24000  # what the Kokoro voice produces
SENTENCE_GAP = 0.12  # seconds of quiet between sentences
# The wake threshold, the pause that ends a request, the follow-up window, the
# voice and its speed are settings Jarvis can change: see settings.py.
ENVELOPE_FPS = 30  # loudness samples per second sent to the HUD while speaking
HEADPHONE_POLL_SECONDS = 2
LECTURE_SECONDS = 30  # a lecture is put into words in pieces of about this length
# Windows keeps every audio output it has ever seen here, each with its current state
ENDPOINTS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
FORM_FACTOR = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},0"
HEADPHONE_FORMS = {3, 5}  # headphones, headset

frames: "queue.Queue[np.ndarray]" = queue.Queue()


def full_speed() -> None:
    """Tell Windows not to throttle this process. Jarvis has no window in front,
    so Windows treats it as background work and runs it at about a third of the
    speed, which makes the voice slower to produce than to play: pauses."""

    class Throttling(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]

    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    state = Throttling(1, 1, 0)  # control execution speed, and leave it unthrottled
    kernel.SetProcessInformation(ctypes.c_void_p(kernel.GetCurrentProcess()), 4, ctypes.byref(state), ctypes.sizeof(state))


def drain() -> None:
    while not frames.empty():
        frames.get_nowait()


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


def microphone() -> None:
    """Feed the microphone into `frames`. While muted the stream is closed, so
    the microphone is released rather than merely ignored."""

    def on_audio(indata, _frames, _time, _status) -> None:
        if not ui_server.muted.is_set():
            frames.put(indata[:, 0].copy())

    while True:
        ui_server.unmuted.wait()
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, callback=on_audio):
            ui_server.muted.wait()


def headphones_connected() -> bool:
    """Whether Windows has a live headphone or headset output, wired or Bluetooth."""
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ENDPOINTS) as root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            name = winreg.EnumKey(root, i)
            try:
                with winreg.OpenKey(root, name) as device:
                    if winreg.QueryValueEx(device, "DeviceState")[0] & 0xF != 1:  # 1 is active
                        continue
                with winreg.OpenKey(root, name + r"\Properties") as props:
                    if winreg.QueryValueEx(props, FORM_FACTOR)[0] in HEADPHONE_FORMS:
                        return True
            except OSError:
                continue
    return False


def follow_headphones() -> None:
    """Switch the microphone on when headphones connect and off when they go.
    The HUD's switch still works in between, until the headphones next change."""
    connected = None
    while True:
        now = headphones_connected()
        if now != connected:
            connected = now
            ui_server.set_muted(not connected)
        time.sleep(HEADPHONE_POLL_SECONDS)


def measure_noise(seconds: float = 1.0) -> float | None:
    """Typical loudness of the room, or None if the microphone went off meanwhile."""
    drain()
    try:
        levels = [rms(frames.get(timeout=1)) for _ in range(int(seconds * RATE / FRAME))]
    except queue.Empty:
        return None
    return float(np.median(levels))


def record_request(speech_level: float, start_timeout: float) -> np.ndarray | None:
    """Record until the speaker pauses. Returns None if nobody starts speaking
    in time, or if the microphone is switched off part-way."""
    drain()
    recorded: list[np.ndarray] = []
    started = False
    quiet = 0.0
    began = time.time()
    while time.time() - began < MAX_SECONDS:
        if ui_server.muted.is_set():
            return None
        try:
            frame = frames.get(timeout=0.1)
        except queue.Empty:
            continue
        recorded.append(frame)
        level = rms(frame)
        emit("level", value=round(min(level / (speech_level * 6), 1.0), 2))
        if level > speech_level:
            started = True
            quiet = 0.0
        else:
            quiet += FRAME / RATE
        if not started and time.time() - began > start_timeout:
            return None
        if started and quiet >= settings.get("silence_seconds"):
            break
    return np.concatenate(recorded).astype(np.float32) / 32768.0


class Player:
    """One audio output kept open for a whole reply. Opening the output afresh
    for every sentence leaves an audible gap, a long one on Bluetooth earbuds."""

    def __init__(self) -> None:
        self.pending = np.zeros(0, dtype=np.float32)
        self.lock = threading.Lock()
        self.stream = sd.OutputStream(samplerate=VOICE_RATE, channels=1, dtype="float32", callback=self._fill)
        self.stream.start()

    def _fill(self, out, count, _time, _status) -> None:
        with self.lock:
            take = min(len(self.pending), count)
            out[:take, 0] = self.pending[:take]
            out[take:, 0] = 0
            self.pending = self.pending[take:]

    def add(self, audio: np.ndarray) -> None:
        with self.lock:
            self.pending = np.concatenate([self.pending, audio.astype(np.float32)])

    def seconds_left(self) -> float:
        return len(self.pending) / VOICE_RATE

    def clear(self) -> None:
        with self.lock:
            self.pending = np.zeros(0, dtype=np.float32)

    def close(self, drain_output: bool) -> None:
        if drain_output:
            time.sleep(self.stream.latency + 0.05)  # let the last of it leave the speakers
        self.stream.stop()
        self.stream.close()


def voice(sentences: queue.Queue, wake: WakeModel, cancel: threading.Event, phone: bool = False) -> str:
    """Speak pieces of text as they arrive on `sentences` (None ends them), so a
    reply starts while the model is still writing the rest of it.

    Listens for the wake word throughout. Returns "wake" if cut short by it,
    "stopped" if cut short from the HUD, otherwise "done"; when cut short it
    sets `cancel` so whoever is producing the text stops too.

    With `phone` the speech is sent to the phone page to be played there, and
    the laptop stays silent; only the page's stop button cuts it short.
    """
    clips: queue.Queue = queue.Queue()

    def synthesise() -> None:
        while (text := sentences.get()) is not None:
            text = to_speech(text)
            if text and not cancel.is_set():
                audio, _ = _kokoro().create(
                    text, voice="bm_" + settings.get("voice"), speed=settings.get("speech_speed"), lang="en-gb"
                )
                clips.put(audio)
        clips.put(None)

    def interrupted() -> str | None:
        """Listens for up to a tenth of a second; says how it was interrupted, if it was."""
        if ui_server.stop.is_set():
            return "stopped"
        try:
            heard = max(wake.predict(frames.get(timeout=0.1)).values()) >= settings.get("wake_threshold")
        except queue.Empty:
            return None
        return "wake" if heard else None

    threading.Thread(target=synthesise, daemon=True).start()
    if phone:
        while True:
            if ui_server.stop.is_set():
                cancel.set()
                return "stopped"
            try:
                clip = clips.get(timeout=0.1)
            except queue.Empty:
                continue
            if clip is None:
                return "done"
            emit("state", state="speaking")
            ui_server.send_audio(clip, VOICE_RATE)
    wake.reset()
    drain()
    player = Player()  # opened now, while the model is still thinking, so the first words are not delayed
    more = True
    try:
        while more or player.seconds_left() > 0:
            if more and player.seconds_left() < 0.25:  # nearly out of sound: line up the next sentence
                try:
                    clip = clips.get_nowait()
                except queue.Empty:
                    clip = False
                if clip is None:
                    more = False
                elif clip is not False:
                    window = VOICE_RATE // ENVELOPE_FPS
                    count = len(clip) // window
                    envelope = np.sqrt(np.mean(clip[: count * window].reshape(count, window) ** 2, axis=1))
                    envelope = envelope / (envelope.max() or 1.0)
                    emit("state", state="speaking")
                    emit("speak", envelope=envelope.round(2).tolist(), fps=ENVELOPE_FPS)
                    player.add(np.concatenate([clip, np.zeros(int(VOICE_RATE * SENTENCE_GAP), dtype=np.float32)]))
            if why := interrupted():
                cancel.set()
                player.clear()
                player.close(drain_output=False)
                return why
        player.close(drain_output=True)
        return "done"
    except Exception:
        player.close(drain_output=False)
        raise


def _tone(notes: list[float], loudness: float) -> np.ndarray:
    """A short chime: each note a soft bell-like blip with a little overtone."""
    blips = []
    for pitch in notes:
        t = np.arange(int(VOICE_RATE * 0.11)) / VOICE_RATE
        wave = np.sin(2 * np.pi * pitch * t) + 0.3 * np.sin(2 * np.pi * pitch * 2 * t)
        fade = np.minimum(t / 0.008, 1.0) * np.exp(-t * 22)  # quick soft attack, then ringing away
        blips.append(wave * fade)
    return (np.concatenate(blips) * loudness).astype(np.float32)


# Audio-only mode: two rising notes mean "listening", the same two falling mean "processing",
# and one low note means "standing by" (no longer listening until the wake word)
CHIMES = {
    "listening": _tone([659.3, 880.0], 0.22),
    "processing": _tone([880.0, 659.3], 0.16),
    "standby": _tone([440.0], 0.18),
}


def chime(kind: str, wait: bool) -> None:
    """Play a chime if audio-only mode is on. With `wait`, return only once it has
    finished, so that the microphone does not record it."""
    if settings.get("audio_only") != "on":
        return

    def play() -> None:
        player = Player()
        player.add(CHIMES[kind])
        time.sleep(len(CHIMES[kind]) / VOICE_RATE)
        player.close(drain_output=True)

    if wait:
        play()
    else:
        threading.Thread(target=play, daemon=True).start()


def say(text: str, wake: WakeModel, phone: bool = False) -> str:
    """Speak one fixed piece of text. Returns as `voice` does."""
    sentences: queue.Queue = queue.Queue()
    sentences.put(text)
    sentences.put(None)
    return voice(sentences, wake, threading.Event(), phone)


def main() -> None:
    if sys.stdout is None:  # started without a console (pythonw), as the Windows startup shortcut does
        sys.stdout = sys.stderr = open(ROOT / "jarvis.log", "a", buffering=1, encoding="utf-8")
    hud = "--no-browser" not in sys.argv
    full_speed()
    try:
        ui_server.start()
    except SystemExit:
        if hud:
            desktop.open_hud()  # already running, probably since login: just show its window
        raise
    if hud:
        desktop.open_hud()
    print(f"HUD at {ui_server.URL}\nLoading models...")
    wake = WakeModel(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    whisper = WhisperModel("small.en", device="cpu", compute_type="int8")
    _kokoro()  # load the voice now so the first "Yes, sir?" isn't delayed
    brain.start()
    mind = brain.Brain()
    said: list[str | None] = [None]  # what Jarvis last replied, for the journal
    reflection: threading.Event | None = None  # set to cut short a reflection that is under way
    checked = 0.0  # when it was last considered whether to reflect

    def deliver(thought, phone: bool = False) -> str:
        """Run `thought(cancel)` on the model, voicing each sentence as it is
        produced (on the phone, with `phone`). Returns how it ended: "done",
        "wake" or "stopped"."""
        ui_server.stop.clear()
        emit("state", state="thinking")
        sentences: queue.Queue = queue.Queue()
        cancel = threading.Event()
        mind.speak = sentences.put
        if reflection is not None:
            reflection.set()  # The user comes first: stop thinking over the journal

        def work() -> None:
            try:
                reply = thought(cancel)
            except Exception as error:  # the model server failing must not take the listener down
                print(f"brain: {error}", file=sys.stderr)
                learning.note_problem("error while answering", str(error))
                reply = "I'm afraid my own thinking has failed me, sir. Ollama may not be running."
                sentences.put(reply)
            said[0] = reply
            if reply and not cancel.is_set():
                print(f"Jarvis: {reply}\n")
                emit("reply", text=reply)
            sentences.put(None)

        threading.Thread(target=work, daemon=True).start()
        outcome = voice(sentences, wake, cancel, phone)
        if outcome != "done":
            print("(interrupted)\n")
        return outcome

    def transcribe(audio: np.ndarray) -> str:
        segments, _ = whisper.transcribe(audio, language="en", vad_filter=True, beam_size=1, hotwords=learning.vocabulary())
        return " ".join(s.text.strip() for s in segments).strip()

    def respond(prompt: str, phone: bool = False) -> str:
        """Answer a request aloud. Returns as `deliver` does."""
        print(f"You: {prompt}")
        emit("heard", text=prompt)
        outcome = deliver(lambda cancel: mind.ask(prompt, cancel), phone)
        learning.journal(prompt, said[0], outcome)
        if mind.quitting:  # The user asked Jarvis to close, and the goodbye has been spoken
            desktop.shut_down()
        return outcome

    def announce(job: dict) -> str:
        """Tell the user about a finished background job or a timer. Returns as `deliver` does."""
        if job["kind"] == "command":  # raw command output needs putting into words
            return deliver(lambda cancel: mind.report(job, cancel))
        outcome = deliver(lambda cancel: mind.tell(job["output"]))  # already written for speaking
        if job.get("restart"):  # Claude changed Jarvis's program: start again to use it
            say("Restarting to take up the change, sir.", wake)
            desktop.restart()
        return outcome

    def converse(greet: bool) -> None:
        """Take spoken requests until the user stops talking."""
        ui_server.stop.clear()
        learning.last_activity = time.time()  # background work stands aside from the first word
        timeout = 5.0
        while True:
            # also after an interruption, so there is a cue that the microphone is open
            # In audio-only mode the rising chime is that cue, and replaces the spoken one.
            if greet and settings.get("audio_only") != "on" and say("Yes, sir?", wake) == "stopped":
                return
            chime("listening", wait=True)
            emit("state", state="listening")
            audio = record_request(speech_level, timeout)
            if audio is None:
                return
            chime("processing", wait=False)
            emit("state", state="thinking")
            prompt = transcribe(audio)
            if not prompt:
                return
            outcome = respond(prompt)
            if outcome == "stopped":
                return
            greet = outcome == "wake"
            timeout = 5.0 if greet else settings.get("follow_up_seconds")

    def think_it_over() -> None:
        """While nothing is happening, read back over the journal for things worth remembering."""
        nonlocal reflection, checked
        if reflection is not None or time.time() - checked < 30:
            return
        checked = time.time()
        if RESTART_REQUEST.exists() and time.time() - learning.last_activity > 120:
            # Claude's weekly tune-up changed the program while Jarvis was running: take up the change
            RESTART_REQUEST.unlink()
            desktop.restart()
        if not learning.due_for_reflection():
            return
        reflection = threading.Event()

        def work() -> None:
            nonlocal reflection
            try:
                kept = learning.reflect(reflection)
                if kept:
                    print(f"(while idle, learned {kept} new thing{'s' if kept != 1 else ''} about {USER})")
            except Exception as error:
                print(f"learning: {error}", file=sys.stderr)
            learning.last_activity = time.time()  # whether it finished or was cut short, wait a while before trying again
            reflection = None

        threading.Thread(target=work, daemon=True).start()

    def from_phone(request: dict) -> str:
        """Answer a request typed or spoken into the phone page, where the reply is heard."""
        ui_server.stop.clear()
        learning.last_activity = time.time()
        prompt = request.get("text")
        if prompt is None:
            emit("state", state="thinking")
            prompt = transcribe(request["audio"].astype(np.float32) / 32768.0)
        if prompt:
            respond(prompt, phone=True)
        else:
            emit("reply", text="I didn't catch that, sir.")
            say("I didn't catch that, sir.", wake, phone=True)
        return "phone"

    def waiting() -> str | None:
        """The next thing to deal with while idle, as (how it ended), or None if nothing is waiting."""
        try:
            return announce(brain.finished.get_nowait())
        except queue.Empty:
            pass
        try:
            return from_phone(ui_server.remote.get_nowait())
        except queue.Empty:
            pass
        try:
            return respond(ui_server.commands.get_nowait())
        except queue.Empty:
            pass
        if desktop.hotkey.is_set():
            desktop.hotkey.clear()
            ui_server.set_muted(False)  # push-to-talk opens the microphone even without headphones
            time.sleep(0.3)  # let the microphone stream open
            return "wake"
        return None

    heard: list[np.ndarray] = []  # the lecture being taken down, since the last piece was handed over
    pieces: queue.Queue = queue.Queue()  # (transcript, sound) waiting to be put into words
    transcript: Path | None = None  # where the lecture being taken down is written

    def write_out() -> None:
        """Hand over what has been heard of the lecture, to be put into words in the background."""
        if heard:
            pieces.put((transcript, np.concatenate(heard).astype(np.float32) / 32768.0))
            heard.clear()

    def take_down() -> None:
        while True:
            path, sound = pieces.get()
            try:
                if sound is None:  # the lecture is over, and all of it is now written down
                    if path.exists():
                        os.startfile(path)
                        os.startfile(brain.summarise_lecture(path))
                        brain.finished.put({"kind": "say", "output": "Sir, the summary of the lecture is on your screen."})
                    continue
                text = transcribe(sound)
                if text:
                    study.note_lecture(path, text)
            except Exception as error:
                print(f"lecture: {error}", file=sys.stderr)

    threading.Thread(target=take_down, daemon=True).start()
    threading.Thread(target=microphone, daemon=True).start()
    threading.Thread(target=follow_headphones, daemon=True).start()
    desktop.start_hotkey()
    desktop.start_tray()
    learning.start()
    # Course files are indexed only while the user is not using Jarvis; scans and lecture
    # recordings, which are heavy work, only after ten quiet minutes.
    study.start(
        busy=lambda: time.time() - learning.last_activity < 60,
        resting=lambda: time.time() - learning.last_activity > 600,
        transcriber=whisper,
        vocabulary=learning.vocabulary,
    )
    speech_level = 300.0  # until the room has been measured, which needs the microphone on
    calibrated = False
    emit("state", state="idle")
    print('Ready. Say "Hey Jarvis".')
    while True:
        if not calibrated and not ui_server.muted.is_set():
            noise = measure_noise()
            if noise is not None:
                speech_level, calibrated = max(noise * 3, 300), True
        outcome = waiting()
        if outcome is None:
            think_it_over()
            taking = study.lecture()
            if taking is not None:
                transcript = taking
            elif transcript is not None:  # note-taking has just ended: show the transcript and a summary
                write_out()
                pieces.put((transcript, None))
                transcript = None
            try:
                frame = frames.get(timeout=0.2)
            except queue.Empty:
                write_out()  # the microphone has gone off: keep what was heard of the lecture
                continue
            if taking is not None:
                heard.append(frame)
                seconds = len(heard) * FRAME / RATE
                # cut at a pause where there is one, so that words are not split
                if seconds >= LECTURE_SECONDS or (seconds >= LECTURE_SECONDS / 2 and rms(frame) < speech_level):
                    write_out()
            if max(wake.predict(frame).values()) < settings.get("wake_threshold"):
                continue
            outcome = "wake"
            if reflection is not None:
                reflection.set()
        if outcome == "wake":
            converse(greet=True)
        if outcome != "phone":  # the laptop took no part, so it gives no cue
            chime("standby", wait=True)
        wake.reset()
        drain()
        emit("state", state="idle")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
