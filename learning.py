"""How Jarvis learns in the background. The language model itself cannot be
retrained here, so "learning" means four things it keeps on disk:

- a journal of every exchange (journal.jsonl);
- facts about the user picked out of that journal while Jarvis is idle, each kept
  only if the user's own words back it up (learned-by-jarvis.md in the shared memory);
- names and terms for the speech recogniser to expect (vocabulary.txt);
- a log of what Jarvis could not do or got wrong (improvements.md), for Claude
  to read when it next works on Jarvis's program.

It also watches Canvas for newly added coursework.
"""
import datetime
import difflib
import json
import re
import threading
import time
from pathlib import Path

import skills
import ui_server
from owner import USER

ROOT = Path(__file__).parent
JOURNAL = ROOT / "journal.jsonl"
STATE = ROOT / "learning-state.json"  # how far reflection has read, and which Canvas items have been seen
PROBLEMS = ROOT / "improvements.md"
VOCABULARY = ROOT / "vocabulary.txt"
SUGGESTIONS = ROOT / "suggestions.md"
MEMORY = Path.home() / "claude-memory"
LEARNED = MEMORY / "learned-by-jarvis.md"

IDLE_BEFORE_REFLECTING = 180  # seconds of quiet before Jarvis thinks over what was said
CANVAS_CHECK_SECONDS = 1800
SEED_WORDS = ["Jarvis", "Claude", "Canvas"]  # add your own names and terms under "vocabulary" in config.json
PRIVATE = re.compile(r"password|passcode|\bpin\b|social security|\bssn\b|card number|\d{6,}", re.I)

last_activity = time.time()
_lock = threading.Lock()


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(**changes) -> None:
    with _lock:
        STATE.write_text(json.dumps({**_state(), **changes}, indent=1), encoding="utf-8")


# ---- the journal -------------------------------------------------------------

