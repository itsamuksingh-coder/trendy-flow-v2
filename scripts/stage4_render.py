"""
Stage 4 -- Render final vertical (1080x1920) MP4 videos using ONE fixed,
reusable template:

    [intro.mp4] -> [content: assets + narration + ambient bed + subtitles] -> [outro.mp4]

Fixed template assets you provide ONCE, committed to the repo under assets/:
    assets/intro.mp4          your branded intro (any source dimensions --
                               fitted into the frame, NEVER cropped)
    assets/outro.mp4          your branded outro (same treatment)
    assets/ambient_news.mp3   a looping ambient news background bed, mixed
                               under the narration at reduced volume

If any of these files are missing, that piece is skipped with a clear
warning rather than crashing the whole render -- so this becomes fully
functional the moment you add each file, no code changes needed.

Design choices, stated plainly:
- Content B-roll (Pexels/Pixabay/Wikimedia assets) is scaled to FILL the
  1080x1920 frame -- standard reel look, minor edge cropping is expected
  and fine for generic footage.
- intro.mp4 / outro.mp4 are scaled to FIT inside the frame with padding
  (letterboxed) -- your branding must never be cropped, whatever its
  native aspect ratio.
- Subtitle timing is approximated by allocating each sentence a share of
  the ACTUAL measured narration duration, proportional to its character
  count. This is a heuristic, not word-level timestamp sync -- stated so
  the limitation is explicit, not hidden.
- The visual asset timeline (built in Stage 2 against a 30s *target*) is
  rescaled at render time to match the TTS engine's *actual* measured
  audio_duration_sec, so visuals and narration always end together
  regardless of small drift between the plan and the real narration length.

Requires ffmpeg + ffprobe on PATH (preinstalled on GitHub Actions
ubuntu-latest runners).

Input:  posts_with_audio.json -- only posts with review_status == "approved"
        AND a valid audio_path are rendered.
Output: videos/<topic_label>.mp4 per rendered post, plus posts_with_video.json.
"""

import json
import re
import subprocess
import shutil
from pathlib import Path

TARGET_W, TARGET_H = 1080, 1920
FPS = 30

ASSETS_DIR = Path("assets")
INTRO_PATH = ASSETS_DIR / "intro.mp4"
OUTRO_PATH = ASSETS_DIR / "outro.mp4"
AMBIENT_PATH = ASSETS_DIR / "ambient_news.mp3"
AMBIENT_VOLUME = 0.15  # relative volume of ambient bed under narration (0-1)

WORK_DIR = Path("render_work")
VIDEOS_DIR = Path("videos")


def run(cmd, description):
    """Run an ffmpeg/ffprobe command, raising with full stderr on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"FAILED: {description}\nCMD: {' '.join(cmd)}\nSTDERR:\n{result.stderr[-3000:]}"
        )
    return result


def get_duration(path):
    result = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        f"probe duration of {path}",
    )
    return float(result.stdout.strip())


def safe_filename(label):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:60]


# ---------- Normalizing individual assets ----------

def normalize_fill(input_path, output_path, duration, is_image):
    """Scale+crop to FILL the frame exactly -- used for content B-roll."""
    vf = (
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_W}:{TARGET_H},fps={FPS},format=yuv420p"
    )
    if is_image:
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-t", str(duration), "-i", str(input_path),
            "-vf", vf, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path), "-t", str(duration),
            "-vf", vf, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(output_path),
        ]
    run(cmd, f"normalize-fill {input_path} -> {output_path}")


def normalize_fit_branding(input_path, output_path):
    """Scale+pad to FIT inside the frame -- used for intro/outro. Never crops.
    Keeps the clip's own native audio untouched."""
    vf = (
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
        f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={FPS},format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2",
        str(output_path),
    ]
    run(cmd, f"normalize-fit(branding) {input_path} -> {output_path}")


# ---------- Content segment assembly ----------

def build_content_video(post, work_dir):
    """Builds the silent, subtitle-free content visual timeline, rescaled
    so its total length matches the ACTUAL measured narration duration."""
    assets = post.get("assets", [])
    if not assets:
        raise RuntimeError("no assets to render")

    planned_total = sum(a["duration_sec"] for a in assets)
    actual_duration = post["audio_duration_sec"]
    scale_factor = actual_duration / planned_total if planned_total > 0 else 1.0

    segment_paths = []
    for i, asset in enumerate(assets):
        target_dur = max(0.5, asset["duration_sec"] * scale_factor)
        seg_path = work_dir / f"seg_{i}.mp4"

        if asset["type"] == "text_card":
            safe_text = asset["text"].replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
            card_ass = work_dir / f"textcard_{i}.ass"
            card_ass.write_text(
                f"[Script Info]\nScriptType: v4.00+\nPlayResX: {TARGET_W}\nPlayResY: {TARGET_H}\n\n"
                f"[V4+ Styles]\n"
                f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                f"Style: Default,Noto Sans Devanagari,64,&HFFFFFF,&HFFFFFF,&H000000,&H000000,0,0,0,0,100,100,0,0,1,0,0,5,10,10,10,1\n\n"
                f"[Events]\n"
                f"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                f"Dialogue: 0,0:00:00.00,{format_ass_time(target_dur)},Default,,0,0,0,,{safe_text}\n",
                encoding="utf-8",
            )
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"color=c=black:s={TARGET_W}x{TARGET_H}:d={target_dur}:r={FPS}",
                "-vf", f"ass={card_ass}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(seg_path),
            ]
            run(cmd, f"render text_card segment {i}")
        elif asset["type"] == "image":
            normalize_fill(asset["url"], seg_path, target_dur, is_image=True)
        elif asset["type"] == "video":
            normalize_fill(asset["url"], seg_path, target_dur, is_image=False)
        else:
            continue

        segment_paths.append(seg_path)

    return segment_paths


