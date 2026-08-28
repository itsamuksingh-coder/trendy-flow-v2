"""
Stage 5 -- Publish rendered videos to YouTube, Facebook, and Instagram in a
round-robin loop: YouTube -> Facebook -> Instagram -> YouTube -> ... with a
human-like delay between each post, rather than posting everything to one
platform back-to-back.

Why round-robin instead of "all YouTube, then all Facebook, then all
Instagram": posting many videos to the same platform in a tight burst is a
much stronger bot signal than spacing platforms out -- rotating platforms
with real gaps between actions looks like a person context-switching
between apps, which is exactly what's actually happening operationally.

Env vars required (GitHub Actions secrets), added when each token exists:
  YOUTUBE_ACCESS_TOKEN   -- OAuth token with youtube.upload scope
  FB_PAGE_ACCESS_TOKEN   -- long-lived Page token with pages_manage_posts
  IG_ACCESS_TOKEN        -- long-lived token with instagram_business_content_publish
  IG_BUSINESS_ACCOUNT_ID -- the Instagram Business Account ID to publish to
  FB_PAGE_ID             -- the Facebook Page ID to publish to

If a platform's token isn't set yet, that platform is skipped gracefully
(logged, not fatal) -- so this can run correctly with only 1 or 2 of the 3
platforms configured while the others are still being set up.

Timing (configurable below):
  MIN_INTERVAL_SEC / MAX_INTERVAL_SEC -- randomized human-like gap between
    each individual post (any platform)
  UPLOAD_FAILSAFE_SEC -- minimum buffer reserved for asset upload time on
    top of the interval, so a slow upload doesn't compress the gap to zero

Input:  posts_with_video.json -- only posts with review_status == "approved"
        AND a valid video_path are published.
Output: posts_published.json recording per-post publish results, and
        posted_topics.json updated with real posted_at timestamps (this is
        the ONLY place posted_topics.json should be written from real
        publishing, as opposed to the manual testing seeds used earlier).
"""

import json
import os
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
IG_BUSINESS_ACCOUNT_ID = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "")
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")

# Upload-Post: a third-party publisher with its own already-approved Meta
# app. You connect your Instagram/Facebook via normal login on their
# dashboard -- no Meta developer app, no app review, no OTP verification
# on your end at all. Free tier: 10 uploads/month, no card required. Sign
# up at upload-post.com, connect your accounts there, then paste the API
# key here. Used automatically as a fallback for any platform whose native
# token isn't set yet.
# Zernio: free tier covers the first 2 connected accounts with UNLIMITED
# posts and full API access, no card -- a better fit than Upload-Post's
# 10/month cap for exactly the Instagram+Facebook pair needed here.
# Connect accounts via normal login on their dashboard -- no Meta developer
# app, no OTP, no device-trust wall, since you're never touching Meta's
# developer side at all. Sign up at zernio.com, connect Instagram +
# Facebook there, get an API key and each account's ID.
ZERNIO_API_KEY = os.environ.get("ZERNIO_API_KEY", "")
ZERNIO_INSTAGRAM_ACCOUNT_ID = os.environ.get("ZERNIO_INSTAGRAM_ACCOUNT_ID", "")
ZERNIO_FACEBOOK_ACCOUNT_ID = os.environ.get("ZERNIO_FACEBOOK_ACCOUNT_ID", "")

UPLOAD_POST_API_KEY = os.environ.get("UPLOAD_POST_API_KEY", "")
UPLOAD_POST_USER = os.environ.get("UPLOAD_POST_USER", "")  # the "profile" name you set in their dashboard

MIN_INTERVAL_SEC = 120   # 2 minutes
MAX_INTERVAL_SEC = 300   # 5 minutes
UPLOAD_FAILSAFE_SEC = 60  # extra buffer reserved for slow uploads

HISTORY_FILE = Path("posted_topics.json")
DAILY_COUNT_FILE = Path("daily_post_count.json")
IST = timezone(timedelta(hours=5, minutes=30))


