"""Jarvis's knowledge of the user's course material.

Reads the documents in the user's school folder (PDF, Word, PowerPoint, text),
cuts them into passages and gives each a numerical fingerprint (an embedding)
through Ollama. A question is then matched against the passages by meaning,
and the closest are handed to the language model to answer from. Everything
stays on this laptop. The index is built and kept up to date in the background.
"""
import base64
import ctypes
import datetime
import hashlib
import io
import json
import logging
import re
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import numpy as np

import skills

logging.getLogger("pypdf").setLevel(logging.ERROR)  # it is talkative about fonts, which do not matter here

ROOT = Path(__file__).parent
INDEX =ROOT / "study-index"  # passages.json (text and where each came from) and vectors.npy
EMBEDDER = "nomic-embed-text"
HOST = "http://127.0.0.1:11434"
DEFAULT_FOLDERS = [str(Path.home() / "Documents" / "School")]  # override with "study_folders" in config.json
READABLE = {".pdf", ".docx", ".pptx", ".txt", ".md"}
PASSAGE_CHARS = 900
MAX_FILE_MB = 60
BATCH = 16
RELEVANT = 0.65  # how close a passage must be to the question to be worth showing the model
RESCAN_SECONDS = 600
MIN_TEXT = 80  # a page or passage with fewer characters than this has nothing worth indexing
RECORDINGS = {".mp4", ".m4a", ".mp3", ".wav", ".mkv", ".mov", ".webm", ".aac"}
MAX_RECORDING_MB = 3000
VISION = "qwen2.5vl:3b"  # reads scanned and handwritten pages; too big to share the graphics card with the language model
SCAN_PROMPT = (
    "This is a page of lecture notes or a quiz, possibly handwritten. Transcribe all of the writing exactly as it appears, "
    "line by line, keeping headings, lists, equations and labels. Where there is a diagram, describe it in one short sentence "
    "inside square brackets. Do not add commentary. Output only the transcription."
)
SCAN_WARNING = "(Read by machine from a scanned or handwritten page; some words may be misread.) "

_lock = threading.Lock()
_passages: list[dict] = []
_vectors = np.zeros((0, 768), dtype=np.float32)
status = "not started"


def folders() -> list[Path]:
    return [Path(p) for p in skills._config().get("study_folders", DEFAULT_FOLDERS)]


# ---- reading files -------------------------------------------------------------

def _scan_text(path: Path, number: int) -> Path:
    """Where the text read off a scanned page is kept. A changed file gets a fresh place."""
    key = hashlib.sha1(f"{path}|{path.stat().st_mtime}".encode()).hexdigest()[:16]
    return INDEX / "pages" / key / f"{number}.txt"


def _pdf(path: Path) -> list[tuple[str, str]]:
    from pypdf import PdfReader

    pages = []
    for number, page in enumerate(PdfReader(str(path)).pages, 1):
        text, where = page.extract_text() or "", f"page {number}"
        if len(text.strip()) < MIN_TEXT and _scan_text(path, number).exists():  # a scan or handwriting, read by the vision model
            text, where = SCAN_WARNING + _scan_text(path, number).read_text(encoding="utf-8"), f"page {number}, read from a scan"
        pages.append((where, text))
    return pages


def _office(path: Path, inner: str, label: str) -> list[tuple[str, str]]:
    """Text from a .docx or .pptx, which are zip files of XML: every text run, a paragraph to a line."""
    parts = []
    with zipfile.ZipFile(path) as archive:
        names = sorted((n for n in archive.namelist() if re.fullmatch(inner, n)), key=lambda n: int(re.sub(r"\D", "", n) or 0))
        for number, name in enumerate(names, 1):
            lines = []
            for paragraph in ElementTree.fromstring(archive.read(name)).iter():
                if paragraph.tag.endswith("}p"):
                    text = "".join(node.text or "" for node in paragraph.iter() if node.tag.endswith("}t"))
                    if text.strip():
                        lines.append(text)
            parts.append((f"{label} {number}" if label else "", "\n".join(lines)))
    return parts


def read(path: Path) -> list[tuple[str, str]]:
    """(where in the file, text) for each page, slide or whole document."""
    kind = path.suffix.lower()
    if kind == ".pdf":
        return _pdf(path)
    if kind == ".pptx":
        return _office(path, r"ppt/slides/slide\d+\.xml", "slide")
    if kind == ".docx":
        return _office(path, r"word/document\.xml", "")
    return [("", path.read_text(encoding="utf-8", errors="replace"))]


