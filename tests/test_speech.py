"""What reaches the voice: markdown stripped, long text cut at a sentence end."""
import speak_hook


def test_markdown_is_stripped():
    text = "## Result\n- **Done**: see [the docs](https://example.com).\n```\ncode\n```\nAll good."
    assert speak_hook.to_speech(text) == "Result Done: see the docs. All good."


def test_long_text_ends_on_a_whole_sentence():
    text = "This is a sentence. " * 60
    spoken = speak_hook.to_speech(text)
    assert len(spoken) <= speak_hook.MAX_CHARS and spoken.endswith(".")
