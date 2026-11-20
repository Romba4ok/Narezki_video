import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import run  # noqa: E402

CFG = {"min_sec": 15, "min_views": 100, "hashtags": "#x"}


def clip(i, dur=30, views=500):
    return {"id": i, "duration": dur, "view_count": views, "title": "t" * 3000, "channel": "x"}


def test_pick_skips_seen_short_and_unpopular_then_sorts_by_views():
    clips = [clip("seen"), clip("short", dur=5), clip("low", views=5), clip("a", views=200), clip("b", views=900)]
    assert [c["id"] for c in run.pick(clips, {"seen": {}}, CFG, 2)] == ["b", "a"]


def test_caption_fits_tiktok_limit():
    assert len(run.caption(clip("a"), CFG)) == 2200


def test_parse_ranking_keeps_rank_order_and_ignores_menu_links():
    html = '<a href="/games">g</a><tr><td>#1</td><td> <a href="/honeymad" x>a</a><a href="/honeymad">b</a></td></tr><tr><td>#2</td><td>\n<a href="/strogo">'
    assert run.parse_ranking(html) == ["honeymad", "strogo"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_convert_makes_1080x1920_with_sound(tmp_path):
    src, dst = tmp_path / "in.mp4", tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:d=2", "-f", "lavfi", "-i", "sine=d=2",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(src)], check=True)
    run.convert(src, dst)  # raises when the output is not 1080x1920 with an audio stream
