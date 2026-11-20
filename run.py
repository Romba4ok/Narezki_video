"""Narezki: top clips of the most watched Russian-speaking Twitch channels -> checks -> 9:16 mp4 with title and subtitles -> Buffer (TikTok).   python run.py
No Twitch API key: the channel list is refreshed on every run from the TwitchTracker Russian ranking, clips are listed and downloaded with yt-dlp.
Env: BUFFER_API_KEY, CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET.
NAREZKI_BUFFER = plan (default for anything unknown: lists the candidates, downloads and sends nothing) | local (renders into out/, sends nothing, writes no state)
                 | draft | queue | now (Buffer). NAREZKI_COUNT overrides per_run.
Dedup: state.json (committed by the workflow) remembers every clip id that was posted, failed or rejected, so nothing is checked or sent twice."""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

import media
from buffer_api import Buffer, BufferError, Cloudinary

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
MODES = ("plan", "local", "draft", "queue", "now")


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


def pick(clips, seen, cfg):
    """Every new clip that is long and popular enough with a clean title, most viewed first."""
    ok = [c for c in clips if c["id"] not in seen and (c.get("duration") or 0) >= cfg["min_sec"] and (c.get("view_count") or 0) >= cfg["min_views"]
          and not any(w in (c.get("title") or "").lower() for w in cfg["bad_title_words"])]
    return sorted(ok, key=lambda c: -c["view_count"])


def caption(clip, cfg):
    return f"{clip.get('title') or ''}\n\n🎮 {clip['channel']} (Twitch)\n\n{cfg['hashtags']}"[:2200]


def download(url, folder):
    subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "-o", str(Path(folder) / "src.%(ext)s"), url], check=True)
    return next(Path(folder).glob("src.*"))


def make(c, cfg, dst):
    """Download, nudity check, subtitles, render. -> None when OK, else the rejection reason (nothing is rendered for a rejected clip)."""
    with tempfile.TemporaryDirectory() as d:
        src = download(c["url"], d)
        if media.nsfw_clip(src, c["duration"], d, cfg["nsfw_score"]):
            return "nsfw"
        words = media.words_of(src, cfg["whisper_model"], cfg["language"])
        media.render(src, dst, d, c.get("title") or "", c["channel"], words, c["duration"])
    return None


def main(env=os.environ):
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    mode = (env.get("NAREZKI_BUFFER") or "draft").strip().lower()
    mode = mode if mode in MODES else "plan"  # a typo must never turn into real publishing
    now, n = datetime.now(timezone.utc), int(env.get("NAREZKI_COUNT") or cfg["per_run"])
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    chans, todo = channels(cfg), []
    for rng in cfg["ranges"]:  # the freshest range that still has something new
        todo = pick([c for ch in chans for c in list_clips(ch, rng)], state, cfg)
        if todo:
            break
    print(f"mode={mode} candidates={len(todo)}")
    if mode == "plan":
        for c in todo[:n]:
            print(f"- {c['id']} {c['channel']} {c['view_count']} views {c['duration']}s: {c.get('title')}")
        return 0
    if mode in ("draft", "queue", "now") and todo:
        buf = Buffer(env["BUFFER_API_KEY"])
        ch = buf.tiktok_channel()
        cloud = Cloudinary(env["CLOUDINARY_CLOUD_NAME"], env["CLOUDINARY_API_KEY"], env["CLOUDINARY_API_SECRET"])
    done = failed = 0
    for c in todo[:cfg["max_tries"]]:
        if done == n:
            break
        print(f"- {c['id']} {c['channel']} {c['view_count']} views {c['duration']}s: {c.get('title')}")
        rec = {"at": now.isoformat(), "title": c.get("title")}
        try:
            with tempfile.TemporaryDirectory() as t:
                dst = Path(t) / "out.mp4" if mode != "local" else ROOT / "out" / f"{c['channel']}_{c['id']}.mp4"
                dst.parent.mkdir(exist_ok=True)
                why = make(c, cfg, dst)
                if why:
                    rec.update(status="rejected", why=why)
                    print(f"  rejected: {why}")
                elif mode == "local":
                    done += 1
                    print(f"  -> {dst}")
                else:
                    rec.update(status=mode, post_id=buf.create_post(ch["id"], caption(c, cfg), cloud.upload(dst, f"narezki/{c['id']}"), mode))
                    done += 1
        except (BufferError, subprocess.CalledProcessError, OSError, StopIteration, ValueError) as e:
            failed += 1
            rec.update(status="error", error=f"{type(e).__name__}: {str(e)[:150]}")  # kept: never auto-retried, a half-done post must not duplicate
            print(f"  FAILED: {rec['error']}")
        if mode != "local":
            state[c["id"]] = rec
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done={done} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