def increment_daily_count():
    """Increments today's (IST) published-post counter -- read by Stage 0
    on the NEXT run to know how much of the 50/day budget is left."""
    today_ist = datetime.now(IST).date().isoformat()
    if DAILY_COUNT_FILE.exists():
        try:
            data = json.loads(DAILY_COUNT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    if data.get("date") != today_ist:
        data = {"date": today_ist, "count": 0}
    data["count"] = data.get("count", 0) + 1
    DAILY_COUNT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Daily count now {data['count']}/50 for {today_ist} IST.")


# ---------- Platform publishers ----------
# Each returns (success: bool, info: dict) and never raises -- publish
# failures are data, not exceptions, so one platform failing doesn't take
# down the whole loop.

def get_youtube_access_token():
    """
    Exchanges the long-lived refresh token for a fresh short-lived access
    token. Done at the start of each publish attempt since access tokens
    expire in ~1 hour -- the refresh token is what actually needs to stay
    valid long-term (note: while the Google Cloud app is in "Testing"
    status, the refresh token itself expires after 7 days and needs
    re-authorization; submitting for Production verification removes that).
    """
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": YOUTUBE_CLIENT_ID,
                "client_secret": YOUTUBE_CLIENT_SECRET,
                "refresh_token": YOUTUBE_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as e:
        print(f"    [error] failed to refresh YouTube access token: {e}")
        return None


def publish_youtube(post):
    if not (YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN):
        return False, {"skipped": "YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN not set"}
    access_token = get_youtube_access_token()
    if not access_token:
        return False, {"error": "could not obtain access token from refresh token"}
    try:
        yt_copy = (post.get("platform_copy") or {}).get("youtube", {})
        title = yt_copy.get("title") or post.get("title", "")
        description = yt_copy.get("caption") or post.get("caption", "")
        hashtags = (post.get("platform_copy") or {}).get("hashtags") or post.get("hashtags", [])
        metadata = {
            "snippet": {
                "title": title[:100],
                "description": description,
                "tags": hashtags,
                "categoryId": "25",  # News & Politics
            },
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }
        video_path = post["video_path"]
        video_size = os.path.getsize(video_path)

        init_resp = requests.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(video_size),
            },
            json=metadata,
            timeout=30,
        )
        init_resp.raise_for_status()
        upload_url = init_resp.headers["Location"]

        with open(video_path, "rb") as f:
            upload_resp = requests.put(
                upload_url,
                headers={"Content-Type": "video/mp4"},
                data=f,
                timeout=300,
            )
        upload_resp.raise_for_status()
        result = upload_resp.json()
        return True, {"video_id": result.get("id"), "url": f"https://youtube.com/shorts/{result.get('id')}"}
    except Exception as e:
        return False, {"error": str(e)}


def publish_facebook(post):
    if FB_PAGE_ACCESS_TOKEN and FB_PAGE_ID:
        return _publish_facebook_native(post)
    if ZERNIO_API_KEY and ZERNIO_FACEBOOK_ACCOUNT_ID:
        return publish_via_zernio(post, "facebook", ZERNIO_FACEBOOK_ACCOUNT_ID)
    if UPLOAD_POST_API_KEY:
        return publish_via_upload_post(post, "facebook")
    return False, {"skipped": "no FB token, ZERNIO, or UPLOAD_POST_API_KEY set"}


def get_caption_for(post, platform):
    """Pulls the platform-specific copywritten caption if available, else
    falls back to the generic caption field."""
    platform_copy = (post.get("platform_copy") or {}).get(platform, {})
    return platform_copy.get("caption") or post.get("caption", "")


def _publish_facebook_native(post):
    try:
        with open(post["video_path"], "rb") as f:
            resp = requests.post(
                f"https://graph.facebook.com/v21.0/{FB_PAGE_ID}/videos",
                params={"access_token": FB_PAGE_ACCESS_TOKEN},
                data={
                    "description": get_caption_for(post, "facebook"),
                },
                files={"source": f},
                timeout=300,
            )
        resp.raise_for_status()
        result = resp.json()
        return True, {"post_id": result.get("id")}
    except Exception as e:
        return False, {"error": str(e)}


