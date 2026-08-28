"""
Fast isolated test harness for Stage 4 -- builds a synthetic single-post
posts_with_audio.json using LOCALLY GENERATED test images (not external
URLs) and synthetic tone audio, so this test has zero external
dependencies and can't fail due to a bad/unverified image URL. This lets
us debug the renderer in ~1-2 minutes instead of waiting ~40 minutes for
real TTS, with a fully reproducible, self-contained test.

This is NOT part of the production pipeline -- it's a debugging tool only.
"""

import json
import subprocess
from pathlib import Path

Path("audio").mkdir(exist_ok=True)
Path("test_images").mkdir(exist_ok=True)
fake_audio_path = "audio/test_post.wav"

# Generate 10 seconds of a simple tone as a stand-in for narration audio --
# we're testing render mechanics (concat, subtitles, mixing, intro/outro),
# not voice quality.
subprocess.run([
    "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
    "-ar", "44100", fake_audio_path,
], check=True)

# Two distinct solid-color local images -- zero network dependency, so this
# test can never fail because of an external URL being wrong or unreachable.
subprocess.run([
    "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=800x600:d=1",
    "-frames:v", "1", "test_images/img1.jpg",
], check=True)
subprocess.run([
    "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1200x800:d=1",
    "-frames:v", "1", "test_images/img2.jpg",
], check=True)

test_post = {
    "topic_label": "test_post",
    "region": "india",
    "headline": "Test headline",
    "script_hi": "यह एक परीक्षण वीडियो है। हम रेंडरर की जांच कर रहे हैं। यह तीसरा वाक्य है।",
    "tts_text_hi": "यह एक परीक्षण वीडियो है। हम रेंडरर की जांच कर रहे हैं। यह तीसरा वाक्य है।",
    "title": "Test Video",
    "caption": "Test caption",
    "hashtags": ["test"],
    "named_person": None,
    "flag_for_manual_review": False,
    "review_status": "approved",
    "audio_path": fake_audio_path,
    "audio_duration_sec": 10.0,
    "assets": [
        {
            "type": "image",
            "url": "test_images/img1.jpg",
            "source": "local_test",
            "query": "test image 1 (blue)",
            "duration_sec": 5.0,
        },
        {
            "type": "image",
            "url": "test_images/img2.jpg",
            "source": "local_test",
            "query": "test image 2 (red)",
            "duration_sec": 5.0,
        },
    ],
}

Path("posts_with_audio.json").write_text(
    json.dumps([test_post], ensure_ascii=False, indent=2), encoding="utf-8"
)
print("Synthetic posts_with_audio.json written (fully local, no external URLs).")
