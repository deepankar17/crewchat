"""Make the short videos of a release series: each episode as a vertical (9:16) and a landscape (16:9) MP4,
narrated and captioned, with a GIF and a thumbnail, from tools/shorts/episodes.json.

    python3 tools/shorts/make_shorts.py                 # every episode
    python3 tools/shorts/make_shorts.py 03-analytics    # only some
    python3 tools/shorts/make_shorts.py --voice am_michael

Needs ffmpeg, Google Chrome, and for the voice Kokoro (kokoro-onnx and soundfile from PyPI, and its model files
kokoro-v1.0.onnx and voices-v1.0.bin in media/voice/, from github.com/thewh1teagle/kokoro-onnx releases):

    uv run --with kokoro-onnx --with soundfile --with websocket-client python3 tools/shorts/make_shorts.py

Each scene is a frame drawn in Chrome (the backdrop, the episode's title, a card for the picture, the scene's
title) with its picture or clip laid into the card by ffmpeg, its narration (Kokoro, a sentence at a time) and
the narration's sentences as captions, each shown while it is said. An episode opens on its title and ends on
crewchat.dev. Everything is written to media/shorts/<episode>/ (not in git): vertical.mp4, landscape.mp4,
captions.srt, episode.gif and thumbnail.png; the pictures and clips come from docs/images and media/clips
(tools/docs_screenshots.py, tools/demo_recordings.py).
"""
import argparse
import base64
import html
import json
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import docs_screenshots as shots  # noqa: E402  (its Chrome)

OUT = ROOT / "media" / "shorts"
VOICE = ROOT / "media" / "voice"
FPS = 30
GAP = 0.28  # seconds between two sentences
TAIL = 0.7  # after the last one, before the next scene
FADE = 0.25
# Where things go, per format: the card the picture sits in (x, y, w, h), and the captions' band (x, y, w, h).
FORMATS = {
    "vertical": {"size": (1080, 1920), "card": (50, 470, 980, 900), "caption": (60, 1400, 960, 300)},
    "landscape": {"size": (1920, 1080), "card": (330, 178, 1260, 708), "caption": (240, 900, 1440, 170)},
}
LOGO = (ROOT / "docs" / "images" / "logo.svg").read_text(encoding="utf-8")

PAGE = """<!doctype html><meta charset="utf-8"><style>
html, body { margin: 0; width: %(w)dpx; height: %(h)dpx; overflow: hidden; }
body { font-family: ui-rounded, "SF Pro Rounded", system-ui, -apple-system, sans-serif; color: #0b1f55;
  background: radial-gradient(60%% 55%% at 0%% 0%%, #c7d7ff 0%%, transparent 62%%),
    radial-gradient(55%% 50%% at 100%% 0%%, #e7d6ff 0%%, transparent 60%%),
    radial-gradient(60%% 55%% at 85%% 100%%, #ffe1cc 0%%, transparent 62%%),
    radial-gradient(50%% 45%% at 10%% 100%%, #d4f1ea 0%%, transparent 60%%), #eef3ff; position: relative; }
.transparent { background: transparent !important; }
.brand { position: absolute; display: flex; align-items: center; gap: 14px; font-weight: 650; letter-spacing: -0.02em; }
.brand svg { width: 1.3em; height: 1.3em; } .brand b { color: #2f5fd6; font-weight: 800; }
.chip { position: absolute; padding: 10px 22px; border-radius: 999px; background: rgba(255,255,255,.7);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.9), 0 6px 20px rgba(11,31,85,.10); font-weight: 650; color: #2148ad; }
h1 { position: absolute; margin: 0; font-weight: 800; letter-spacing: -0.03em; line-height: 1.05; }
h2 { position: absolute; margin: 0; font-weight: 750; letter-spacing: -0.02em; line-height: 1.1; color: #2148ad; }
p.tag { position: absolute; margin: 0; color: #4b587d; font-weight: 600; line-height: 1.25; }
.card { position: absolute; border-radius: 34px; background: #ffffff;
  box-shadow: inset 0 0 0 1px rgba(11,31,85,.06), 0 30px 70px -20px rgba(11,31,85,.35), 0 8px 24px rgba(11,31,85,.10); }
.stat { position: absolute; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.stat .n { font-size: 260px; font-weight: 850; letter-spacing: -0.05em; color: #2f5fd6; line-height: 1; }
.stat .t { font-size: 46px; font-weight: 650; color: #4b587d; max-width: 80%%; line-height: 1.25; margin-top: 30px; }
.cap { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; }
.cap span { display: inline-block; text-align: center; font-weight: 750; line-height: 1.22; color: #0b1f55;
  background: rgba(255,255,255,.88); border-radius: 28px; padding: 18px 30px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.95), 0 10px 30px rgba(11,31,85,.14); }
</style><body class="%(cls)s">%(body)s</body>"""


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+", text) if s.strip()]