def journal(you: str, jarvis: str | None, ended: str) -> None:
    """Record one exchange, and note that Jarvis has just been in use."""
    global last_activity
    last_activity = time.time()
    entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "you": you, "jarvis": jarvis or "", "ended": ended}
    with JOURNAL.open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _entries() -> list[dict]:
    try:
        return [json.loads(line) for line in JOURNAL.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


# ---- the improvement log -----------------------------------------------------

def note_problem(kind: str, request: str, detail: str = "") -> None:
    """Log something Jarvis could not do or got wrong, for Claude to read later."""
    if not PROBLEMS.exists():
        PROBLEMS.write_text(
            "# What Jarvis could not do or got wrong\n\n"
            "Jarvis adds a line here whenever it has to refuse, a command fails, or the user reports a fault.\n"
            "Claude: read this before working on Jarvis, fix what can be fixed, and remove lines once dealt with.\n\n",
            encoding="utf-8",
        )
    request, detail = " ".join(request.split())[:200], " ".join(detail.split())[:200]
    with PROBLEMS.open("a", encoding="utf-8") as out:
        out.write(f"- {datetime.datetime.now():%Y-%m-%d %H:%M} | {kind} | {request}{' | ' + detail if detail else ''}\n")


# ---- vocabulary for the speech recogniser -------------------------------------

def vocabulary() -> str:
    """Names and terms the speech recogniser should expect, as one line of text."""
    try:
        extra = [line.strip() for line in VOCABULARY.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        extra = []
    courses = list(skills._config().get("courses", {}).values())
    personal_words = [str(word) for word in skills._config().get("vocabulary", [])]
    return ", ".join(dict.fromkeys(SEED_WORDS + personal_words + courses + extra))


def learn_word(word: str) -> str:
    word = " ".join(str(word).split())[:60]
    if not word:
        return "Which word, sir?"
    if word.lower() in vocabulary().lower():
        return f"I already listen out for {word}."
    with VOCABULARY.open("a", encoding="utf-8") as out:
        out.write(word + "\n")
    return f"Learned. I will listen out for {word} from now on."


# ---- learning facts about the user -------------------------------------------------

def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower())


def _learned_lines() -> list[str]:
    try:
        return [line[2:].strip() for line in LEARNED.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
    except OSError:
        return []


def _keep_fact(fact: str) -> None:
    if not LEARNED.exists():
        LEARNED.write_text(
            "---\nname: learned-by-jarvis\ndescription: Facts about the user that Jarvis picked up from conversation on its own; "
            "each is backed by something the user said, but none has been confirmed\ntype: user\n---\n\n"
            "Jarvis's small local model wrote these while idle. Each was kept only because the user's own words, quoted after it, "
            "support it. Treat them as probably true, and check with the user before relying on one for anything that matters.\n\n",
            encoding="utf-8",
        )
        index = MEMORY / "MEMORY.md"
        if index.exists() and LEARNED.name not in index.read_text(encoding="utf-8"):
            with index.open("a", encoding="utf-8") as out:
                out.write("- [Learned by Jarvis](learned-by-jarvis.md) — facts Jarvis picked up from conversation unprompted; unconfirmed\n")
    with LEARNED.open("a", encoding="utf-8") as out:
        out.write(f"- {fact}\n")


def reflect(cancel: threading.Event) -> int:
    """Read what the user has said since the last reflection and keep any lasting facts
    about the user, provided the user's own words support them. Returns how many were kept."""
    import brain  # here, not at the top: brain imports this module

    entries = _entries()
    done = _state().get("reflected", 0)
    said = [entry["you"] for entry in entries[done:] if len(entry["you"].split()) >= 4][-30:]
    if not said:
        _save_state(reflected=len(entries))
        think_of_features(cancel)
        return 0
    prompt = (
        "These are things {USER} said to a voice assistant. Pick out only lasting facts or preferences that {USER} states "
        "about {USER}'s own life: identity, family and friends, likes and dislikes, routines, plans, work and studies. "
        "Ignore questions, requests, commands, small talk, and anything about the assistant itself. Most lines contain nothing "
        "worth keeping; an empty list is the usual answer.\n"
        'Reply with JSON only, in this form: {"facts": [{"fact": "{USER} ...", "quote": "the exact words {USER} used"}]}\n\n'
        + "\n".join(f"- {line}" for line in said)
    )
    body = {
        "model": brain.MODEL, "format": "json", "stream": True, "keep_alive": "30m", "options": brain.OPTIONS,
        "messages": [{"role": "user", "content": prompt}],
    }
    text = ""
    with brain._post("/api/chat", body) as response:
        for line in response:
            if cancel.is_set():
                return 0  # The user needs Jarvis: leave the journal position where it was and try again later
            text += json.loads(line).get("message", {}).get("content", "")
    try:
        facts = json.loads(text).get("facts", [])
    except (ValueError, AttributeError):
        facts = []
    spoken = _plain(" ".join(said))
    kept = 0
    for item in facts if isinstance(facts, list) else []:
        fact, quote = str(item.get("fact", "")).strip(), _plain(str(item.get("quote", "")))
        # The quote must really be something the user said, which is what stops the model inventing things
        if len(quote.split()) < 3 or quote not in spoken or PRIVATE.search(fact) or len(fact) > 200:
            continue
        if any(difflib.SequenceMatcher(None, _plain(fact), _plain(old)).ratio() > 0.8 for old in _learned_lines()):
            continue
        _keep_fact(f'{fact} ({USER}, {datetime.date.today().isoformat()}: "{str(item["quote"]).strip()}")')
        kept += 1
    _save_state(reflected=len(entries))
    think_of_features(cancel)
    return kept


def what_was_learned() -> str:
    facts = _learned_lines()
    try:
        problems = sum(line.startswith("- ") for line in PROBLEMS.read_text(encoding="utf-8").splitlines())
    except OSError:
        problems = 0
    lines = ["I have not picked up anything new about you on my own yet, sir."] if not facts else (
        [f"I have picked up {len(facts)} thing{'s' if len(facts) != 1 else ''} about you on my own, sir. The most recent:"]
        + [fact.split(f" ({USER}, ")[0].rstrip(".") + "." for fact in facts[-5:]]
    )
    if problems:
        lines.append(f"I have also logged {problems} thing{'s' if problems != 1 else ''} I could not do, for Claude to look at.")
    return "\n".join(lines)


def forget(words: str) -> str:
    """Remove learned facts that mention all of the given words."""
    wanted = [w for w in _plain(words).split() if len(w) > 2 and w not in ("that", "what", "you", "learned", "about", "forget", "the", "please")]
    if not wanted or not LEARNED.exists():
        return "I have nothing learned that matches, sir."
    lines = LEARNED.read_text(encoding="utf-8").splitlines()
    keep = [line for line in lines if not (line.startswith("- ") and all(w in _plain(line) for w in wanted))]
    if len(keep) == len(lines):
        return "I have nothing learned that matches, sir."
    LEARNED.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return f"Forgotten, sir. {len(lines) - len(keep)} note{'s' if len(lines) - len(keep) != 1 else ''} removed."


# ---- suggesting features --------------------------------------------------------

def _suggestions() -> list[list[str]]:
    """[status, idea, reason] for each suggestion on file. Status is new, told, wanted or declined."""
    found = []
    try:
        for line in SUGGESTIONS.read_text(encoding="utf-8").splitlines():
            match = re.match(r"- \[(new|told|wanted|declined)\] (.+?)(?: \| (.*))?$", line)
            if match:
                found.append([match.group(1), match.group(2).strip(), (match.group(3) or "").strip()])
    except OSError:
        pass
    return found


def _save_suggestions(items: list[list[str]]) -> None:
    header = (
        "# Features Jarvis thinks would be useful\n\n"
        "Jarvis adds ideas here from what the user asks for and what it could not do; Claude's weekly tune-up may add some too.\n"
        "Status: new (not yet mentioned), told (mentioned to the user), wanted (the user said yes), declined (the user said no).\n\n"
    )
    SUGGESTIONS.write_text(header + "".join(f"- [{s}] {idea}{' | ' + why if why else ''}\n" for s, idea, why in items), encoding="utf-8")


def add_suggestion(idea: str, why: str = "") -> bool:
    """File an idea unless one much like it is already there. Returns whether it was added."""
    idea, why = " ".join(idea.split()).rstrip("."), " ".join(why.split()).rstrip(".")
    items = _suggestions()
    if len(idea) < 12 or any(difflib.SequenceMatcher(None, _plain(idea), _plain(old)).ratio() > 0.7 for _, old, _ in items):
        return False
    _save_suggestions(items + [["new", idea, why]])
    return True


def mark_suggestion(idea: str, status: str) -> None:
    items = _suggestions()
    for item in items:
        if item[1] == idea:
            item[0] = status
    _save_suggestions(items)


def next_suggestion() -> tuple[str, str] | None:
    """The oldest idea not yet put to the user, as (idea, reason), marking it as told."""
    for status, idea, why in _suggestions():
        if status == "new":
            mark_suggestion(idea, "told")
            return idea, why
    return None


def waiting_suggestions() -> int:
    return sum(status == "new" for status, _, _ in _suggestions())


def think_of_features(cancel: threading.Event) -> int:
    """From what the user has been asking and what Jarvis could not do, think of abilities worth having.
    At most once a day, and only when there is something new to go on. Returns how many ideas were filed."""
    import brain  # here, not at the top: brain imports this module

    today = datetime.date.today().isoformat()
    entries = _entries()
    if _state().get("suggested_on") == today or len(entries) - _state().get("suggested_at", 0) < 8:
        return 0
    try:
        problems = [line[2:] for line in PROBLEMS.read_text(encoding="utf-8").splitlines() if line.startswith("- ")][-20:]
    except OSError:
        problems = []
    asked = [entry["you"] for entry in entries[-60:] if len(entry["you"].split()) >= 3]
    have = "; ".join(f"{name}: {about}" for name, (_, about, _) in skills.SKILLS.items())
    already = "; ".join(idea for _, idea, _ in _suggestions()) or "none"
    prompt = (
        "You are a voice assistant on {USER}'s laptop, thinking about how you could be more useful to {USER}.\n"
        f"What you can already do: {have}.\n"
        f"Ideas already suggested, do not repeat them: {already}.\n\n"
        "Things {USER} asked you recently:\n" + "\n".join(f"- {line}" for line in asked) + "\n\n"
        "Things you could not do or got wrong:\n" + ("\n".join(f"- {line}" for line in problems) or "- nothing logged") + "\n\n"
        "Suggest at most two new abilities that would clearly have helped with what is listed above. Each must be specific, "
        "something a program on a laptop could do, and tied to something {USER} actually asked. If nothing stands out, suggest nothing.\n"
        'Reply with JSON only: {"ideas": [{"idea": "what you would be able to do, in one sentence", "because": "what {USER} asked that shows the need"}]}'
    )
    body = {
        "model": brain.MODEL, "format": "json", "stream": True, "keep_alive": "30m", "options": brain.OPTIONS,
        "messages": [{"role": "user", "content": prompt}],
    }
    text = ""
    with brain._post("/api/chat", body) as response:
        for line in response:
            if cancel.is_set():
                return 0
            text += json.loads(line).get("message", {}).get("content", "")
    try:
        ideas = json.loads(text).get("ideas", [])
    except (ValueError, AttributeError):
        ideas = []
    filed = sum(
        add_suggestion(str(item.get("idea", "")), str(item.get("because", "")))
        for item in (ideas if isinstance(ideas, list) else [])[:2] if isinstance(item, dict)
    )
    _save_state(suggested_on=today, suggested_at=len(entries))
    return filed


# ---- background work -----------------------------------------------------------

def _watch_canvas() -> None:
    """Announce coursework that newly appears in Canvas. What is announced waits until
    the microphone is on, which means the headphones are in, so nothing is said to an empty room."""
    held: list[str] = []
    while True:
        try:
            events = skills._canvas_events() or []
            keys = {f"{when.isoformat()}|{title}": (when, title) for when, title in events}
            seen = _state().get("canvas_seen")
            if seen is not None:
                for key in sorted(set(keys) - set(seen)):
                    when, title = keys[key]
                    if when > datetime.datetime.now():
                        held.append(f"Sir, something new has appeared in Canvas: {skills._spoken(title)}, due {skills._day(when)} at {when:%I:%M %p}.")
            if keys:
                _save_state(canvas_seen=sorted(keys))
        except Exception as error:  # the network being down must not end the watch
            print(f"canvas watch: {error}")
        waited = 0
        while waited < CANVAS_CHECK_SECONDS:
            if held and not ui_server.muted.is_set():
                skills.finished.put({"kind": "say", "output": " ".join(held[:4])})
                held.clear()
            time.sleep(20)
            waited += 20


def start() -> None:
    threading.Thread(target=_watch_canvas, daemon=True).start()


def due_for_reflection() -> bool:
    """Whether there is anything to think over while idle: new conversation to learn from, or features to consider."""
    if time.time() - last_activity <= IDLE_BEFORE_REFLECTING:
        return False
    state, entries = _state(), len(_entries())
    new_talk = entries > state.get("reflected", 0)
    ideas_due = state.get("suggested_on") != datetime.date.today().isoformat() and entries - state.get("suggested_at", 0) >= 8
    return new_talk or ideas_due
