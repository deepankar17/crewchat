"""Turn screenshots of the Firebase and Google Cloud consoles into clips that play like a recording:
a pointer moves to each button and clicks, typed text appears letter by letter, and private
values (account name and email, API key, client ID and secret) are blurred.

    pip install pillow
    python3 tools/tutorial/console_clips.py          # macOS only; writes media/clips/fb-*.mp4

The screenshots are in media/console (not in git, since they come from a real account), with
steps.json: one entry per screenshot, in order, with
  region  the part of the browser window the screenshot shows (x0, y0, x1, y1, window pixels),
  click   where the pointer clicks to get to the next screenshot (window pixels),
  type    for a click into a text field: the field's box; the next screenshot's text in that box
          is revealed from left to right, as if typed,
  paste   the next screenshot fades in, as if pasted,
  blur    boxes to blur (window pixels).
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "media" / "console"
CLIPS = ROOT / "media" / "clips"
W, H, FPS = 1456, 819, 30

SCENES = {
    "fb-project": ["home", "name", "named", "gemini", "gemini-off", "analytics", "analytics-off", "creating", "ready"],
    "fb-database": ["menu-firestore", "firestore", "db-edition", "db-location", "db-mode", "db-create"],
    "fb-rules": ["db-ready", "rules-old", "rules-new", "rules-published"],
    "fb-auth": ["menu-auth", "auth", "providers", "google-off", "google-on", "public-name", "email-menu", "email-set",
                "google-enabled"],
    "fb-webapp": ["settings-menu", "project-settings", "webapp", "webapp-named", "webconfig", "webconfig-done"],
    "fb-oauth": ["credentials", "credentials-menu", "client-type", "client-type-menu", "client-desktop", "client-named",
                 "client-created", "audience"],
}

# The pointer: the same arrow as in the chat recordings, white so it shows on the dark consoles.
ARROW = [(4, 2), (19, 11.5), (12.4, 12.9), (16.4, 20.5), (13.4, 22.1), (9.4, 14.4), (4, 19)]


def to_frame(step, point):
    x0, y0, x1, y1 = step["region"]
    return ((point[0] - x0) * W / (x1 - x0), (point[1] - y0) * H / (y1 - y0))


def picture(step):
    img = Image.open(SHOTS / step["img"]).convert("RGB").resize((W, H), Image.LANCZOS)
    for box in step.get("blur", []):
        a, b = to_frame(step, box[:2]), to_frame(step, box[2:])
        box = tuple(int(v) for v in (max(0, a[0]), max(0, a[1]), min(W, b[0]), min(H, b[1])))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        part = img.crop(box)
        small = part.resize((max(1, part.width // 14), max(1, part.height // 14)), Image.BILINEAR)
        img.paste(small.resize(part.size, Image.NEAREST).filter(ImageFilter.GaussianBlur(5)), box[:2])
    return img


def pointer(img, at, ripple=None):
    img = img.copy()
    draw = ImageDraw.Draw(img)
    if ripple is not None:  # 0..1: a ring that grows and fades where the click lands
        r = 10 + 26 * ripple
        alpha = int(230 * (1 - ripple))
        ring = Image.new("RGBA", img.size)
        ImageDraw.Draw(ring).ellipse((at[0] - r, at[1] - r, at[0] + r, at[1] + r), outline=(138, 180, 248, alpha), width=4)
        img = Image.alpha_composite(img.convert("RGBA"), ring).convert("RGB")
        draw = ImageDraw.Draw(img)
    scale = 1.45
    points = [(at[0] + (x - 4) * scale, at[1] + (y - 2) * scale) for x, y in ARROW]
    shadow = [(x + 2, y + 3) for x, y in points]
    draw.polygon(shadow, fill=(0, 0, 0))
    draw.polygon(points, fill=(255, 255, 255), outline=(20, 20, 20), width=2)
    return img


def ease(t):
    return t * t * (3 - 2 * t)


def render(name, ids):
    steps = {s["id"]: s for s in json.loads((SHOTS / "steps.json").read_text())}
    seq = [steps[i] for i in ids]
    pics = [picture(s) for s in seq]
    frames = []  # (image, seconds)

    def hold(img, seconds):
        frames.append((img, seconds))

    at = (W * 0.62, H * 0.72)
    for n, (step, img) in enumerate(zip(seq, pics)):
        nxt = pics[n + 1] if n + 1 < len(pics) else None
        if nxt is None or not step.get("click"):
            hold(pointer(img, at), 2.4 if nxt is None else 1.8)
            if nxt is not None:
                for k in range(1, 7):
                    frames.append((pointer(Image.blend(img, nxt, k / 6), at), 1 / FPS))
            continue
        hold(pointer(img, at), 0.45)
        target = to_frame(step, step["click"])
        moves = int(0.75 * FPS)
        for k in range(1, moves + 1):
            t = ease(k / moves)
            frames.append((pointer(img, (at[0] + (target[0] - at[0]) * t, at[1] + (target[1] - at[1]) * t)), 1 / FPS))
        at = target
        for k in range(1, 9):
            frames.append((pointer(img, at, k / 9), 1 / FPS))
        if step.get("type"):
            a, b = to_frame(step, step["type"][:2]), to_frame(step, step["type"][2:])
            box = (int(a[0]), int(a[1]), int(b[0]), int(b[1]))
            steps_n = int(0.9 * FPS)
            for k in range(1, steps_n + 1):
                cut = int(box[0] + (box[2] - box[0]) * k / steps_n)
                part = img.copy()
                part.paste(nxt.crop((box[0], box[1], cut, box[3])), box[:2])
                frames.append((pointer(part, at), 1 / FPS))
            hold(pointer(nxt, at), 0.5)
        elif step.get("paste"):
            for k in range(1, 10):
                frames.append((pointer(Image.blend(img, nxt, k / 9), at), 1 / FPS))
            hold(pointer(nxt, at), 0.4)
        else:
            hold(pointer(img, at), 0.25)
            for k in range(1, 6):
                frames.append((pointer(Image.blend(img, nxt, k / 5), at), 1 / FPS))
    write(name, frames)


def write(name, frames):
    work = CLIPS / (name + "-frames")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    listing = []
    for i, (img, seconds) in enumerate(frames):
        p = work / ("%05d.png" % i)
        img.save(p)
        listing.append({"image": str(p), "duration": seconds})
    (work / "frames.json").write_text(json.dumps({"frames": listing, "output": str(CLIPS / (name + ".mp4"))}))
    binary = ROOT / "media" / "work" / "clip"
    if not binary.exists():
        binary.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", str(ROOT / "tools" / "tutorial" / "clip.swift"), "-o", str(binary)], check=True)
    subprocess.run([str(binary), str(work / "frames.json")], check=True)
    shutil.rmtree(work, ignore_errors=True)
    print("  media/clips/%s.mp4  %.1fs" % (name, sum(s for _, s in frames)))


def main(names):
    for name in names or SCENES:
        render(name, SCENES[name])


if __name__ == "__main__":
    main(sys.argv[1:])
