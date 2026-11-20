"""Narezki: top Twitch clips of a game -> 9:16 mp4 -> Buffer (TikTok).   python run.py
Env: TWITCH_CLIENT_ID/SECRET, BUFFER_API_KEY, CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET.
NAREZKI_BUFFER = plan (default for anything unknown: lists the picks, downloads and sends nothing) | draft | queue | now. NAREZKI_COUNT overrides per_run.
Dedup: state.json (committed by the workflow) remembers every clip id, posted or failed, so nothing is sent twice."""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from buffer_api import Buffer, BufferError, Cloudinary

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
MODES = ("plan", "draft", "queue", "now")


def twitch_clips(cfg, env, now):
    """Clips of the last cfg.hours from cfg.games (names), or from the cfg.top_games most watched categories right now; most viewed first."""
    t = requests.post("https://id.twitch.tv/oauth2/token", timeout=30, data={
        "client_id": env["TWITCH_CLIENT_ID"], "client_secret": env["TWITCH_CLIENT_SECRET"], "grant_type": "client_credentials"})
    t.raise_for_status()
    h = {"Client-Id": env["TWITCH_CLIENT_ID"], "Authorization": f"Bearer {t.json()['access_token']}"}
    api = "https://api.twitch.tv/helix/"
    if cfg["games"]:
        games = requests.get(api + "games", headers=h, params=[("name", n) for n in cfg["games"]], timeout=30).json().get("data")
    else:
        games = requests.get(api + "games/top", headers=h, params={"first": 50}, timeout=30).json().get("data")
        games = [g for g in games if g["name"] not in cfg["exclude"]][:cfg["top_games"]]
    if not games:
        raise SystemExit("no Twitch categories found: check config.json games")
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    out = []
    for g in games:
        r = requests.get(api + "clips", headers=h, timeout=30, params={
            "game_id": g["id"], "first": 20, "started_at": (now - timedelta(hours=cfg["hours"])).strftime(fmt), "ended_at": now.strftime(fmt)})
        r.raise_for_status()
        out += [dict(c, game=g["name"]) for c in r.json()["data"]]
    return sorted(out, key=lambda c: -c["view_count"])


def pick(clips, seen, cfg, n):
    ok = [c for c in clips if c["id"] not in seen and c["duration"] >= cfg["min_sec"]
          and (not cfg["languages"] or c["language"] in cfg["languages"])]
    return ok[:n]


def caption(clip, cfg):
    return f"{clip['title']}\n\n🎮 {clip['broadcaster_name']} · {clip['game']} (Twitch)\n\n{cfg['hashtags']}"[:2200]


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
    todo = pick(twitch_clips(cfg, env, now), state, cfg, int(env.get("NAREZKI_COUNT") or cfg["per_run"]))
    print(f"mode={mode} picked={len(todo)}")
    failed = 0
    if mode != "plan" and todo:
        buf = Buffer(env["BUFFER_API_KEY"])
        ch = buf.tiktok_channel()
        cloud = Cloudinary(env["CLOUDINARY_CLOUD_NAME"], env["CLOUDINARY_API_KEY"], env["CLOUDINARY_API_SECRET"])
    for c in todo:
        print(f"- {c['id']} {c['game']} {c['view_count']} views {c['duration']}s {c['broadcaster_name']}: {c['title']}")
        if mode == "plan":
            continue
        try:
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "out.mp4"
                convert(download(c["url"], d), out)
                url = cloud.upload(out, f"narezki/{c['id']}")
            state[c["id"]] = {"status": mode, "post_id": buf.create_post(ch["id"], caption(c, cfg), url, mode), "at": now.isoformat(), "title": c["title"]}
        except (BufferError, subprocess.CalledProcessError, OSError, StopIteration) as e:
            failed += 1
            state[c["id"]] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:150]}", "at": now.isoformat()}  # kept: never auto-retried, a half-done post must not duplicate
            print(f"  FAILED: {state[c['id']]['error']}")
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