# What the voice says in words, written as people read it in the captions.
WRITTEN = [("crewchat dot dev slash demo", "crewchat.dev/demo"), ("crewchat dot dev", "crewchat.dev"),
           ("zero point fifteen", "0.15"), ("macOS twenty-six", "macOS 26")]


def written(text):
    for said, shown in WRITTEN:
        text = text.replace(said, shown)
    return text


def esc(text):
    return html.escape(text, quote=True)


# ---------------------------------------------------------------------------------------------------------------
# Drawing frames in Chrome
# ---------------------------------------------------------------------------------------------------------------
class Painter:
    def __init__(self, work):
        self.b = shots.Browser()
        self.work = work
        self.n = 0

    def close(self):
        self.b.close()

    def png(self, fmt, body, transparent=False, name=None):
        """A frame of this format, as a PNG file (transparent outside what is drawn, for captions)."""
        w, h = FORMATS[fmt]["size"]
        self.n += 1
        page = self.work / ("page-%d.html" % self.n)
        page.write_text(PAGE % {"w": w, "h": h, "cls": "transparent" if transparent else "", "body": body},
                        encoding="utf-8")
        self.b.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
        self.b.call("Emulation.setDefaultBackgroundColorOverride",
                    color={"r": 0, "g": 0, "b": 0, "a": 0} if transparent else {"r": 238, "g": 243, "b": 255, "a": 1})
        self.b.open(page.as_uri(), wait=0.25)
        shot = self.b.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        out = self.work / (name or ("frame-%d.png" % self.n))
        out.write_bytes(base64.b64decode(shot["data"]))
        return out


def brand_and_chip(fmt, series, number, total):
    w, h = FORMATS[fmt]["size"]
    if fmt == "vertical":
        return ('<div class="brand" style="left:60px;top:70px;font-size:44px">%s<span>crew<b>chat</b></span></div>'
                '<div class="chip" style="right:60px;top:74px;font-size:28px">%s · %d/%d</div>' % (LOGO, esc(series), number, total))
    return ('<div class="brand" style="left:60px;top:44px;font-size:36px">%s<span>crew<b>chat</b></span></div>'
            '<div class="chip" style="right:60px;top:44px;font-size:24px">%s · %d/%d</div>' % (LOGO, esc(series), number, total))


PAD = 22  # the picture inside its card, with a white margin


