import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import curate  # noqa: E402
import media  # noqa: E402
import run  # noqa: E402

CFG = {"min_sec": 15, "min_views": 100, "hashtags": "#x", "bad_title_words": ["18+", "стриптиз"], "default_avg": 5000, "min_avg": 500, "channel_cooldown_hours": 24}
NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def clip(i, dur=30, views=500, title="t", channel="x"):
    return {"id": i, "duration": dur, "view_count": views, "title": title, "channel": channel}


def test_pick_skips_seen_short_unpopular_and_dirty_titles():
    clips = [clip("seen"), clip("short", dur=5), clip("low", views=5), clip("dirty", title="Клуб СТРИПТИЗ"), clip("a")]
    assert [c["id"] for c in run.pick(clips, {"seen": {}}, CFG)] == ["a"]


def test_rank_scores_by_views_per_channel_average_one_per_channel_and_cooldown():
    clips = [clip("big", views=9000, channel="big"), clip("small", views=1500, channel="small"), clip("small2", views=1400, channel="small"),
             clip("cool", views=99999, channel="cool")]
    state = {"old": {"status": "now", "channel": "cool", "at": (NOW - timedelta(hours=3)).isoformat()}}
    r = run.rank(clips, state, CFG, {"big": 50000, "small": 1000, "cool": 100}, NOW)
    assert [c["id"] for c in r] == ["small", "big"]  # 1.5 beats 0.18; one clip per channel; "cool" posted 3 h ago


def test_caption_uses_hook_and_fits_tiktok_limit():
    assert run.caption(clip("a"), CFG, "Хук!").startswith("Хук!")
    assert len(run.caption(clip("a", title="t" * 3000), CFG)) == 2200


def test_parse_ranking_gives_average_viewers_in_rank_order():
    html = ('<a href="/games">g</a><tr><td>#1</td><td> <a href="/honeymad"><img></a></td><td><a href="/honeymad">HoneyMad</a></td>'
            '<td class="color-viewers"><span class="to-number">135979</span></td></tr>'
            '<tr><td>#2</td><td>\n<a href="/strogo"></a></td><td class="color-viewers"><span class="to-number">26193</span></td></tr>')
    assert run.parse_ranking(html) == {"honeymad": 135979, "strogo": 26193}


ITEMS = [{"id": 1, "channel": "a", "view_count": 5, "score": 1.0, "duration": 20, "title": "t", "text": "x"},
         {"id": 2, "channel": "b", "view_count": 5, "score": 0.5, "duration": 20, "title": "t", "text": "y"}]


def test_choose_validates_claude_answer_and_fills_up_to_n():
    cfg = {}
    ok = lambda p, c: '```json\n{"picks":[{"id":"2","hook":"«Он не ожидал»"},{"id":"99","hook":"чужой"}]}\n```'  # noqa: E731
    assert curate.choose(ITEMS, 2, cfg, ok) == [("2", "Он не ожидал"), ("1", None)]  # unknown id dropped, quotes stripped, filled by score
    swear = lambda p, c: '{"picks":[{"id":"1","hook":"ну и хуйня"}]}'  # noqa: E731
    assert curate.choose(ITEMS, 1, cfg, swear) == [("1", None)]  # a swearing hook is not used
    with pytest.raises(ValueError):
        curate.choose(ITEMS, 1, cfg, lambda p, c: "не знаю")


def test_nsfw_blocks_exposed_but_allows_covered_and_low_scores():
    d = lambda c, s: [{"class": c, "score": s}]  # noqa: E731
    assert media.is_nsfw(d("FEMALE_BREAST_EXPOSED", 0.6), 0.45)
    assert not media.is_nsfw(d("FEMALE_BREAST_COVERED", 0.9), 0.45)
    assert not media.is_nsfw(d("BUTTOCKS_EXPOSED", 0.3), 0.45)


def test_ass_has_title_channel_and_chunked_subtitles_and_no_ass_injection():
    a = media.ass("hi {\\b1}\nthere", "chan", [(0.0, 0.5, "a"), (0.5, 1.0, "b"), (1.0, 1.5, "c")], 5, per_chunk=2)
    assert a.count("Dialogue:") == 4 and "0:00:00.00,0:00:01.00,Sub,,0,0,0,,a b" in a and "{\\b1}" not in a


def test_ass_drops_late_words_and_caps_long_chunks_and_censors():
    a = media.ass("t", "c", [(0.0, 9.0, "уёбища"), (9.5, 9.9, "ok"), (36.1, 36.5, "late")], 36, per_chunk=1)
    subs = [line for line in a.splitlines() if ",Sub," in line]
    assert len(subs) == 2 and subs[0].endswith("у*****") and "0:00:01.10" in subs[0] and "late" not in a  # 0.6*1+0.5 s cap


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_render_makes_1080x1920_with_sound_and_subtitles(tmp_path):
    src, dst = tmp_path / "in.mp4", tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:d=2", "-f", "lavfi", "-i", "sine=d=2",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(src)], check=True)
    media.render(src, dst, tmp_path, "Привет мир", "kanal", [(0.2, 0.8, "тест"), (0.8, 1.5, "слова")], 2)  # raises when not 1080x1920 with audio
