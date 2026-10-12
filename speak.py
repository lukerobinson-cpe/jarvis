"""Speak text aloud in the Jarvis voice: python speak.py "Hello, sir." [voice]"""
import sys
from functools import lru_cache
from pathlib import Path

import onnxruntime
import sounddevice as sd
from kokoro_onnx import Kokoro

ROOT = Path(__file__).parent
DEFAULT_VOICE = "bm_daniel"
VOICE_THREADS = 6  # leaves the rest of the processor for the language model, which runs at the same time


@lru_cache(maxsize=1)
def _kokoro() -> Kokoro:
    options = onnxruntime.SessionOptions()
    # Without this the voice model's memory grows with every new sentence length
    # it meets, into gigabytes over a long session.
    options.enable_cpu_mem_arena = False
    options.intra_op_num_threads = VOICE_THREADS
    session = onnxruntime.InferenceSession(
        str(ROOT / "models" / "kokoro-v1.0.onnx"), options, providers=["CPUExecutionProvider"]
    )
    return Kokoro.from_session(session, str(ROOT / "models" / "voices-v1.0.bin"))


def speak(text: str, voice: str = DEFAULT_VOICE) -> None:
    audio, rate = _kokoro().create(text, voice=voice, speed=1.0, lang="en-gb")
    sd.play(audio, rate)
    sd.wait()


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    speak(text, sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VOICE)