def media_size(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip().split(",")
    return int(out[0]), int(out[1])


def crop_of(fmt, scene):
    """The part of the picture a scene shows in this format: (x0, y0, x1, y1) as fractions. A whole page is too
    small to read on a phone held upright, so the vertical cut zooms in on what matters ("crop")."""
    return tuple(scene.get("crop") or (0, 0, 1, 1)) if fmt == "vertical" else (0, 0, 1, 1)


def layout(fmt, media, crop=(0, 0, 1, 1)):
    """Where this scene's card goes, shaped to its picture, and the captions' band right under it (vertical) or
    below (landscape): (card, caption band)."""
    cx, cy, cw, ch = FORMATS[fmt]["card"]
    band = FORMATS[fmt]["caption"]
    if media is None:
        return (cx, cy, cw, ch), band
    w, h = media_size(ROOT / media)
    w, h = w * (crop[2] - crop[0]), h * (crop[3] - crop[1])
    if fmt == "vertical":
        ch = max(420, min(1000, round((cw - 2 * PAD) * h / w) + 2 * PAD))
        # The card and its captions together, centred between the title (to y 400) and the footer (from y 1780).
        cy = max(cy, 400 + (1380 - (ch + 40 + band[3])) // 2)
        return (cx, cy, cw, ch), (band[0], cy + ch + 40, band[2], band[3])
    cw = max(560, min(cw, round((ch - 2 * PAD) * w / h) + 2 * PAD))
    return ((1920 - cw) // 2, cy, cw, ch), band


def scene_frame(p, fmt, head, scene, card):
    """The scene's backdrop: the brand, the episode, the scene's title, and the card its picture goes in."""
    cx, cy, cw, ch = card
    if fmt == "vertical":
        title = '<h2 style="left:60px;right:60px;top:196px;font-size:40px">%s</h2>' % esc(head["title"])
        title += '<h1 style="left:60px;right:60px;top:260px;font-size:84px">%s</h1>' % esc(scene["title"])
        title += ('<div class="brand" style="left:0;right:0;bottom:70px;justify-content:center;font-size:34px;color:#4b587d">'
                  'crewchat.dev</div>')
    else:
        title = '<h1 style="left:330px;right:330px;top:96px;font-size:58px;text-align:center">%s</h1>' % esc(scene["title"])
    card = '<div class="card" style="left:%dpx;top:%dpx;width:%dpx;height:%dpx"></div>' % (cx, cy, cw, ch)
    if scene.get("stat"):
        card += ('<div class="stat" style="left:%dpx;top:%dpx;width:%dpx;height:%dpx"><div class="n">%s</div>'
                 '<div class="t">%s</div></div>' % (cx, cy, cw, ch, esc(scene["stat"]), esc(scene.get("note", ""))))
    return p.png(fmt, head["chrome"] + title + card)


def title_frame(p, fmt, head, title, subtitle):
    w, h = FORMATS[fmt]["size"]
    big, small = (120, 52) if fmt == "vertical" else (110, 46)
    body = head["chrome"] + (
        '<div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;'
        'text-align:center;padding:0 90px"><h1 style="position:static;font-size:%dpx">%s</h1>'
        '<p class="tag" style="position:static;font-size:%dpx;margin-top:34px;max-width:900px">%s</p></div>'
        % (big, esc(title), small, esc(subtitle)))
    return p.png(fmt, body)


def caption_png(p, fmt, text, band):
    x, y, w, h = band
    size = 50 if fmt == "vertical" else 40
    body = '<div style="position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx"><div class="cap"><span style="font-size:%dpx">%s</span></div></div>' % (
        x, y, w, h, size, esc(text))
    return p.png(fmt, body, transparent=True)


def card_mask(p, fmt, card):
    """White where the card is (its rounded corners), black elsewhere: what the picture shows through."""
    cx, cy, cw, ch = card
    body = ('<div style="position:absolute;inset:0;background:#000"></div><div style="position:absolute;left:0;top:0;'
            'width:%dpx;height:%dpx;background:#fff;border-radius:34px"></div>' % (cw, ch))
    full = p.png(fmt, body)
    out = full.with_name("mask-%s-%dx%d.png" % (fmt, cw, ch))
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(full), "-vf", "crop=%d:%d:0:0,format=gray" % (cw, ch),
                    str(out)], check=True)
    return out


# ---------------------------------------------------------------------------------------------------------------
# The voice
# ---------------------------------------------------------------------------------------------------------------
class Voice:
    def __init__(self, voice, speed):
        from kokoro_onnx import Kokoro  # optional: only this tool needs it
        import soundfile
        self.k = Kokoro(str(VOICE / "kokoro-v1.0.onnx"), str(VOICE / "voices-v1.0.bin"))
        self.sf = soundfile
        self.voice, self.speed = voice, speed

    def say(self, text, path):
        samples, rate = self.k.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        self.sf.write(str(path), samples, rate, subtype="PCM_16")
        return len(samples) / rate


def wav_seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def narration(voice, text, work, stem):
    """The scene's narration, a sentence at a time with a short pause between: one WAV, and each sentence's
    (start, end, text) in it."""
    parts, cues, t = [], [], 0.25
    for i, line in enumerate(sentences(text)):
        path = work / ("%s-%d.wav" % (stem, i))
        seconds = voice.say(line, path)
        parts.append((path, t))
        cues.append((t, t + seconds, line))
        t += seconds + GAP
    total = t - GAP + TAIL
    out = work / ("%s.wav" % stem)
    inputs, mix = [], []
    for n, (path, start) in enumerate(parts):
        inputs += ["-i", str(path)]
        mix.append("[%d]adelay=%d:all=1[a%d]" % (n, int(start * 1000), n))
    mix.append("".join("[a%d]" % n for n in range(len(parts))) + "amix=inputs=%d:normalize=0,apad[o]" % len(parts))
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(mix), "-map", "[o]",
                    "-t", "%.3f" % total, "-ar", "48000", "-ac", "2", str(out)], check=True)
    return out, cues, total


