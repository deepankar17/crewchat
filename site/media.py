"""Make the website's media from the docs' images: each demo GIF as a small looping MP4 with a
poster frame, and the screenshots as JPEG. Needs ffmpeg; run it when docs/images change, then
`python3 site/build.py`. The results are committed, so building and deploying need no ffmpeg."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"
OUT = ROOT / "site" / "assets" / "media"

# The GIFs the site shows (docs/images/<name>.gif).
CLIPS = ["demo-start", "demo-talk", "demo-task", "demo-roles", "demo-files", "demo-phone", "diagram-architecture"]
# Screenshots, and the width each is scaled down to.
STILLS = {"screenshot-light": 1600, "screenshot-dark": 1600, "task-board": 1400}


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def probe(path, entries):
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", entries, "-of", "csv=p=0",
                           str(path)], capture_output=True, text=True, check=True).stdout.strip()


def size(path):
    return [int(n) for n in probe(path, "stream=width,height").split(",")[:2]]


def main():
    if not shutil.which("ffmpeg"):
        sys.exit("media.py needs ffmpeg (brew install ffmpeg)")
    shutil.rmtree(OUT, ignore_errors=True)  # only what the lists above name
    OUT.mkdir(parents=True)
    for name in CLIPS:
        mp4 = OUT / (name + ".mp4")
        # H.264 needs even sides; yuv420p and faststart so every browser plays it at once.
        ffmpeg("-i", str(IMAGES / (name + ".gif")), "-movflags", "+faststart", "-pix_fmt", "yuv420p", "-an",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-crf", "26", "-preset", "slow", str(mp4))
        # The poster: a frame from the middle, since many demos open on an empty screen.
        middle = float(probe(mp4, "format=duration")) / 2
        ffmpeg("-ss", "%.2f" % middle, "-i", str(mp4), "-frames:v", "1", "-q:v", "4", str(OUT / (name + ".jpg")))
    for name, width in STILLS.items():
        ffmpeg("-i", str(IMAGES / (name + ".png")), "-vf", "scale=%d:-2" % width, "-q:v", "3",
               str(OUT / (name + ".jpg")))
    # Each file's width and height, so build.py sets them and pages do not jump while loading.
    sizes = {p.name: size(p) for p in sorted(OUT.iterdir()) if p.suffix in (".mp4", ".jpg")}
    (ROOT / "site" / "media.json").write_text(json.dumps(sizes, indent=1) + "\n")
    total = sum(p.stat().st_size for p in OUT.iterdir())
    print("Wrote %d files in %s (%.1f MB)." % (len(list(OUT.iterdir())), OUT.relative_to(ROOT), total / 1e6))


if __name__ == "__main__":
    main()
