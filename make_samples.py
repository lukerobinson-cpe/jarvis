"""Generate the same line in each British male Kokoro voice for comparison."""
import time
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro

ROOT = Path(__file__).parent
VOICES = ["bm_george", "bm_fable", "bm_lewis", "bm_daniel"]
LINE = (
    "Good evening, sir. All systems are online. "
    "You have three meetings tomorrow, and I've taken the liberty of "
    "rescheduling the one you were dreading."
)

kokoro = Kokoro(str(ROOT / "models" / "kokoro-v1.0.onnx"), str(ROOT / "models" / "voices-v1.0.bin"))
out = ROOT / "samples"
out.mkdir(exist_ok=True)

for voice in VOICES:
    start = time.time()
    audio, rate = kokoro.create(LINE, voice=voice, speed=1.0, lang="en-gb")
    sf.write(out / f"{voice}.wav", audio, rate)
    print(f"{voice}: {len(audio) / rate:.1f}s audio in {time.time() - start:.1f}s")