# ---------------------------------------------------------------------------------------------------------------
# Putting a scene together
# ---------------------------------------------------------------------------------------------------------------
def probe_seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def render_scene(fmt, frame, media, card, mask, audio, cues, captions, seconds, out, crop=(0, 0, 1, 1)):
    """One scene as an MP4: the frame, the picture or clip in its card, the captions in turn, the narration."""
    w, h = FORMATS[fmt]["size"]
    cx, cy, cw, ch = card
    inputs = ["-loop", "1", "-framerate", str(FPS), "-i", str(frame)]
    filters, last = [], "[0]"
    n = 1
    if media is not None:
        src = ROOT / media
        if src.suffix.lower() in (".png", ".jpg", ".jpeg"):
            inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(src)]
            speed = 1.0
        else:
            length = probe_seconds(src) or seconds
            speed = min(2.5, max(1.0, length / max(seconds, 1)))  # the whole clip, a little faster when it is long
            inputs += (["-ignore_loop", "0"] if src.suffix.lower() == ".gif" else ["-stream_loop", "-1"]) + ["-i", str(src)]
        inputs += ["-loop", "1", "-i", str(mask)]
        pad = PAD
        x0, y0, x1, y1 = crop
        cut = "crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f," % (x1 - x0, y1 - y0, x0, y0) if crop != (0, 0, 1, 1) else ""
        filters.append(("[%d]setpts=PTS/%.3f,fps=%d," % (n, speed, FPS)) + cut +
                       "scale=%d:%d:force_original_aspect_ratio=decrease:flags=lanczos,"
                       "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=white,format=rgba[m]" % (cw - 2 * pad, ch - 2 * pad, cw, ch))
        filters.append("[%d]format=gray,scale=%d:%d[k]" % (n + 1, cw, ch))
        filters.append("[m][k]alphamerge[mk]")
        filters.append("%s[mk]overlay=%d:%d:shortest=0[v0]" % (last, cx, cy))
        last, n = "[v0]", n + 2
    for i, ((start, end, _), cap) in enumerate(zip(cues, captions)):
        inputs += ["-loop", "1", "-i", str(cap)]
        filters.append("%s[%d]overlay=0:0:enable='between(t,%.3f,%.3f)'[c%d]" % (last, n, start, end + GAP * 0.8, i))
        last, n = "[c%d]" % i, n + 1
    filters.append("%sfade=t=in:st=0:d=%.2f,fade=t=out:st=%.3f:d=%.2f,format=yuv420p[v]" % (last, FADE, seconds - FADE, FADE))
    inputs += ["-i", str(audio)]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(filters),
                    "-map", "[v]", "-map", "%d:a" % n, "-t", "%.3f" % seconds, "-r", str(FPS), "-s", "%dx%d" % (w, h),
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(out)], check=True)


