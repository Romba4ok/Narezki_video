import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import run  # noqa: E402

CFG = {"min_sec": 15, "languages": ["ru"], "hashtags": "#gta6"}


def clip(i, dur=30, lang="ru", views=1):
    return {"id": i, "duration": dur, "language": lang, "view_count": views, "title": "t" * 3000, "broadcaster_name": "x", "game": "g"}


def test_pick_skips_seen_short_and_other_language():
    clips = [clip("seen"), clip("short", dur=5), clip("en", lang="en"), clip("a"), clip("b")]
    assert [c["id"] for c in run.pick(clips, {"seen": {}}, CFG, 1)] == ["a"]


def test_caption_fits_tiktok_limit():
    assert len(run.caption(clip("a"), CFG)) == 2200


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_convert_makes_1080x1920_with_sound(tmp_path):
    src, dst = tmp_path / "in.mp4", tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:d=2", "-f", "lavfi", "-i", "sine=d=2",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(src)], check=True)
    run.convert(src, dst)  # raises when the output is not 1080x1920 with an audio stream
