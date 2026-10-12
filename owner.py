"""Who Jarvis works for. The program itself names no one: the user's name and any
personal touches live in config.json, which is never committed.

Text sent to the language model or to Claude may contain the placeholder {USER};
`personal` fills it in just before the text leaves Jarvis.
"""
import json
from pathlib import Path

CONFIG = Path(__file__).parent / "config.json"


def _config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


USER = str(_config().get("user_name", "")).strip() or "the user"


def personal(text: str) -> str:
    """Fill in the user's name wherever the placeholder appears."""
    return text.replace("{USER}", USER)


def personal_json(text: str) -> str:
    """As `personal`, for text that is already JSON, so the name is escaped properly."""
    return text.replace("{USER}", json.dumps(USER)[1:-1])


def persona_notes() -> str:
    """Extra lines for Jarvis's character, from "persona_notes" in config.json."""
    notes = _config().get("persona_notes", [])
    return "\n".join(f"- {note}" for note in notes if str(note).strip())


def enabled(feature: str) -> bool:
    """Optional features, switched on in config.json under "features"."""
    return bool(_config().get("features", {}).get(feature, False))
