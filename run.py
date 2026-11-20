"""Narezki: clips of the most watched Russian-speaking Twitch channels -> checks -> Claude picks the best -> 9:16 mp4 with hook title and subtitles -> Buffer (TikTok).
python run.py. No Twitch API key: the channel list (with average viewers) is refreshed on every run from the TwitchTracker Russian ranking, clips are listed and
downloaded with yt-dlp.
Selection: a clip's score = its views / the channel's average viewers (so small channels with a strong clip beat a big channel's average one); one clip per channel,
a channel that was posted in the last channel_cooldown_hours is skipped; the best `shortlist` clean clips (nudity check + transcript) go to Claude, which picks
the funniest/most surprising and writes the hook. No Claude (no CLI/token/answer) = the best score with the viewers' title.
Env: BUFFER_API_KEY, CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET, CLAUDE_CODE_OAUTH_TOKEN (optional).
NAREZKI_BUFFER = plan (default for anything unknown: lists the ranked candidates, downloads and sends nothing) | local (renders into out/, sends nothing, writes no state)
                 | draft | queue | now (Buffer). NAREZKI_COUNT overrides per_run.
Dedup: state.json (committed by the workflow) remembers every clip that was posted, failed or rejected, so nothing is checked or sent twice."""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import curate
import media
from buffer_api import Buffer, BufferError, Cloudinary

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
MODES = ("plan", "local", "draft", "queue", "now")
POSTED = ("draft", "queue", "now")


def parse_ranking(html):
    """{login: average viewers} of the TwitchTracker table, in rank order."""
    return {m[0]: int(m[1]) for m in re.findall(r'<td>#\d+</td>\s*<td>\s*<a href="/(\w+)".*?class="color-viewers"><span class="to-number">(\d+)</span>', html, re.S)}


def channels(cfg):
    """-> (channel list, {login: avg viewers}). cfg.channels (manual + fallback) plus the current top of the ranking; blocked words removed.
    shortcut: if the ranking site blocks the runner (Cloudflare) only cfg.channels is used (average viewers then default_avg), upgrade to another source then."""
    ranking = {}
    try:
        r = requests.get(cfg["ranking_url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        ranking = parse_ranking(r.text)
    except requests.RequestException as e:
        print(f"ranking unavailable ({str(e)[:80]}), using config channels only")
    if not ranking:
        print("ranking gave no channels, using config channels only")
    top = list(ranking)[:cfg["ranking_top"]]
    allowed = [c for c in dict.fromkeys(cfg["channels"] + top) if not any(w in c.lower() for w in cfg["block_words"])]
    print(f"channels ({len(allowed)}): {', '.join(allowed)}")
    return allowed, ranking


def list_clips(channel, rng):
    """Most popular clips of a channel in range 24hr|7d|30d (yt-dlp, flat). A channel without clips or an error gives []."""
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--flat-playlist", "--playlist-end", "10",
                        f"https://www.twitch.tv/{channel}/clips?filter=clips&range={rng}"],
                       capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONUTF8="1"))
    if r.returncode:
        return []
    return [dict(e, channel=channel) for e in json.loads(r.stdout).get("entries", [])]


def pick(clips, seen, cfg):
    """Every new clip that is long and popular enough with a clean title."""
    return [c for c in clips if c["id"] not in seen and (c.get("duration") or 0) >= cfg["min_sec"] and (c.get("view_count") or 0) >= cfg["min_views"]
            and not any(w in (c.get("title") or "").lower() for w in cfg["bad_title_words"])]


def rank(clips, state, cfg, avg, now):
    """New clips scored by views / channel average viewers, best clip per channel, channels posted within the cooldown skipped. Best score first."""
    since = now - timedelta(hours=cfg["channel_cooldown_hours"])
    cool = {v.get("channel") for v in state.values() if v.get("status") in POSTED and datetime.fromisoformat(v["at"]) > since}
    best = {}
    for c in sorted((dict(c, score=c["view_count"] / max(avg.get(c["channel"], cfg["default_avg"]), cfg["min_avg"]))
                     for c in pick(clips, state, cfg) if c["channel"] not in cool), key=lambda c: -c["score"]):
        best.setdefault(c["channel"], c)
    return list(best.values())


def caption(clip, cfg, hook=None):
    return f"{hook or clip.get('title') or ''}\n\n🎮 {clip['channel']} (Twitch)\n\n{cfg['hashtags']}"[:2200]