def srt_time(t):
    ms = int(round(t * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def make_episode(spec, series, number, total, voice, outro, keep_work=False):
    out = OUT / spec["id"]
    work = out / "work"
    shutil.rmtree(out, ignore_errors=True)
    work.mkdir(parents=True)
    p = Painter(work)
    srt, srt_at, srt_n = [], 0.0, 0
    try:
        # The narration is the same for both formats: made once.
        scenes = [{"title": spec["title"], "say": spec["title"] + ". " + spec["tagline"] + ".", "kind": "title"}]
        scenes += [dict(s, kind="scene") for s in spec["scenes"]]
        scenes.append({"title": outro["title"], "say": outro["say"], "kind": "outro"})
        voiced = []
        for i, scene in enumerate(scenes):
            audio, cues, seconds = narration(voice, scene["say"], work, "say-%02d" % i)
            voiced.append((scene, audio, cues, seconds))
            for start, end, text in cues:
                srt_n += 1
                srt.append("%d\n%s --> %s\n%s\n" % (srt_n, srt_time(srt_at + start), srt_time(srt_at + end), written(text)))
            srt_at += seconds
        (out / "captions.srt").write_text("\n".join(srt), encoding="utf-8")
        for fmt in FORMATS:
            head = {"title": spec["title"], "chrome": brand_and_chip(fmt, series, number, total)}
            parts = []
            for i, (scene, audio, cues, seconds) in enumerate(voiced):
                media = scene.get("media") if scene["kind"] == "scene" else None
                crop = crop_of(fmt, scene)
                card, band = layout(fmt, media, crop)
                if scene["kind"] == "title":
                    frame = title_frame(p, fmt, head, spec["title"], spec["tagline"])
                elif scene["kind"] == "outro":
                    frame = title_frame(p, fmt, head, outro["title"], outro["subtitle"])
                else:
                    frame = scene_frame(p, fmt, head, scene, card)
                mask = card_mask(p, fmt, card) if media else None
                # A title card shows its words already: no captions on it.
                captions = [caption_png(p, fmt, written(text), band) for _, _, text in cues] if scene["kind"] == "scene" else []
                cues = cues if captions else []
                part = work / ("%s-%02d.mp4" % (fmt, i))
                render_scene(fmt, frame, media, card, mask, audio, cues, captions, seconds, part, crop)
                parts.append(part)
                if fmt == "vertical" and scene["kind"] == "title":
                    shutil.copy(frame, out / "thumbnail.png")
                if fmt == "landscape" and scene["kind"] == "title":
                    shutil.copy(frame, out / "thumbnail-landscape.png")
            listing = work / ("%s.txt" % fmt)
            listing.write_text("".join("file '%s'\n" % part.name for part in parts), encoding="utf-8")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                            "-c", "copy", "-movflags", "+faststart", str(out / ("%s.mp4" % fmt))], check=True, cwd=str(work))
        # The GIF, for X and LinkedIn: the episode's first scene, silent, from the landscape cut.
        first = work / "landscape-01.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(first), "-vf",
                        "fps=12,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160[p];"
                        "[b][p]paletteuse=dither=bayer:bayer_scale=4", "-loop", "0", str(out / "episode.gif")], check=True)
    finally:
        p.close()
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("episodes", nargs="*", help="episode ids (all if none)")
    ap.add_argument("--script", default=str(HERE / "episodes.json"))
    ap.add_argument("--voice", help="a Kokoro voice (default: the script's)")
    ap.add_argument("--keep", action="store_true", help="keep each episode's work folder")
    args = ap.parse_args(argv)
    script = json.loads(Path(args.script).read_text(encoding="utf-8"))
    voice = Voice(args.voice or script["voice"], script.get("speed", 1.0))
    chosen = [e for e in script["episodes"] if not args.episodes or e["id"] in args.episodes]
    for spec in chosen:
        number = script["episodes"].index(spec) + 1
        started = time.time()
        out = make_episode(spec, script["series"], number, len(script["episodes"]), voice, script["outro"], args.keep)
        seconds = probe_seconds(out / "vertical.mp4")
        print("%s: %.0f s long, made in %.0f s -> %s" % (spec["id"], seconds, time.time() - started, out.relative_to(ROOT)))


if __name__ == "__main__":
    main()
