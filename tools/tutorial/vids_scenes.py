"""Render the tutorial's scenes for Google Vids: one MP4 per scene, each exactly as long as its
narration, so picture and voice stay together.

    pip install websocket-client
    python3 tools/tutorial/vids_scenes.py                       # every scene of scenes.json
    python3 tools/tutorial/vids_scenes.py talk files            # only some
    python3 tools/tutorial/vids_scenes.py --script firebase.json  # another video's script

Reads tools/tutorial/scenes.json (or the --script given) (the script, and each scene's narration length as Google Vids
reported it) and the recordings in media/clips (tools/demo_recordings.py, console_clips.py). Writes media/vids/ (scenes.json)
or media/vids/<script name>/.
In Vids, give every scene its voiceover first, read the lengths, put them in scenes.json, then
drop each rendered scene into its scene.
"""
import html
import json
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(HERE))
import docs_screenshots as shots  # noqa: E402
import make_video as mv  # noqa: E402  (the slide design)

OUT = ROOT / "media" / "vids"
CLIPS = ROOT / "media" / "clips"
MEDIA = (80, 276, 1760, 768)  # where a recording goes on a slide: x, y, width, height
PAD = 0.5  # seconds longer than the narration, so the scene never runs out of picture


def clip_size(path):
    out = subprocess.run(["mdls", "-name", "kMDItemPixelWidth", "-name", "kMDItemPixelHeight", str(path)],
                         capture_output=True, text=True).stdout
    nums = [int(x) for x in re.findall(r"= (\d+)", out)]
    return (nums[1], nums[0]) if len(nums) == 2 else (1920, 1080)  # mdls lists height first


def fit(width, height):
    x, y, w, h = MEDIA
    if width / height >= w / h:
        fw, fh = w, w * height / width
    else:
        fw, fh = h * width / height, h
    return [round(x + (w - fw) / 2), round(y + (h - fh) / 2), round(fw), round(fh)]


def clip_slide(scene, chapter, rect):
    head = mv.brand(chapter) + '<h1>%s</h1><p class="cap">%s</p>' % (
        html.escape(scene["heading"]), html.escape(scene.get("caption", "")))
    frame = ('<div style="position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;border-radius:18px;background:#fff;'
             'box-shadow:0 24px 60px rgba(20,35,80,.18),0 2px 8px rgba(20,35,80,.10)"></div>' % tuple(rect))
    return mv.page(head + frame)


def main():
    args = sys.argv[1:]
    script = "scenes.json"
    if args[:1] == ["--script"]:
        script, args = args[1], args[2:]
    spec = json.loads((HERE / script).read_text(encoding="utf-8"))
    out = OUT if script == "scenes.json" else OUT / Path(script).stem
    out.mkdir(parents=True, exist_ok=True)
    (OUT / "work").mkdir(exist_ok=True)
    mv.WORK = OUT / "work"
    silence = OUT / "work" / "silence.wav"
    with wave.open(str(silence), "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(48000)
        w.writeframes(b"\0" * 4 * 48000 * 40)
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(silence), str(silence.with_suffix(".m4a"))], check=True)
    silence = silence.with_suffix(".m4a")
    binary = OUT / "work" / "scene"
    subprocess.run(["swiftc", "-O", str(HERE / "scene.swift"), "-o", str(binary)], check=True, capture_output=True)
    browser = shots.Browser()
    chapter = ""
    try:
        for i, (scene, seconds) in enumerate(zip(spec["scenes"], spec["durations"])):
            chapter = scene.get("chapter", chapter)
            if args and scene["id"] not in args:
                continue
            name = "%02d-%s" % (i + 1, scene["id"])
            png = OUT / "work" / (name + ".png")
            job = {"background": str(png), "duration": seconds + PAD, "output": str(out / (name + ".mp4")),
                   "silence": str(silence), "lead": 0.4, "tail": 0.9}
            if scene["kind"] == "clip":
                clip = CLIPS / scene["clip"]
                rect = fit(*clip_size(clip))
                mv.render(browser, clip_slide(scene, chapter, rect), png)
                job.update(clip=str(clip), rect=rect)
            else:
                mv.render(browser, mv.slide_html(scene, chapter), png)
            (OUT / "work" / (name + ".json")).write_text(json.dumps(job))
            subprocess.run([str(binary), str(OUT / "work" / (name + ".json"))], check=True)
    finally:
        browser.close()
        shutil.rmtree(shots.TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
