"""Jarvis's own adjustable settings, which it may change when the user asks.

They live in settings.json and take effect straight away. Everything here is
bounded, so a wrong value cannot stop Jarvis working.
"""
import json
from pathlib import Path

FILE = Path(__file__).parent / "settings.json"
INSTRUCTIONS = Path(__file__).parent / "instructions.txt"  # standing instructions the user has given, one per line

DEFAULTS = {
    "speech_speed": 1.0,
    "voice": "daniel",
    "silence_seconds": 0.8,
    "follow_up_seconds": 6.0,
    "wake_threshold": 0.5,
    "reply_length": "short",
    "audio_only": "off",
}
STEPS = {"speech_speed": 0.1, "silence_seconds": 0.15, "follow_up_seconds": 2.0, "wake_threshold": 0.05}  # for "higher" and "lower"
RANGES = {
    "speech_speed": (0.7, 1.5),
    "silence_seconds": (0.4, 2.5),
    "follow_up_seconds": (0.0, 20.0),
    "wake_threshold": (0.2, 0.9),
}
CHOICES = {
    "voice": ["daniel", "george", "lewis", "fable"],
    "reply_length": ["short", "normal", "detailed"],
    "audio_only": ["on", "off"],
}
MEANINGS = {
    "speech_speed": "how fast Jarvis talks; 1.0 is normal, higher is faster",
    "voice": "which British voice Jarvis uses",
    "silence_seconds": "how long Jarvis waits after {USER} stops talking before answering; lower answers sooner but may cut {USER} off",
    "follow_up_seconds": "how long Jarvis keeps listening after a reply without the wake word",
    "wake_threshold": "how sure Jarvis must be that it heard 'Hey Jarvis'; lower triggers more easily",
    "reply_length": "how long Jarvis's answers are",
    "audio_only": "when on, Jarvis plays a tone when it starts listening and another when it starts processing, for use without looking at the screen",
}

_cache: tuple[float, dict] = (-1.0, {})


def _all() -> dict:
    global _cache
    try:
        stamp = FILE.stat().st_mtime
        if stamp != _cache[0]:
            _cache = (stamp, json.loads(FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        _cache = (-1.0, {})
    return {**DEFAULTS, **{k: v for k, v in _cache[1].items() if k in DEFAULTS}}


def get(name: str):
    return _all()[name]


def describe() -> str:
    """Every setting with its current value and allowed values, for the model's prompt."""
    current = _all()
    lines = []
    for name, meaning in MEANINGS.items():
        allowed = f"{RANGES[name][0]} to {RANGES[name][1]}" if name in RANGES else ", ".join(CHOICES[name])
        lines.append(f"- {name} = {current[name]} ({meaning}; allowed: {allowed})")
    return "\n".join(lines)


def change(name: str, value: str) -> str:
    name = name.strip().lower().replace(" ", "_")
    if name not in DEFAULTS:
        return f"There is no setting called {name}. The settings are: {', '.join(DEFAULTS)}."
    if name in RANGES:
        low, high = RANGES[name]
        word = str(value).strip().lower()
        try:
            if word in ("higher", "faster", "more", "up", "longer"):
                new = get(name) + STEPS[name]
            elif word in ("lower", "slower", "less", "down", "shorter"):
                new = get(name) - STEPS[name]
            else:
                new = float(word)
        except ValueError:
            return f"{name} needs 'higher', 'lower', or a number from {low} to {high}."
        new = round(min(max(new, low), high), 2)
    else:
        new = str(value).strip().lower().removeprefix("bm_")
        if new not in CHOICES[name]:
            return f"{name} must be one of: {', '.join(CHOICES[name])}."
    saved = {k: v for k, v in _all().items() if v != DEFAULTS[k]}
    saved[name] = new
    FILE.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    return f"{name} is now {new}. It takes effect immediately."


def instructions() -> str:
    try:
        return INSTRUCTIONS.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def add_instruction(text: str) -> str:
    text = " ".join(str(text).split())
    if not text:
        return "Nothing to add."
    with INSTRUCTIONS.open("a", encoding="utf-8") as out:
        out.write(f"- {text}\n")
    return "Saved. It applies from the next reply onwards."
