"""Jarvis may change its own settings by voice, but only within safe bounds."""
import pytest

import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "INSTRUCTIONS", tmp_path / "instructions.txt")
    monkeypatch.setattr(settings, "_cache", (-1.0, {}))


def test_defaults_apply_when_nothing_is_saved():
    assert settings.get("voice") == "daniel"
    assert settings.get("speech_speed") == 1.0


def test_nudging_moves_one_step():
    settings.change("speech_speed", "faster")
    assert settings.get("speech_speed") == pytest.approx(1.1)
    settings.change("speech speed", "lower")
    assert settings.get("speech_speed") == pytest.approx(1.0)


def test_values_are_clamped_to_their_range():
    settings.change("wake_threshold", "5")
    assert settings.get("wake_threshold") == 0.9
    settings.change("wake_threshold", "-1")
    assert settings.get("wake_threshold") == 0.2


def test_choices_are_checked():
    assert "must be one of" in settings.change("voice", "robot")
    settings.change("voice", "bm_george")
    assert settings.get("voice") == "george"


def test_unknown_settings_and_bad_numbers_are_explained():
    assert "no setting called" in settings.change("volume", "11")
    assert "needs 'higher', 'lower'" in settings.change("silence_seconds", "soon")


def test_only_changed_values_are_saved():
    settings.change("reply_length", "detailed")
    saved = settings.FILE.read_text()
    assert "detailed" in saved and "voice" not in saved


def test_standing_instructions_are_kept_one_per_line():
    assert settings.instructions() == ""
    settings.add_instruction("  Call me   Luke.  ")
    settings.add_instruction("Keep answers short.")
    assert settings.instructions().splitlines() == ["- Call me Luke.", "- Keep answers short."]