def passages(path: Path) -> list[dict]:
    """A file cut into passages of a few sentences, each knowing where it came from."""
    course = next((part for part in path.parts if re.match(r"[A-Z]{4} \d{4}", part)), path.parent.name)
    found = []
    for where, text in read(path):
        text = re.sub(r"[ \t]+", " ", text).strip()
        if len(text) < MIN_TEXT:  # a blank or unread scanned page, or a title slide
            continue
        pieces, current = [], ""
        for paragraph in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", text):
            if current and len(current) + len(paragraph) > PASSAGE_CHARS:
                pieces.append(current)
                current = ""
            current = f"{current} {paragraph}".strip()
            while len(current) > PASSAGE_CHARS * 1.5:  # a single enormous paragraph
                pieces.append(current[:PASSAGE_CHARS])
                current = current[PASSAGE_CHARS:]
        if current:
            pieces.append(current)
        found += [{"file": str(path), "name": path.name, "course": course, "where": where, "text": piece} for piece in pieces if len(piece) >= 80]
    return found


# ---- embeddings ----------------------------------------------------------------

def _embed(texts: list[str], purpose: str) -> np.ndarray:
    """Fingerprints for the texts. `purpose` is 'search_document' or 'search_query', which this embedder expects to be told."""
    # num_gpu 0 keeps the embedder on the processor: the graphics card is full of the
    # language model, and making room there would unload it and stall the next reply.
    body = {"model": EMBEDDER, "input": [f"{purpose}: {text[:2000]}" for text in texts], "keep_alive": "30m", "options": {"num_gpu": 0}}
    request = urllib.request.Request(HOST + "/api/embed", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    vectors = np.array(json.loads(urllib.request.urlopen(request, timeout=300).read())["embeddings"], dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def _load() -> None:
    global _passages, _vectors
    try:
        _passages = json.loads((INDEX / "passages.json").read_text(encoding="utf-8"))
        _vectors = np.load(INDEX / "vectors.npy")
    except (OSError, ValueError):
        _passages, _vectors = [], np.zeros((0, 768), dtype=np.float32)


def _save() -> None:
    INDEX.mkdir(exist_ok=True)
    (INDEX / "passages.json").write_text(json.dumps(_passages, ensure_ascii=False), encoding="utf-8")
    np.save(INDEX / "vectors.npy", _vectors)


def update(busy=lambda: False) -> str:
    """Bring the index in line with the folders: read new and changed files, drop ones that have gone.
    `busy()` is asked between batches; while it says the user is using Jarvis, the work waits."""
    global _passages, _vectors, status
    wanted = {}
    for folder in folders():
        for path in folder.rglob("*"):
            try:
                if path.suffix.lower() in READABLE and path.is_file() and path.stat().st_size < MAX_FILE_MB * 2**20:
                    wanted[str(path)] = path.stat().st_mtime
            except OSError:
                continue
    try:
        known = json.loads((INDEX / "files.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    gone = [file for file in known if file not in wanted or known[file] != wanted[file]]
    if gone:
        with _lock:
            keep = [i for i, p in enumerate(_passages) if p["file"] not in gone]
            _passages, _vectors = [_passages[i] for i in keep], _vectors[keep]
        for file in gone:
            known.pop(file)
    todo = [file for file in wanted if file not in known]
    for count, file in enumerate(todo, 1):
        status = f"reading {count} of {len(todo)}: {Path(file).name}"
        try:
            new = passages(Path(file))
        except Exception as error:  # a damaged or password-protected file is skipped, not fatal
            print(f"study: could not read {Path(file).name}: {error}")
            new = []
        vectors = []
        for start in range(0, len(new), BATCH):
            while busy():
                time.sleep(2)
            vectors.append(_embed([p["text"] for p in new[start:start + BATCH]], "search_document"))
        with _lock:
            if new:
                _passages += new
                _vectors = np.vstack([_vectors, *vectors])
            known[file] = wanted[file]
            if count % 10 == 0 or count == len(todo):
                _save()
                (INDEX / "files.json").write_text(json.dumps(known), encoding="utf-8")
    if gone and not todo:
        with _lock:
            _save()
            (INDEX / "files.json").write_text(json.dumps(known), encoding="utf-8")
    status = f"{len(known)} files, {len(_passages)} passages"
    return status


def search(question: str, limit: int = 4) -> list[tuple[float, dict]]:
    """The passages closest in meaning to the question, best first, with how close each is (0 to 1)."""
    with _lock:
        if not len(_passages):
            return []
        scores = _vectors @ _embed([question], "search_query")[0]
        best = np.argsort(scores)[::-1][:limit * 3]
        picked, seen = [], set()
        for i in best:  # skip near-identical passages, which duplicate downloads produce
            key = _passages[i]["text"][:120]
            if key not in seen:
                seen.add(key)
                picked.append((float(scores[i]), _passages[i]))
        return picked[:limit]


def notes_for(question: str) -> tuple[str, list[str]]:
    """Passages from the user's course material that bear on the question, laid out for the model,
    and the documents they came from as they should be said aloud. Empty if nothing is close."""
    try:
        found = [(score, p) for score, p in search(question, limit=3) if score >= RELEVANT]
    except Exception as error:  # the embedder not being available must never stop Jarvis answering
        print(f"study: {error}")
        return "", []
    text = "\n\n".join(f"[{p['course']}, {p['name']}{', ' + p['where'] if p['where'] else ''}]\n{p['text']}" for _, p in found)
    spoken = lambda name: re.sub(r"\s+", " ", re.sub(r"\.\w+$|\(\d+\)|%20|[-_]", " ", name)).strip()
    return text, list(dict.fromkeys(spoken(p["name"]) for _, p in found))[:2]


def _files(kinds: set) -> list[Path]:
    found = []
    for folder in folders():
        for path in folder.rglob("*"):
            try:
                if path.suffix.lower() in kinds and path.is_file():
                    found.append(path)
            except OSError:
                continue
    return found


def _read_again(path: Path) -> None:
    """Have the next update re-read this file, because more of it can now be read."""
    try:
        known = json.loads((INDEX / "files.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if known.pop(str(path), None) is not None:
        global _passages, _vectors
        with _lock:
            keep = [i for i, p in enumerate(_passages) if p["file"] != str(path)]
            _passages, _vectors = [_passages[i] for i in keep], _vectors[keep]
            (INDEX / "files.json").write_text(json.dumps(known), encoding="utf-8")


def _vision(prompt: str, image: bytes | None, keep: str) -> str:
    body = {"model": VISION, "prompt": prompt, "stream": False, "keep_alive": keep, "options": {"temperature": 0, "num_ctx": 4096}}
    if image is not None:
        body["images"] = [base64.b64encode(image).decode()]
    request = urllib.request.Request(HOST + "/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(request, timeout=600).read()).get("response", "")


def read_scans(busy=lambda: False) -> int:
    """Read the scanned and handwritten PDF pages that have no text in them, using the vision model.
    Stops as soon as the user is using Jarvis, because the vision model pushes the language model off the graphics card."""
    global status
    import pypdfium2
    from pypdf import PdfReader

    done = 0
    for path in _files({".pdf"}):
        try:
            if path.stat().st_size > MAX_FILE_MB * 2**20:
                continue
            blank = [n for n, page in enumerate(PdfReader(str(path)).pages, 1)
                     if len((page.extract_text() or "").strip()) < MIN_TEXT and not _scan_text(path, n).exists()]
        except Exception:
            continue
        if not blank:
            continue
        document = pypdfium2.PdfDocument(str(path))
        for number in blank:
            if busy():
                break
            status = f"reading scanned page {number} of {path.name}"
            picture = io.BytesIO()
            document[number - 1].render(scale=1.6).to_pil().convert("L").save(picture, format="PNG")
            text = _vision(SCAN_PROMPT, picture.getvalue(), "5m").strip()
            target = _scan_text(path, number)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")  # kept even if empty, so a blank page is not tried again
            done += 1
        document.close()
        _read_again(path)
        if busy():
            break
    if done:
        try:
            _vision("", None, "0")  # let go of the graphics card straight away
        except OSError:
            pass
    return done


def _sound(path: Path) -> np.ndarray:
    """The audio of a recording (any common audio or video file) as the speech recogniser wants it:
    one channel at 16 kHz. Done here because the recogniser's own file reader fails with the installed PyAV."""
    import av

    pieces = []
    with av.open(str(path)) as container:
        track = next(stream for stream in container.streams if stream.type == "audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(track):
            pieces += [out.to_ndarray().reshape(-1) for out in resampler.resample(frame)]
        pieces += [out.to_ndarray().reshape(-1) for out in resampler.resample(None)]
    return np.concatenate(pieces).astype(np.float32) / 32768.0


def transcribe_recordings(transcriber, busy=lambda: False, vocabulary=lambda: "") -> int:
    """Turn lecture recordings in the study folders into text files beside them, which then get indexed like any other notes."""
    global status
    done = 0
    for path in _files(RECORDINGS):
        target = path.with_name(f"{path.stem} (transcript).txt")
        try:
            if path.stat().st_size > MAX_RECORDING_MB * 2**20 or (target.exists() and target.stat().st_mtime >= path.stat().st_mtime):
                continue
        except OSError:
            continue
        if busy():
            break
        status = f"transcribing {path.name}"
        try:
            sound = _sound(path)
        except Exception as error:  # no audio track, or a file that will not open
            print(f"study: could not read the sound of {path.name}: {error}")
            target.write_text(f"Jarvis could not read the sound of {path.name}: {error}\n", encoding="utf-8")
            continue
        segments, _ = transcriber.transcribe(sound, language="en", vad_filter=True, beam_size=1, hotwords=vocabulary() or None)
        paragraphs, current, started = [], [], 0.0
        for segment in segments:  # produced one at a time, so the work can pause between them
            while busy():
                time.sleep(2)
            if current and segment.start - started > 60:
                paragraphs.append(f"[{int(started // 60)}:{int(started % 60):02d}] {' '.join(current)}")
                current = []
            if not current:
                started = segment.start
            current.append(segment.text.strip())
        if current:
            paragraphs.append(f"[{int(started // 60)}:{int(started % 60):02d}] {' '.join(current)}")
        header = (f"Transcript of {path.name}, made by Jarvis on {datetime.date.today():%d %B %Y}.\n"
                  "Machine transcription: expect some wrong words, especially names and symbols. Times are minutes:seconds into the recording.\n\n")
        target.write_text(header + "\n\n".join(paragraphs) + "\n", encoding="utf-8")
        done += 1
    return done


LECTURE_FLAG = ROOT / "lecture-mode"  # exists while Jarvis is taking a lecture down, so it carries on across restarts
LECTURE_HOURS = 3  # a lecture left running is ended after this long


def lecture(on: bool | None = None) -> Path | None:
    """Start or stop taking a lecture down, with `on`. Returns the transcript being written, or None when there is none."""
    if on:
        if lecture() is None:
            LECTURE_FLAG.touch()
        watch_lecture(lecture())  # also when already taking notes: asking again brings the window back
    elif on is False:
        LECTURE_FLAG.unlink(missing_ok=True)
    try:
        began = datetime.datetime.fromtimestamp(LECTURE_FLAG.stat().st_mtime)
    except OSError:
        return None
    if datetime.datetime.now() - began > datetime.timedelta(hours=LECTURE_HOURS):
        LECTURE_FLAG.unlink(missing_ok=True)
        return None
    return folders()[0] / "Lecture transcripts" / f"Lecture {began:%Y-%m-%d %H%M} (transcript).txt"


def note_lecture(path: Path, text: str) -> None:
    """Add what was just heard to a lecture transcript, under the time of day. It is indexed like any other notes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"Transcript of a lecture, taken down live by Jarvis on {datetime.date.today():%d %B %Y}.\n"
                        "Machine transcription: expect some wrong words, especially names and symbols.\n\n", encoding="utf-8")
    if text:
        with path.open("a", encoding="utf-8") as out:
            out.write(f"[{datetime.datetime.now():%H:%M}] {text}\n\n")


def watch_lecture(path: Path) -> None:
    """Open a window that shows a lecture transcript and follows it as it grows."""
    note_lecture(path, "")  # the window needs the file to be there
    if ctypes.windll.user32.FindWindowW(None, "Lecture notes (live)"):
        return  # already open
    follow = f"$Host.UI.RawUI.WindowTitle = 'Lecture notes (live)'; Get-Content -LiteralPath '{path}' -Encoding UTF8 -Wait"
    subprocess.Popen(["powershell", "-NoProfile", "-NoExit", "-Command", follow], creationflags=subprocess.CREATE_NEW_CONSOLE)


def start(busy=lambda: False, resting=lambda: True, transcriber=None, vocabulary=lambda: "") -> None:
    """Load the saved index, then keep it up to date in the background. Indexing waits
    while `busy()`; the heavy jobs (reading scans, transcribing recordings) only begin once `resting()`."""
    _load()

    def keep_up() -> None:
        global status
        while True:
            jobs = [lambda: update(busy)]
            if resting():  # each job on its own, so that one failing (the vision model missing, say) does not stop the others
                jobs = [
                    lambda: read_scans(busy=lambda: not resting()),
                    lambda: transcribe_recordings(transcriber, busy, vocabulary) if transcriber is not None else 0,
                ] + jobs
            for job in jobs:
                try:
                    job()
                except Exception as error:
                    print(f"study: {error}")
                    status = f"{len(_passages)} passages (the last pass hit an error)"
            time.sleep(RESCAN_SECONDS)

    threading.Thread(target=keep_up, daemon=True).start()