def concat_segments(segment_paths, output_path, work_dir):
    concat_list = work_dir / "concat_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in segment_paths:
            f.write(f"file '{p.resolve()}'\n")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(output_path),
    ]
    run(cmd, "concat content segments")


# ---------- Subtitles ----------

MAX_SUBTITLE_CHARS = 28  # keep each on-screen chunk short -- never the whole sentence

# Layout for the translucent bottom bar the subtitle text sits in.
BAR_H = 200
BAR_BOTTOM_MARGIN = 230  # cleared platform UI (like/comment/share buttons, captions) that sits in the bottom safe zone
BAR_Y = TARGET_H - BAR_H - BAR_BOTTOM_MARGIN
TEXT_MARGIN_V = BAR_BOTTOM_MARGIN + (BAR_H - 90) // 2


def chunk_by_words(text, max_chars=MAX_SUBTITLE_CHARS):
    """
    Splits text into chunks up to max_chars, breaking ONLY on whitespace so
    words are never cut mid-way. A single word longer than max_chars is kept
    whole rather than broken.
    """
    words = text.split()
    chunks = []
    current = ""
    for w in words:
        candidate = (current + " " + w).strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            chunks.append(current)
            current = w
    if current:
        chunks.append(current)
    return chunks


def format_ass_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds - int(seconds)) * 100)  # ASS uses centiseconds
    return f"{h:01}:{m:02}:{s:02}.{cs:02}"


ASS_HEADER_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans Devanagari,{fontsize},&HFFFFFF,&HFFFFFF,&H000000,&H000000,0,0,0,0,100,100,0,0,1,{outline},0,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def generate_subtitle_ass(post, ass_path):
    """
    Generates a NATIVE .ass file (not SRT) -- this matters: ffmpeg's
    `subtitles` filter internally converts SRT to ASS before rendering, and
    that conversion path was found to break Devanagari conjunct formation
    (म्न rendering as broken half-forms instead of a proper ligature).
    Feeding a real .ass file to the `ass` filter directly avoids that
    conversion step and renders conjuncts correctly -- verified directly
    before rolling this out.
    """
    chunks = chunk_by_words(post["script_hi"])
    total_chars = sum(len(c) for c in chunks) or 1
    duration = post["audio_duration_sec"]

    header = ASS_HEADER_TEMPLATE.format(
        width=TARGET_W, height=TARGET_H, fontsize=68, outline=2, margin_v=TEXT_MARGIN_V
    )

    lines = [header]
    t = 0.0
    for chunk in chunks:
        share = len(chunk) / total_chars
        dur = max(0.8, duration * share)
        start, end = t, min(t + dur, duration)
        # Escape ASS special characters in the text itself.
        safe_text = chunk.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        lines.append(
            f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,{safe_text}"
        )
        t = end

    ass_path.write_text("\n".join(lines), encoding="utf-8")


def burn_subtitles(video_path, ass_path, output_path):
    # Full-width translucent bar drawn first, subtitle text (via the `ass`
    # filter, which correctly shapes Devanagari conjuncts) rendered on top.
    vf = (
        f"drawbox=x=0:y={BAR_Y}:w={TARGET_W}:h={BAR_H}:color=black@0.55:t=fill,"
        f"ass={ass_path}"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    run(cmd, f"burn subtitles onto {video_path}")


# ---------- Audio: narration + ambient mix ----------
# NOTE: every audio output below is forced to the SAME format (AAC, 44100Hz,
# stereo) as normalize_fit_branding() uses for intro/outro. This matters:
# the final concat step uses stream-copy ("-c copy"), which can silently
# drop/mute audio on segments whose channel count or sample rate doesn't
# exactly match the others. A channel mismatch here was the actual cause of
# the outro audio going silent.

def build_mixed_audio(narration_path, duration, output_path):
    has_ambient = AMBIENT_PATH.exists()
    if has_ambient:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(narration_path),
            "-stream_loop", "-1", "-i", str(AMBIENT_PATH),
            "-filter_complex",
            (
                f"[1:a]volume={AMBIENT_VOLUME}[amb];"
                f"[0:a][amb]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            ),
            "-map", "[aout]", "-t", str(duration),
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            str(output_path),
        ]
        run(cmd, "mix narration + ambient bed")
    else:
        print("    [warn] assets/ambient_news.mp3 not found -- using narration only (no ambient bed)")
        cmd = [
            "ffmpeg", "-y", "-i", str(narration_path), "-t", str(duration),
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            str(output_path),
        ]
        run(cmd, "transcode narration-only audio to AAC")


def mux_video_audio(video_path, audio_path, output_path):
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path),
        "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest",
        str(output_path),
    ]
    run(cmd, "mux video + audio")


