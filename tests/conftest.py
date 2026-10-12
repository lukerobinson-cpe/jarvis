import sys
from pathlib import Path

# The modules live in the folder above, side by side, as Jarvis runs them.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
