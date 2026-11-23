"""Clip processing: nudity check (NudeNet), Russian subtitles (faster-whisper) and the 9:16 render (ffmpeg + ASS).
Layout 1080x1920: blurred copy of the clip as background, title + channel on top, the whole clip (nothing cropped, facecam and HUD stay) in the middle,
big word-by-word subtitles under it."""
import subprocess
from pathlib import Path

# exposed = shown uncovered; the *_COVERED classes are allowed on purpose (covered nudity may be posted)
EXPOSED = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
_models = {}


def is_nsfw(detections, thr):
    return any(d["class"] in EXPOSED and d["score"] >= thr for d in detections)


def nsfw_clip(src, duration, folder, thr, fps=2):
    """True when any frame (`fps` per second) shows exposed nudity. shortcut: a flash shorter than 1/fps s can slip through, raise `fps` if it does."""
    from nudenet import NudeDetector
    det = _models.setdefault("nude", NudeDetector())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", f"fps={fps},scale=960:-2",
                    str(Path(folder) / "n%03d.jpg")], check=True)
    return any(is_nsfw(det.detect(str(f)), thr) for f in sorted(Path(folder).glob("n*.jpg")))


def words_of(src, model="small", language="ru"):
    """[(start, end, word)] from faster-whisper; [] when there is no speech or the model is not available."""
    try:
        from faster_whisper import WhisperModel
        m = _models.setdefault("whisper", WhisperModel(model, device="cpu", compute_type="int8"))
        segs, _ = m.transcribe(str(src), language=language, word_timestamps=True, vad_filter=True, beam_size=1)
        return [(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or []) if w.word.strip()]
    except Exception as e:  # subtitles are a bonus: the video still goes out without them
        print(f"  no subtitles: {type(e).__name__}: {str(e)[:100]}")
        return []


ROOTS = ("хуй", "хуе", "хуя", "хуё", "пизд", "ебат", "ебан", "ебал", "ёб", "уеб", "заеб", "блят", "бляд")


def censor(word):
    """Hard swearing in the subtitles becomes 'х***' (TikTok demotes it). shortcut: a root list, not a dictionary, add roots when one slips through."""
    return word[0] + "*" * (len(word) - 1) if any(r in word.lower() for r in ROOTS) else word


def _t(s):
    cs = round(s * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _clean(s):
    return s.replace("{", "(").replace("}", ")").replace("\n", " ")


def ass(title, channel, words, duration, per_chunk=4):
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            "Style: Title,Arial,74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,5,0,8,70,70,300,1\n"
            "Style: Chan,Arial,46,&H0000D7FF,&H0000D7FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,8,70,70,180,1\n"
            "Style: Sub,Arial,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,8,60,60,1200,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    end = _t(duration)
    ev = [f"Dialogue: 0,{_t(0)},{end},Chan,,0,0,0,,{_clean(channel)}", f"Dialogue: 0,{_t(0)},{end},Title,,0,0,0,,{_clean(title)}"]
    chunks = [words[i:i + per_chunk] for i in range(0, len(words), per_chunk)]
    for i, c in enumerate(chunks):
        start, nxt = c[0][0], chunks[i + 1][0][0] if i + 1 < len(chunks) else duration
        # cap: a wrong word timestamp must not hold the text on screen, and an event that ends before it starts makes libass drop the others
        stop = min(c[-1][1] + 0.15, start + 0.6 * len(c) + 0.5, nxt, duration)
        if stop > start:
            ev.append(f"Dialogue: 0,{_t(start)},{_t(stop)},Sub,,0,0,0,,{_clean(' '.join(censor(w[2]) for w in c))}")
    return head + "\n".join(ev) + "\n"


def render(src, dst, folder, title, channel, words, duration):
    """-> dst, 1080x1920 H.264/AAC 30 fps. Raises when the result is not 1080x1920 with sound."""
    (Path(folder) / "subs.ass").write_text(ass(title, channel, words, duration), encoding="utf-8")
    fc = ("[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=40:6,eq=brightness=-0.15[bg];"
          "[b]scale=1080:-2[fg];[bg][fg]overlay=0:(H-h)/2-100,fps=30,subtitles=subs.ass,format=yuv420p[v]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)],
                   check=True, cwd=folder)  # cwd: the subtitles filter takes a relative path (a Windows drive colon breaks the filter syntax)
    import json
    p = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(dst)], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in p["streams"] if s["codec_type"] == "video")
    if (v["width"], v["height"]) != (1080, 1920) or not any(s["codec_type"] == "audio" for s in p["streams"]):
        raise ValueError("converted file is not 1080x1920 with sound")
    return dst
