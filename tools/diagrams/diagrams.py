"""The animated diagrams in the README and docs/ (GIF), and in the tutorial videos (MP4).

    pip install websocket-client pillow
    python3 tools/diagrams/diagrams.py                 # every diagram
    python3 tools/diagrams/diagrams.py talk team       # only some

Each diagram is a spec in specs.py: cards and groups placed on a 1280x720 canvas (or the people
and services of a sequence), and the steps that play on it. engine.js draws any moment of it in
Chrome; this script steps through the timeline frame by frame and writes
docs/images/diagram-<name>.gif, and media/clips/diagram-<name>.mp4 for the ones the videos use
(MP4 needs macOS: tools/tutorial/clip.swift).
"""
import base64
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import crewchat  # noqa: E402
import docs_screenshots as shots  # noqa: E402
from specs import SPECS  # noqa: E402

from PIL import Image  # noqa: E402

GIFS = ROOT / "docs" / "images"
CLIPS = ROOT / "media" / "clips"
GIF_FPS, GIF_WIDTH = 10, 880
VIDEO_FPS = 30

PAGE = """<!doctype html><meta charset="utf-8">
<style>html,body{margin:0;background:#fff;font-family:-apple-system,"SF Pro Text","Helvetica Neue",Arial,sans-serif}
svg{display:block}</style>
<svg id="d" width="1280" height="720" viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg"></svg>
<script>window.LOGO = %s;</script>
<script>%s</script>
<script>window.TOTAL = setup(%s); render(0);</script>"""


def page(name, spec):
    logo = "data:image/svg+xml;base64," + base64.b64encode(crewchat.LOGO_FILE).decode()
    path = shots.TMP / ("diagram-%s.html" % name)
    path.write_text(PAGE % (json.dumps(logo), (HERE / "engine.js").read_text(), json.dumps(spec)), encoding="utf-8")
    return path.as_uri()


def capture(browser, name, spec, fps, scale, gif=False):
    """[(image, seconds)] for the whole timeline, with runs of identical frames merged."""
    browser.size(1280, 720, scale=scale)
    browser.open(page(name, spec), wait=0.8)
    if gif:
        browser.js("window.STILL_LINES = true")
    total = browser.js("window.TOTAL")
    frames, last = [], None
    for i in range(int(total * fps) + 1):
        browser.js("render(%f)" % (i / fps))
        data = browser.call("Page.captureScreenshot", format="png")["data"]
        if data == last:
            frames[-1][1] += 1 / fps
            continue
        last = data
        frames.append([Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB"), 1 / fps])
    return frames


def write_gif(name, frames):
    small = [(img.resize((GIF_WIDTH, round(img.height * GIF_WIDTH / img.width)), Image.LANCZOS), d) for img, d in frames]
    # One palette for every frame, from moments across the whole animation, so every step's
    # colour is in it and nothing flickers.
    picks = [small[round(i * (len(small) - 1) / 11)][0] for i in range(12)]
    h = picks[0].height
    sample = Image.new("RGB", (GIF_WIDTH, h * len(picks)))
    for j, img in enumerate(picks):
        sample.paste(img, (0, j * h))
    adaptive = sample.quantize(colors=72, method=Image.MEDIANCUT, dither=Image.NONE).getpalette()[:72 * 3]
    # Plus the diagrams' own colours exactly, so a colour seen only briefly is not swapped.
    engine = (HERE / "engine.js").read_text()
    exact = re.findall(r"#([0-9a-f]{6})", engine[engine.index("const PALETTE"):engine.index("const STEP_COLORS")])
    colours = adaptive + [int(h[i:i + 2], 16) for h in exact for i in (0, 2, 4)]
    palette = Image.new("P", (1, 1))
    palette.putpalette(colours + [0] * (768 - len(colours)))
    images = [img.quantize(palette=palette, dither=Image.NONE) for img, _ in small]
    path = GIFS / ("diagram-%s.gif" % name)
    images[0].save(path, save_all=True, append_images=images[1:], loop=0, optimize=False, disposal=1,
                   duration=[max(20, round(d * 1000)) for _, d in small])
    print("  docs/images/%s  %.1fs  %d frames  %d KB" % (path.name, sum(d for _, d in small), len(images),
                                                        path.stat().st_size // 1024))


def write_mp4(name, frames):
    CLIPS.mkdir(parents=True, exist_ok=True)
    work = CLIPS / ("diagram-%s-frames" % name)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    listing = []
    for i, (img, d) in enumerate(frames):
        p = work / ("%05d.png" % i)
        img.save(p)
        listing.append({"image": str(p), "duration": d})
    out = CLIPS / ("diagram-%s.mp4" % name)
    (work / "frames.json").write_text(json.dumps({"frames": listing, "output": str(out)}))
    binary = ROOT / "media" / "work" / "clip"
    if not binary.exists():
        binary.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", str(ROOT / "tools" / "tutorial" / "clip.swift"), "-o", str(binary)], check=True)
    subprocess.run([str(binary), str(work / "frames.json")], check=True, stdout=subprocess.DEVNULL)
    shutil.rmtree(work, ignore_errors=True)
    print("  media/clips/%s  %.1fs" % (out.name, sum(d for _, d in frames)))


def main(names):
    unknown = [n for n in names if n not in SPECS]
    if unknown:
        sys.exit("no such diagram: %s (have: %s)" % (", ".join(unknown), ", ".join(SPECS)))
    browser = shots.Browser()
    try:
        for name in names or SPECS:
            spec = SPECS[name]
            print(name)
            if spec.get("gif", True):
                write_gif(name, capture(browser, name, spec, GIF_FPS, 1, gif=True))
            if spec.get("video"):
                write_mp4(name, capture(browser, name, spec, VIDEO_FPS, 1.5))
    finally:
        browser.close()
        shutil.rmtree(shots.TMP, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
