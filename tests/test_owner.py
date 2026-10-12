"""The user's name is filled in only as text leaves Jarvis, and safely inside JSON."""
import json

import owner


def test_placeholder_is_filled(monkeypatch):
    monkeypatch.setattr(owner, "USER", "Sam")
    assert owner.personal("Tell {USER} it is done.") == "Tell Sam it is done."


def test_json_stays_valid_whatever_the_name(monkeypatch):
    monkeypatch.setattr(owner, "USER", 'Jo "JJ" O\\Brien')
    body = json.dumps({"content": "Address {USER} as sir."})
    assert json.loads(owner.personal_json(body))["content"] == 'Address Jo "JJ" O\\Brien as sir.'
