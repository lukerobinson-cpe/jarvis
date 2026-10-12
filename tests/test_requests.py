"""Requests Jarvis handles itself rather than leaving to the model."""
import pytest

import brain


@pytest.mark.parametrize("said, wanted", [
    ("turn off the microphone", True),
    ("switch the mic off", True),
    ("mute your microphone", True),
    ("turn on my microphone", True),
    ("unmute the mic", True),
    ("what microphone should I buy", False),
])
def test_microphone_requests(said, wanted):
    assert bool(brain.MICROPHONE.search(said)) is wanted


def test_closing_jarvis_is_not_shutting_down_the_laptop():
    assert brain.SHUT_DOWN.search("shut yourself down") and not brain.THE_LAPTOP.search("shut yourself down")
    assert brain.THE_LAPTOP.search("shut down the laptop")


@pytest.mark.parametrize("said, yes", [("yes please", True), ("go ahead", True), ("okay", True), ("no", False), ("wait", False)])
def test_confirmation(said, yes):
    assert bool(brain.YES.search(said) and not brain.NO.search(said)) is yes


def test_sentences_split_for_speaking_as_they_arrive():
    text = 'First one. Second "quoted." Third?'
    ends = [0] + [match.end() for match in brain.SENTENCE_END.finditer(text)] + [len(text)]
    assert [text[a:b].strip() for a, b in zip(ends, ends[1:])] == ["First one.", 'Second "quoted."', "Third?"]
