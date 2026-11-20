import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import media  # noqa: E402
import run  # noqa: E402

CFG = {"min_sec": 15, "min_views": 100, "hashtags": "#x", "bad_title_words": ["18+", "стриптиз"]}


def clip(i, dur=30, views=500, title="t"):
    return {"id": i, "duration": dur, "view_count": views, "title": title, "channel": "x"}


def test_pick_skips_seen_short_unpopular_and_dirty_titles_then_sorts_by_views():
    clips = [clip("seen"), clip("short", dur=5), clip("low", views=5), clip("dirty", title="Клуб СТРИПТИЗ"), clip("a", views=200), clip("b", views=900)]
    assert [c["id"] for c in run.pick(clips, {"seen": {}}, CFG)] == ["b", "a"]


def test_caption_fits_tiktok_limit():
    assert len(run.caption(clip("a", title="t" * 3000), CFG)) == 2200


def test_parse_ranking_keeps_rank_order_and_ignores_menu_links():
    html = '<a href="/games">g</a><tr><td>#1</td><td> <a href="/honeymad" x>a</a><a href="/honeymad">b</a></td></tr><tr><td>#2</td><td>\n<a href="/strogo">'
    assert run.parse_ranking(html) == ["honeymad", "strogo"]


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
