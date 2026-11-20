"""Narezki: top clips of the most watched Russian-speaking Twitch channels -> 9:16 mp4 -> Buffer (TikTok).   python run.py
No Twitch API key: the channel list is refreshed on every run from the TwitchTracker Russian ranking, clips are listed and downloaded with yt-dlp.
Env: BUFFER_API_KEY, CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET.
NAREZKI_BUFFER = plan (default for anything unknown: lists the picks, downloads and sends nothing) | draft | queue | now. NAREZKI_COUNT overrides per_run.
Dedup: state.json (committed by the workflow) remembers every clip id, posted or failed, so nothing is sent twice."""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from buffer_api import Buffer, BufferError, Cloudinary

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
MODES = ("plan", "draft", "queue", "now")


def parse_ranking(html):
    """Channel logins of the TwitchTracker table, in rank order."""
    return list(dict.fromkeys(re.findall(r'<td>#\d+</td>\s*<td>\s*<a href="/(\w+)"', html)))


def channels(cfg):
    """cfg.channels (manual + fallback) plus the current top of the ranking; blocked words removed.
    shortcut: if the ranking site blocks the runner (Cloudflare) only cfg.channels is used, upgrade to another ranking source then."""
    found = []
    try:
        r = requests.get(cfg["ranking_url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        found = parse_ranking(r.text)[:cfg["ranking_top"]]
    except requests.RequestException as e:
        print(f"ranking unavailable ({str(e)[:80]}), using config channels only")
    if not found:
        print("ranking gave no channels, using config channels only")
    allowed = [c for c in dict.fromkeys(cfg["channels"] + found) if not any(w in c.lower() for w in cfg["block_words"])]
    print(f"channels ({len(allowed)}): {', '.join(allowed)}")
    return allowed


def list_clips(channel, rng):
    """Most popular clips of a channel in range 24hr|7d|30d (yt-dlp, flat). A channel without clips or an error gives []."""
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--flat-playlist", "--playlist-end", "10",
                        f"https://www.twitch.tv/{channel}/clips?filter=clips&range={rng}"],
                       capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONUTF8="1"))
    if r.returncode:
        return []
    return [dict(e, channel=channel) for e in json.loads(r.stdout).get("entries", [])]


def pick(clips, seen, cfg, n):
    ok = [c for c in clips if c["id"] not in seen and (c.get("duration") or 0) >= cfg["min_sec"] and (c.get("view_count") or 0) >= cfg["min_views"]]
    return sorted(ok, key=lambda c: -c["view_count"])[:n]


def caption(clip, cfg):
    return f"{clip.get('title') or ''}\n\n🎮 {clip['channel']} (Twitch)\n\n{cfg['hashtags']}"[:2200]


def convert(src, dst):
    """Any clip -> 1080x1920 H.264/AAC 30 fps: blurred fill behind, the clip centred (keeps facecam and HUD whole)."""
    fc = ("[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5[bg];"
          "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p[v]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)], check=True)
    p = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(dst)], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in p["streams"] if s["codec_type"] == "video")
    if (v["width"], v["height"]) != (1080, 1920) or not any(s["codec_type"] == "audio" for s in p["streams"]):
        raise BufferError("converted file is not 1080x1920 with sound")


def download(url, folder):
    subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "-o", str(Path(folder) / "src.%(ext)s"), url], check=True)
    return next(Path(folder).glob("src.*"))


def main(env=os.environ):
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    mode = (env.get("NAREZKI_BUFFER") or "draft").strip().lower()
    mode = mode if mode in MODES else "plan"  # a typo must never turn into real publishing
    now = datetime.now(timezone.utc)
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    chans, todo = channels(cfg), []
    for rng in cfg["ranges"]:  # the freshest range that still has something new
        todo = pick([c for ch in chans for c in list_clips(ch, rng)], state, cfg, int(env.get("NAREZKI_COUNT") or cfg["per_run"]))
        if todo:
            break
    print(f"mode={mode} picked={len(todo)}")
    failed = 0
    if mode != "plan" and todo:
        buf = Buffer(env["BUFFER_API_KEY"])
        ch = buf.tiktok_channel()
        cloud = Cloudinary(env["CLOUDINARY_CLOUD_NAME"], env["CLOUDINARY_API_KEY"], env["CLOUDINARY_API_SECRET"])
    for c in todo:
        print(f"- {c['id']} {c['channel']} {c['view_count']} views {c['duration']}s: {c.get('title')}")
        if mode == "plan":
            continue
        try:
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "out.mp4"
                convert(download(c["url"], d), out)
                url = cloud.upload(out, f"narezki/{c['id']}")
            state[c["id"]] = {"status": mode, "post_id": buf.create_post(ch["id"], caption(c, cfg), url, mode), "at": now.isoformat(), "title": c.get("title")}
        except (BufferError, subprocess.CalledProcessError, OSError, StopIteration) as e:
            failed += 1
            state[c["id"]] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:150]}", "at": now.isoformat()}  # kept: never auto-retried, a half-done post must not duplicate
            print(f"  FAILED: {state[c['id']]['error']}")
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
