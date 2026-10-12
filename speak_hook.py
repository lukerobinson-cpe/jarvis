"""Claude Code Stop hook: speak the last assistant reply in the Jarvis voice.

Reads the hook's JSON payload on stdin. Delete the file `voice_off` next to this
script to enable speech, or create it to mute.
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
MAX_CHARS = 600


def last_assistant_text(payload: dict) -> str:
    text = payload.get("last_assistant_message")
    if text:
        return text
    transcript = payload.get("transcript_path")
    if not transcript or not Path(transcript).exists():
        return ""
    for line in reversed(Path(transcript).read_text(encoding="utf-8").splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        if isinstance(content, str):
            return content
        parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if any(parts):
            return "\n".join(parts)
    return ""


def to_speech(text: str) -> str:
    """Strip markdown that reads badly aloud and cap the length at a sentence end."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.M)  # tables
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s*(#+|[-*•]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"[*_`#>]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_CHARS:
        return text
    cut = text[:MAX_CHARS]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: end + 1] if end > 0 else cut


def main() -> None:
    # listen.py speaks its own replies so it knows when to reopen the microphone
    if (ROOT / "voice_off").exists() or os.environ.get("JARVIS_LISTENER"):
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    text = to_speech(last_assistant_text(payload))
    if not text:
        return
    from speak import speak

    speak(text)


if __name__ == "__main__":
    main()
