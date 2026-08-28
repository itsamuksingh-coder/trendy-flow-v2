"""
Fast isolated test harness for Stage 5 -- builds 3 identical tiny test
posts_with_video.json entries so the existing round-robin publisher
naturally sends one to YouTube, one to Facebook, one to Instagram, without
waiting through the full fetch/script/TTS/render pipeline.

This is NOT part of the production pipeline -- debugging tool only.
"""

import json
from pathlib import Path

TEST_VIDEO = "test_assets/test_publish_check.mp4"

test_posts = []
for i, label in enumerate(["test_publish_youtube", "test_publish_facebook", "test_publish_instagram"]):
    test_posts.append({
        "topic_label": label,
        "title": "SSD News Test Post",
        "caption": "This is a test post to verify the publishing pipeline. Will be deleted shortly. #test",
        "hashtags": ["test"],
        "review_status": "approved",
        "video_path": TEST_VIDEO,
        "video_duration_sec": 5.0,
    })

Path("posts_with_video.json").write_text(
    json.dumps(test_posts, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"Wrote {len(test_posts)} test posts, one per platform via round-robin.")
