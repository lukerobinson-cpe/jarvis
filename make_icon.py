"""Draw the Jarvis icon (a red arc reactor) and save it as ui/jarvis.ico and ui/jarvis.png."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024  # drawn large, then scaled down so the edges are smooth
RED, HOT, DARK = (255, 45, 45), (255, 150, 130), (14, 3, 5)
out = Path(__file__).parent / "ui"


def ring(draw, radius, width, colour, start=0, end=360):
    box = [SIZE / 2 - radius, SIZE / 2 - radius, SIZE / 2 + radius, SIZE / 2 + radius]
    draw.arc(box, start, end, fill=colour, width=width)


base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(base)
draw.ellipse([24, 24, SIZE - 24, SIZE - 24], fill=DARK + (255,))

glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
g = ImageDraw.Draw(glow)
ring(g, 452, 18, RED + (255,))
for start in (20, 140, 260):  # three thick arcs, like the HUD's
    ring(g, 378, 40, RED + (255,), start, start + 80)
for i in range(24):  # dashed band
    ring(g, 292, 20, RED + (170,), i * 15, i * 15 + 8)
ring(g, 222, 10, HOT + (255,))
g.ellipse([SIZE / 2 - 132, SIZE / 2 - 132, SIZE / 2 + 132, SIZE / 2 + 132], fill=RED + (255,))
g.ellipse([SIZE / 2 - 58, SIZE / 2 - 58, SIZE / 2 + 58, SIZE / 2 + 58], fill=(255, 215, 205, 255))

soft = glow.filter(ImageFilter.GaussianBlur(22))
soft.putalpha(soft.getchannel("A").point(lambda a: a * 0.45))
base.alpha_composite(soft)  # soft glow underneath
base.alpha_composite(glow)
mask = Image.new("L", (SIZE, SIZE), 0)
ImageDraw.Draw(mask).ellipse([24, 24, SIZE - 24, SIZE - 24], fill=255)
base.putalpha(mask)

icon = base.resize((256, 256), Image.LANCZOS)
icon.save(out / "jarvis.png")
icon.save(out / "jarvis.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
print("saved", out / "jarvis.ico")