# ---------- Final assembly: intro + content + outro ----------

def assemble_final(content_path, output_path, work_dir):
    pieces = []

    if INTRO_PATH.exists():
        intro_norm = work_dir / "intro_norm.mp4"
        normalize_fit_branding(INTRO_PATH, intro_norm)
        pieces.append(intro_norm)
    else:
        print("    [warn] assets/intro.mp4 not found -- skipping intro")

    pieces.append(content_path)

    if OUTRO_PATH.exists():
        outro_norm = work_dir / "outro_norm.mp4"
        normalize_fit_branding(OUTRO_PATH, outro_norm)
        pieces.append(outro_norm)
    else:
        print("    [warn] assets/outro.mp4 not found -- skipping outro")

    if len(pieces) == 1:
        shutil.copy(pieces[0], output_path)
        return

    concat_list = work_dir / "final_concat_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in pieces:
            f.write(f"file '{Path(p).resolve()}'\n")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(output_path),
    ]
    run(cmd, "concat intro + content + outro")


# ---------- Per-post orchestration ----------

def render_post(post):
    label = safe_filename(post["topic_label"])
    work_dir = WORK_DIR / label
    work_dir.mkdir(parents=True, exist_ok=True)

    print("  Building content visual timeline...")
    segments = build_content_video(post, work_dir)
    silent_content = work_dir / "content_silent.mp4"
    concat_segments(segments, silent_content, work_dir)

    print("  Generating + burning subtitles...")
    ass_path = work_dir / "subs.ass"
    generate_subtitle_ass(post, ass_path)
    content_with_subs = work_dir / "content_subs.mp4"
    burn_subtitles(silent_content, ass_path, content_with_subs)

    print("  Mixing narration + ambient audio...")
    mixed_audio = work_dir / "mixed_audio.aac"
    build_mixed_audio(post["audio_path"], post["audio_duration_sec"], mixed_audio)

    print("  Muxing video + audio...")
    content_final = work_dir / "content_final.mp4"
    mux_video_audio(content_with_subs, mixed_audio, content_final)

    print("  Assembling intro + content + outro...")
    VIDEOS_DIR.mkdir(exist_ok=True)
    final_output = VIDEOS_DIR / f"{label}.mp4"
    assemble_final(content_final, final_output, work_dir)

    return str(final_output), get_duration(final_output)


def main():
    import os as _os
    for stale_file in ("stage4_error.txt", "stage4_per_post_errors.txt"):
        if _os.path.exists(stale_file):
            _os.remove(stale_file)
            print(f"Cleared stale {stale_file} from a previous run.")

    posts = json.loads(Path("posts_with_audio.json").read_text(encoding="utf-8"))
    ready = [p for p in posts if p.get("review_status") == "approved" and p.get("audio_path")]
    print(f"{len(ready)} of {len(posts)} posts have approved status + audio -- rendering those.")

    per_post_errors = []
    debug_trace = []
    ready_labels = {p["topic_label"] for p in ready}
    for i, post in enumerate(posts):
        if post["topic_label"] not in ready_labels:
            debug_trace.append(f"{post['topic_label']}: SKIPPED (not in ready set)")
            continue
        print(f"Rendering {i+1}: {post['topic_label']}...")
        try:
            video_path, duration = render_post(post)
            post["video_path"] = video_path
            post["video_duration_sec"] = round(duration, 2)
            print(f"  -> {video_path} ({duration:.1f}s)")
            debug_trace.append(f"{post['topic_label']}: SUCCESS -> video_path={video_path} dur={duration}")
        except Exception as e:
            import traceback as tb
            full_trace = tb.format_exc()
            print(f"  [error] render failed for {post['topic_label']}: {e}")
            per_post_errors.append(f"=== {post['topic_label']} ===\n{full_trace}\n")
            post["video_path"] = None
            post["review_status"] = "pending_review"
            debug_trace.append(f"{post['topic_label']}: EXCEPTION -> {e}")

    if per_post_errors:
        Path("stage4_per_post_errors.txt").write_text("\n".join(per_post_errors), encoding="utf-8")
        print(f"Wrote stage4_per_post_errors.txt ({len(per_post_errors)} failures)")

    # Unconditional trace -- written every run regardless of outcome, so we
    # can see exactly what happened to each post even when no exception
    # was raised (e.g. a silent logic issue rather than a crash).
    Path("stage4_debug_trace.txt").write_text(
        f"ready_labels: {sorted(ready_labels)}\n\n" + "\n".join(debug_trace),
        encoding="utf-8",
    )

    out_path = Path("posts_with_video.json")
    out_path.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    return posts


if __name__ == "__main__":
    import sys
    import traceback
    try:
        main()
    except Exception:
        with open("stage4_error.txt", "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        print("Wrote full traceback to stage4_error.txt")
        sys.exit(1)