def upload_to_zernio_media(video_path):
    """
    Uploads a local video file to Zernio's own hosting via their presigned
    URL flow -- this is the permanent solution for the "needs a public URL"
    requirement: Zernio hosts the file themselves, no external public
    repo/bucket needed on our end at all.
    """
    filename = os.path.basename(video_path)
    presign_resp = requests.post(
        "https://zernio.com/api/v1/media/presign",
        headers={"Authorization": f"Bearer {ZERNIO_API_KEY}", "Content-Type": "application/json"},
        json={"filename": filename, "contentType": "video/mp4"},
        timeout=30,
    )
    presign_resp.raise_for_status()
    presign_data = presign_resp.json()
    upload_url = presign_data["uploadUrl"]
    public_url = presign_data["publicUrl"]

    with open(video_path, "rb") as f:
        put_resp = requests.put(upload_url, data=f, headers={"Content-Type": "video/mp4"}, timeout=300)
    put_resp.raise_for_status()

    return public_url


def publish_via_zernio(post, platform, account_id):
    """
    Publishes through Zernio -- free for the first 2 connected accounts,
    unlimited posts, no card. Their platform names are lowercase
    ("instagram", "facebook").
    """
    if not ZERNIO_API_KEY or not account_id:
        return False, {"skipped": "ZERNIO_API_KEY or account ID not set"}
    try:
        media_url = upload_to_zernio_media(post["video_path"])
        resp = requests.post(
            "https://zernio.com/api/v1/posts",
            headers={"Authorization": f"Bearer {ZERNIO_API_KEY}", "Content-Type": "application/json"},
            json={
                "content": get_caption_for(post, platform),
                "mediaItems": [{"type": "video", "url": media_url}],
                "platforms": [{"platform": platform, "accountId": account_id}],
                "publishNow": True,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return True, {"result": resp.json()}
    except Exception as e:
        return False, {"error": str(e)}


def publish_instagram(post):
    if IG_ACCESS_TOKEN and IG_BUSINESS_ACCOUNT_ID:
        return _publish_instagram_native(post)
    if ZERNIO_API_KEY and ZERNIO_INSTAGRAM_ACCOUNT_ID:
        return publish_via_zernio(post, "instagram", ZERNIO_INSTAGRAM_ACCOUNT_ID)
    if UPLOAD_POST_API_KEY:
        return publish_via_upload_post(post, "instagram")
    return False, {"skipped": "no IG token, ZERNIO, or UPLOAD_POST_API_KEY set"}


def _publish_instagram_native(post):
    try:
        # Instagram's Content Publishing API needs a publicly reachable URL
        # for the video file, not a local path or direct upload -- video_url
        # must be set by an earlier step (e.g. a temporary hosted copy).
        video_url = post.get("public_video_url")
        if not video_url:
            return False, {"error": "no public_video_url set -- Instagram requires a hosted URL, not a local file"}

        container_resp = requests.post(
            f"https://graph.facebook.com/v21.0/{IG_BUSINESS_ACCOUNT_ID}/media",
            params={
                "access_token": IG_ACCESS_TOKEN,
                "media_type": "REELS",
                "video_url": video_url,
                "caption": get_caption_for(post, "instagram"),
            },
            timeout=30,
        )
        container_resp.raise_for_status()
        container_id = container_resp.json()["id"]

        # Poll until the container finishes processing before publishing.
        for _ in range(30):
            status_resp = requests.get(
                f"https://graph.facebook.com/v21.0/{container_id}",
                params={"access_token": IG_ACCESS_TOKEN, "fields": "status_code"},
                timeout=15,
            )
            status = status_resp.json().get("status_code")
            if status == "FINISHED":
                break
            if status == "ERROR":
                return False, {"error": "Instagram container processing failed"}
            time.sleep(10)

        publish_resp = requests.post(
            f"https://graph.facebook.com/v21.0/{IG_BUSINESS_ACCOUNT_ID}/media_publish",
            params={"access_token": IG_ACCESS_TOKEN, "creation_id": container_id},
            timeout=30,
        )
        publish_resp.raise_for_status()
        result = publish_resp.json()
        return True, {"media_id": result.get("id")}
    except Exception as e:
        return False, {"error": str(e)}


def publish_via_upload_post(post, platform):
    """
    Publishes through Upload-Post's API -- their already-approved Meta app
    handles Instagram/Facebook, so this works without you ever creating a
    Meta developer app or hitting the OTP verification bug. Free tier:
    10 uploads/month, no card. Sign up at upload-post.com, connect your
    account(s) in their dashboard under a "profile" name, get an API key.
    """
    if not UPLOAD_POST_API_KEY or not UPLOAD_POST_USER:
        return False, {"skipped": "UPLOAD_POST_API_KEY or UPLOAD_POST_USER not set"}
    try:
        with open(post["video_path"], "rb") as f:
            resp = requests.post(
                "https://api.upload-post.com/api/upload",
                headers={"Authorization": f"ApiKey {UPLOAD_POST_API_KEY}"},
                data={
                    "user": UPLOAD_POST_USER,
                    "platform[]": platform,
                    "title": post.get("title", "")[:100],
                    "caption": get_caption_for(post, platform),
                },
                files={"video": f},
                timeout=300,
            )
        resp.raise_for_status()
        return True, {"result": resp.json()}
    except Exception as e:
        return False, {"error": str(e)}


PLATFORM_ORDER = [
    ("youtube", publish_youtube),
    ("facebook", publish_facebook),
    ("instagram", publish_instagram),
]


# ---------- History ----------

def mark_posted(topic_label):
    history = []
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    history.append({
        "topic_label": topic_label,
        "posted_at": datetime.now(timezone.utc).isoformat(),
    })
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Orchestration ----------

def check_zernio_usage():
    """
    Logs current Zernio usage/plan status for visibility -- there's no
    manual delete endpoint for presigned media uploads (only the built-in
    7-day auto-expiry), so this is a monitoring safety net rather than
    active cleanup: if usage ever approaches a real limit, it shows up
    here in the logs instead of surfacing as a failed post.
    """
    if not ZERNIO_API_KEY:
        return
    try:
        resp = requests.get(
            "https://zernio.com/api/v1/usage-stats",
            headers={"Authorization": f"Bearer {ZERNIO_API_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        print(f"Zernio usage/plan status: {resp.json()}")
    except Exception as e:
        print(f"[warn] could not fetch Zernio usage stats: {e}")


def main():
    check_zernio_usage()
    posts = json.loads(Path("posts_with_video.json").read_text(encoding="utf-8"))
    ready = [p for p in posts if p.get("review_status") == "approved" and p.get("video_path")]
    print(f"{len(ready)} of {len(posts)} posts are approved + rendered -- publishing those to ALL platforms.")

    if not ready:
        print("Nothing ready to publish this run.")
        Path("posts_published.json").write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
        return posts

    ready_labels = {p["topic_label"] for p in ready}
    ready_index = 0
    for post in posts:
        if post["topic_label"] not in ready_labels:
            continue
        ready_index += 1
        print(f"Publishing {post['topic_label']} to all platforms ({ready_index}/{len(ready)})...")

        any_success = False
        for platform_name, publish_fn in PLATFORM_ORDER:
            print(f"  -> {platform_name}...")
            success, info = publish_fn(post)
            post.setdefault("publish_results", {})[platform_name] = {
                "success": success,
                **info,
                "attempted_at": datetime.now(timezone.utc).isoformat(),
            }
            if success:
                print(f"     success: {info}")
                any_success = True
            elif "skipped" in info:
                print(f"     skipped: {info['skipped']}")
            else:
                print(f"     FAILED: {info.get('error')}")

            # Short gap between platforms for the SAME video -- this is the
            # same content going to different apps, not the repeated-content
            # bot pattern the longer between-post gap guards against.
            if platform_name != PLATFORM_ORDER[-1][0]:
                time.sleep(random.randint(15, 30))

        if any_success:
            mark_posted(post["topic_label"])
            increment_daily_count()

        # Longer human-like gap before the NEXT distinct video, plus upload
        # failsafe buffer. Skip the wait after the last ready post.
        if ready_index < len(ready):
            wait = random.randint(MIN_INTERVAL_SEC, MAX_INTERVAL_SEC) + UPLOAD_FAILSAFE_SEC
            print(f"  Waiting {wait}s before next video...")
            time.sleep(wait)

    out_path = Path("posts_published.json")
    out_path.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    return posts


if __name__ == "__main__":
    import sys
    import traceback
    try:
        main()
    except Exception:
        with open("stage5_error.txt", "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        print("Wrote full traceback to stage5_error.txt")
        sys.exit(1)