def download(url, folder):
    subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "-o", str(Path(folder) / "src.%(ext)s"), url], check=True)
    return next(Path(folder).glob("src.*"))


def prepare(c, cfg, folder):
    """Download, nudity check, transcript. -> (src, words, None) or (None, None, 'nsfw')."""
    src = download(c["url"], folder)
    if media.nsfw_clip(src, c["duration"], folder, cfg["nsfw_score"]):
        return None, None, "nsfw"
    return src, media.words_of(src, cfg["whisper_model"], cfg["language"]), None


def main(env=os.environ):
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    mode = (env.get("NAREZKI_BUFFER") or "draft").strip().lower()
    mode = mode if mode in MODES else "plan"  # a typo must never turn into real publishing
    now, n = datetime.now(timezone.utc), int(env.get("NAREZKI_COUNT") or cfg["per_run"])
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    chans, avg = channels(cfg)
    ranked, clips = [], {}
    for rng in cfg["ranges"]:  # the freshest range first, the next one only while there are fewer candidates than the shortlist
        clips.update({c["id"]: c for ch in chans for c in list_clips(ch, rng)})
        ranked = rank(list(clips.values()), state, cfg, avg, now)
        if len(ranked) >= cfg["shortlist"]:
            break
    print(f"mode={mode} candidates={len(ranked)}")
    if mode == "plan" or not ranked:
        for c in ranked[:cfg["shortlist"]]:
            print(f"- {c['id']} {c['channel']} score {c['score']:.1f} ({c['view_count']} views) {c['duration']}s: {c.get('title')}")
        return 0
    if mode in POSTED:
        buf = Buffer(env["BUFFER_API_KEY"])
        ch = buf.tiktok_channel()
        cloud = Cloudinary(env["CLOUDINARY_CLOUD_NAME"], env["CLOUDINARY_API_KEY"], env["CLOUDINARY_API_SECRET"])

    def save(c, **rec):
        if mode != "local":  # a local test run never touches the state the schedule relies on
            state[c["id"]] = dict(rec, channel=c["channel"], title=c.get("title"), at=now.isoformat())
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    done = failed = 0
    with tempfile.TemporaryDirectory() as work:
        ready = []  # clean clips with their transcript. shortcut: clips that were checked but not picked are checked again next run, cache them if the runs get slow
        for c in ranked[:cfg["max_tries"]]:
            if len(ready) == cfg["shortlist"]:
                break
            folder = Path(work) / str(c["id"])
            folder.mkdir()
            try:
                src, words, why = prepare(c, cfg, folder)
            except (subprocess.CalledProcessError, OSError, StopIteration) as e:
                print(f"- {c['channel']} {c['id']}: skipped, {type(e).__name__}")  # not recorded: may be a passing download error
                continue
            print(f"- {c['channel']} {c['id']} score {c['score']:.1f}: {why or 'ok'}")
            if why:
                save(c, status="rejected", why=why)
            else:
                ready.append(dict(c, src=src, folder=folder, words=words, text=" ".join(w[2] for w in words)))
        if not ready:
            print("no clean clips this run")
            return 0
        try:
            picks = curate.choose(ready, n, cfg)
            print("claude picked:", picks)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as e:
            print(f"claude unavailable ({type(e).__name__}: {str(e)[:100]}), using the best score")
            picks = [(str(c["id"]), None) for c in ready[:n]]
        byid = {str(c["id"]): c for c in ready}
        for cid, hook in picks:
            c = byid[cid]
            try:
                dst = ROOT / "out" / f"{c['channel']}_{cid}.mp4" if mode == "local" else Path(work) / f"{cid}.mp4"
                dst.parent.mkdir(exist_ok=True)
                media.render(c["src"], dst, c["folder"], hook or c.get("title") or "", c["channel"], c["words"], c["duration"])
                if mode == "local":
                    print(f"  -> {dst}  hook: {hook}")
                else:
                    save(c, status=mode, hook=hook, post_id=buf.create_post(ch["id"], caption(c, cfg, hook), cloud.upload(dst, f"narezki/{cid}"), mode))
                done += 1
            except (BufferError, subprocess.CalledProcessError, OSError, ValueError) as e:
                failed += 1
                save(c, status="error", error=f"{type(e).__name__}: {str(e)[:150]}")  # kept: never auto-retried, a half-done post must not duplicate
                print(f"  FAILED {cid}: {type(e).__name__}: {str(e)[:150]}")
    print(f"done={done} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
