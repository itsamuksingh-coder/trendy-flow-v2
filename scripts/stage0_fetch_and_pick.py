"""
Stage 0 — Fetch news + pick 5-10 topics worth turning into posts.

Pipeline position: FIRST step. Triggered by GitHub Actions workflow_dispatch
(one tap from your phone). Output feeds Stage 1 (Gemini script generation).

Sources: official public RSS feeds published directly by major Hindi news
channels/outlets for syndication. This is what RSS is built for -- it is
not scraping, no ToS is being bypassed, and no API key/signup is needed
for the feeds themselves.

Env vars required (set as GitHub Actions secrets):
  GEMINI_API_KEY      - your existing Gemini API key

Persistent state:
  posted_topics.json  - committed back to the repo after each run. This is
                         your dedup memory. Keep it in the repo, not in a
                         temp runner filesystem, or every run starts blind.
"""

import os
import json
import re
import time
import hashlib
import base64
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
import xml.etree.ElementTree as ET

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

DAILY_POST_CAP = 50
DAILY_COUNT_FILE = Path("daily_post_count.json")
IST = timezone(timedelta(hours=5, minutes=30))


def get_remaining_daily_budget():
    """
    Reads today's (IST calendar day) published-post count and returns how
    many more posts can be attempted before hitting the 50/day cap. Resets
    automatically when the IST date rolls over. This is checked BEFORE
    generating/rendering anything, so we don't waste TTS/render/API cost on
    posts that would just be discarded for exceeding the cap anyway.
    """
    today_ist = datetime.now(IST).date().isoformat()
    if DAILY_COUNT_FILE.exists():
        try:
            data = json.loads(DAILY_COUNT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    if data.get("date") != today_ist:
        # New IST day -- reset the counter.
        data = {"date": today_ist, "count": 0}

    remaining = max(0, DAILY_POST_CAP - data.get("count", 0))
    print(f"Daily post cap: {data.get('count', 0)}/{DAILY_POST_CAP} used today ({today_ist} IST) -- {remaining} remaining.")
    return remaining


PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
AUTO_APPROVE = os.environ.get("AUTO_APPROVE", "false").strip().lower() == "true"
TOPIC_LIMIT = os.environ.get("TOPIC_LIMIT", "").strip()  # if set, forces min=max=this value

TARGET_DURATION_SEC = 30        # target total video length
IMAGE_MIN_DURATION = 3          # seconds an image is shown for, minimum
IMAGE_MAX_DURATION = 5          # seconds an image is shown for, maximum
MIN_IMAGES_IF_NO_VIDEO = 5      # floor when filling the timeline with images only
MAX_IMAGES_IF_NO_VIDEO = 10     # ceiling when filling the timeline with images only
MAX_SINGLE_ASSET_DURATION = 6   # no single clip may occupy more than this --
                                 # forces visual variety instead of one long clip

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")  # auto-set by Actions, "owner/repo"


def create_review_issue(post):
    """
    Creates a GitHub Issue for a pending_review post -- this is the minimum
    review UI: readable on the GitHub mobile app, with a label to approve.
    Nothing gets silently discarded; every held-back post is visible and
    actionable, not just stuck in a JSON file.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    reason = post.get("flag_reason") or "Held back (coverage/asset check), not a content flag"
    body = (
        f"**Title:** {post.get('title','')}\n\n"
        f"**Hook:** {post.get('hook','')}\n\n"
        f"**Reason held back:** {reason}\n\n"
        f"**Headline:** {post.get('headline','')}\n\n"
        f"**Script:**\n{post.get('script_hi','')}\n\n"
        f"---\n"
        f"To approve and let this run through on the next pipeline run, "
        f"add the `approved` label to this issue. To discard it "
        f"permanently, just close the issue without that label.\n\n"
        f"<details><summary>Full post data (do not edit)</summary>\n\n"
        f"```json\n{json.dumps(post, ensure_ascii=False, indent=2)}\n```\n"
        f"</details>"
    )
    try:
        resp = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={
                "title": f"[REVIEW] {post.get('topic_label','untitled')}",
                "body": body,
                "labels": ["pending-review"],
            },
            timeout=15,
        )
        resp.raise_for_status()
        print(f"    Created review issue: {resp.json().get('html_url')}")
    except requests.RequestException as e:
        print(f"    [warn] failed to create review issue: {e}")


def fetch_approved_review_issues():
    """
    Checks for issues labeled BOTH 'pending-review' and 'approved' -- these
    are posts you've reviewed and greenlit. Returns their full post data
    (stored in the issue body) and closes each issue so it isn't picked up
    again on a future run.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return []
    approved_posts = []
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            params={"labels": "pending-review,approved", "state": "open", "per_page": 20},
            timeout=15,
        )
        resp.raise_for_status()
        for issue in resp.json():
            match = re.search(r"```json\n(.*?)\n```", issue["body"], re.DOTALL)
            if not match:
                continue
            try:
                post = json.loads(match.group(1))
                post["review_status"] = "approved"
                approved_posts.append(post)
                requests.patch(
                    issue["url"],
                    headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
                    json={"state": "closed", "labels": ["approved", "processed"]},
                    timeout=15,
                )
                print(f"  Resuming approved post from issue #{issue['number']}: {post.get('topic_label')}")
            except (json.JSONDecodeError, KeyError) as e:
                print(f"    [warn] couldn't parse post data from issue #{issue['number']}: {e}")
    except requests.RequestException as e:
        print(f"  [warn] failed to fetch approved review issues: {e}")
    return approved_posts

# Official public RSS feeds. Mix of Hindi and English sources -- language of
# the source article doesn't matter, since Stage 1's Gemini call translates
# and rewrites into Hindi narration regardless. "region" tags let the ranking
# prompt enforce a roughly 90% India-focused / 10% any-region mix.
RSS_SOURCES = [
    # --- Indian sources (Hindi) ---
    ("Aaj Tak", "https://www.aajtak.in/rssfeeds/?id=home", "india"),
    ("NDTV Hindi", "https://feeds.feedburner.com/ndtvkhabar-latest", "india"),
    ("News18 Hindi", "https://hindi.news18.com/rss/khabar/nation/nation.xml", "india"),
    ("Amar Ujala", "https://www.amarujala.com/rss/breaking-news.xml", "india"),
    ("Navbharat Times", "https://navbharattimes.indiatimes.com/langapi/sitemap/gstandrssfeed.xml", "india"),
    ("TV9 Hindi", "https://www.tv9hindi.com/feed", "india"),
    ("Oneindia Hindi", "https://hindi.oneindia.com/rss/hindi-news-fb.xml", "india"),
    # --- Indian sources (English) -- broadens topic coverage, same-day translation ---
    ("NDTV English - Top Stories", "https://feeds.feedburner.com/ndtvnews-top-stories", "india"),
    ("Times of India - Top Stories", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "india"),
    ("Hindustan Times - India News", "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml", "india"),
    # --- Global sources -- for the ~10% any-region slice ---
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "global"),
    ("Reuters World", "https://www.reutersagency.com/feed/?best-topics=world", "global"),
]

HISTORY_FILE = Path("posted_topics.json")
HISTORY_RETENTION_DAYS = 14   # how far back we check for duplicates
TARGET_TOPIC_COUNT = (5, 10)  # min, max topics to return this run
FINAL_TOPIC_LIMIT = None      # if set, trims the final list to exactly this many AFTER dedup filtering
if TOPIC_LIMIT:
    n = int(TOPIC_LIMIT)
    FINAL_TOPIC_LIMIT = n
    # Ask Gemini for a small buffer beyond n so the deterministic dedup
    # filter below has room to drop an exact-duplicate pick without leaving
    # zero results -- we trim down to n only after filtering.
    TARGET_TOPIC_COUNT = (n, n + 3)

GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"


def post_to_gemini_with_retry(body, max_retries=3, timeout=90):
    """
    Wraps a Gemini API call with retries + exponential backoff, so a
    transient network blip (timeout, connection reset) doesn't kill the
    whole run -- it just costs a few extra seconds and tries again.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(GEMINI_URL, json=body, timeout=timeout)
            if resp.status_code != 200:
                print(f"Gemini API error {resp.status_code} (attempt {attempt}/{max_retries}): {resp.text[:1000]}")
                if attempt == max_retries:
                    resp.raise_for_status()
                time.sleep(2 * attempt)
                continue
            return resp
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"  [warn] Gemini request failed (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(2 * attempt)  # 2s, 4s, 8s
    raise last_error


# ---------- Stage 0a: fetch raw news via RSS ----------

def parse_rss_feed(source_name, url, region, timeout=15):
    """
    Parse a standard RSS 2.0 feed. Returns a list of article dicts in the
    same shape the rest of the pipeline expects (title, description, url,
    published, source, region). Tolerant of feeds that use slightly
    different tag sets.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsPipelineBot/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] failed to fetch {source_name}: {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  [warn] failed to parse {source_name}: {e}")
        return []

    articles = []
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")

    for item in items:
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if title:
            articles.append({
                "title": title,
                "description": description,
                "url": link,
                "published": pub_date,
                "source": source_name,
                "region": region,
            })
    return articles


def fetch_rss_news():
    """
    Pull recent news from every source in RSS_SOURCES. Each source is fetched
    independently so one broken/slow feed doesn't kill the whole run.
    """
    all_articles = []
    for source_name, url, region in RSS_SOURCES:
        print(f"Fetching {source_name} ({region})...")
        articles = parse_rss_feed(source_name, url, region)
        print(f"  {len(articles)} items from {source_name}")
        all_articles.extend(articles)
        time.sleep(0.5)  # be a polite, low-rate client to each publisher
    return all_articles


def dedupe_raw_articles(articles):
    """Basic exact/near-exact title dedup before we even ask Gemini to rank."""
    seen_hashes = set()
    unique = []
    for a in articles:
        title = (a.get("title") or "").strip().lower()
        if not title:
            continue
        h = hashlib.md5(title.encode("utf-8")).hexdigest()
        if h not in seen_hashes:
            seen_hashes.add(h)
            unique.append(a)
    return unique


# ---------- Stage 0b: load/save post history ----------

def load_history():
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
    return [
        h for h in history
        if datetime.fromisoformat(h["posted_at"]) > cutoff
    ]


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    # Caller (GitHub Actions step) is responsible for git add/commit/push.


# ---------- Stage 0c: Gemini ranking + dedup + virality scoring ----------

RANKING_PROMPT = """You are the assignment editor for a fast-paced Hindi news
Shorts channel. You will be given:
1. A list of recent raw news articles (title + short description).
2. A list of topics we have ALREADY posted about in the last {retention_days} days.

Your job: pick {min_n}-{max_n} of the raw articles that are the best candidates
for short-form video (high virality potential, clear visual story, broad
audience relevance, not overly local/niche unless unusually significant).
Prioritize the MOST RECENT stories (check "published" timestamps) that also
have strong viral potential -- a highly viral story from days ago is worth
less than an equally strong one that just broke, since news value decays
fast. When two candidates are similarly strong, prefer the more recent one.

REGION MIX: Aim for roughly 90% of picks to be India-focused stories (region:
"india") and up to 10% to be any-region/global stories (region: "global") if a
genuinely strong global story exists in this batch. Do not force a global pick
if nothing in the batch is good enough -- 100% India is fine too. Never let
global picks exceed ~10% of the total.

Return ONLY a JSON array, no markdown fences, no explanation. Each item:
{{
  "article_index": <index into the input article list>,
  "topic_label": "<short English label for this topic, for internal dedup tracking>",
  "virality_score": <1-10>,
  "reason": "<one line why this is a good pick>"
}}

CRITICAL DEDUP RULES:
- Do NOT pick a topic that is substantively the same event/story as anything in
  "already_posted". Compare by underlying event, not just by wording.
- BE CAREFUL: two articles can involve the same NAMED PERSON but be genuinely
  DIFFERENT news (e.g. a politician's statement yesterday vs. a different
  politician's unrelated statement today, or the same public figure in two
  unrelated stories a week apart). Do not reject a topic just because a name
  overlaps with something already posted — check whether the actual EVENT is
  the same. Conversely, do not pick two articles in THIS batch that cover the
  same underlying event even if phrased differently by different outlets.
- If fewer than {min_n} genuinely distinct, non-duplicate, good topics exist
  in this batch, return fewer — never pad with weak or duplicate picks.

already_posted (last {retention_days} days):
{history_json}

raw_articles:
{articles_json}
"""


def rank_topics(articles, history):
    articles_payload = [
        {
            "index": i,
            "title": a.get("title", ""),
            "description": (a.get("description") or "")[:300],
            "published": a.get("published", ""),
            "region": a.get("region", "india"),
        }
        for i, a in enumerate(articles)
    ]
    history_payload = [
        {"topic_label": h["topic_label"], "posted_at": h["posted_at"]}
        for h in history
    ]

    prompt = RANKING_PROMPT.format(
        retention_days=HISTORY_RETENTION_DAYS,
        min_n=TARGET_TOPIC_COUNT[0],
        max_n=TARGET_TOPIC_COUNT[1],
        history_json=json.dumps(history_payload, ensure_ascii=False),
        articles_json=json.dumps(articles_payload, ensure_ascii=False),
    )

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    resp = post_to_gemini_with_retry(body)
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    picks = json.loads(text)

    # Deterministic backstop: Gemini is told about already_posted in the
    # prompt, but LLM instruction-following on exclusion lists isn't
    # reliable enough to depend on alone. Hard-filter any pick whose
    # topic_label exactly matches (case-insensitive) something already
    # posted, regardless of what the model decided.
    already_posted_labels = {h["topic_label"].strip().lower() for h in history}
    before = len(picks)
    picks = [p for p in picks if p.get("topic_label", "").strip().lower() not in already_posted_labels]
    if len(picks) < before:
        print(f"  [dedup] hard-filtered {before - len(picks)} pick(s) that exactly matched posted history")

    return picks


# ---------- Stage 1: Hindi script generation (chained, same run) ----------

CONTENT_PROMPT = """You are a scriptwriter for a fast-paced Hindi news Shorts
channel. You will be given a news headline and summary. Produce ONE JSON
object, and nothing else -- no markdown fences, no preamble, no explanation.

Rules:
- All narration/script text must be in natural spoken Hindi (Devanagari script),
  suitable for a 25-30 second video read aloud at a brisk news-anchor pace
  (~2.5-3 words/second).
- tts_text_hi must be pure narration only: no stage directions, no emojis, no
  parenthetical notes -- only what should be spoken aloud.
- visual_queries must be SHORT ENGLISH search terms (2-4 words each) describing
  GENERIC concepts only -- locations, objects, actions, moods. NEVER generate a
  visual_query naming or implying a specific real, private, or ambiguous
  individual. If the story centers on a specific named public figure, set
  "named_person" to their name instead, and leave that visual slot for
  Wikimedia/manual handling downstream -- do not put the person's name in
  visual_queries.
- SEQUENCE MATTERS: visual_queries are shown IN ORDER, timed proportionally
  across the narration -- the first query plays during roughly the first
  portion of tts_text_hi, the second during the next portion, and so on. List
  them in the SAME chronological order as what's actually being said at each
  point in tts_text_hi, so the visual on screen matches what's being narrated
  at that moment. If the story covers two distinct things one after another
  (e.g. an origin/context detail, then a different location or development),
  the queries for the first thing must come before the queries for the
  second thing -- never group all queries for the story generically without
  regard to which part of the narration they belong to. A mismatch here
  (visual and narration talking about different things at the same moment)
  is jarring enough to make viewers scroll away.
- Make visual_queries as SPECIFIC as the real story actually supports: use the
  real city/state named in the headline (e.g. "gurugram flooding street" not
  just "flooding"), the real setting (e.g. "hindu temple courtyard" if the
  story is genuinely about a temple event, "parliament session" if it's
  genuinely about Parliament), and the real type of incident. Only use a
  region, religion, or setting detail if it is ACTUALLY STATED in the
  headline/summary you were given -- never infer or add a religion, region, or
  identity detail that isn't explicitly present in the source text. Generic
  filler queries like "news background" or "city street" are a last resort,
  not a first choice.
- COUNTRY ACCURACY: for region "india" stories, every visual_query must
  explicitly include "India" or "Indian" (e.g. "Indian traffic police",
  "India flooded street") unless a more specific Indian city/state is already
  named per the rule above. Stock photo search engines frequently mis-tag
  generic South Asian imagery as Bangladesh, Pakistan, or Nepal -- an
  unqualified query like "flooded street" or "crowded market" risks pulling
  the wrong country's visuals into an India story, which is a real accuracy
  problem, not a cosmetic one.
- COMBINE FOR RELATABILITY: when the story has a specific memorable visual
  action or combination of objects (e.g. a dog carrying a bag, a car covered
  in marks, a vehicle stuck in floodwater), write ONE query that captures
  that combined scene (e.g. "dog carrying bag mouth") rather than splitting
  it into disconnected generic queries (e.g. separately "dog" and "police
  investigation"). A combined, specific scene is both more findable as real
  stock content AND far more visually relatable to the actual story than
  generic fragments that don't individually suggest what happened.
- DISTINCT ANGLES, NOT REPETITION: when a story has multiple real, distinct
  elements (e.g. a temple + the security force guarding it + that force's
  headquarters + the surrounding riverside), give each element its OWN
  specific query, so the video shows genuinely different visuals for each
  beat rather than one generic shot standing in for everything. Never reuse
  the same underlying subject across two queries expecting different images
  -- if the story only has one real visual element, it's fine to have fewer
  queries rather than padding with near-duplicates.
- hook must grab attention in the first 3 seconds when spoken.
- Keep hashtags relevant, Hindi-news-audience-appropriate, max 8.
- SENSITIVE WORD DISPLAY: for words that are sensitive/explicit in nature
  (e.g. English "sex", or Hindi equivalents), the on-screen script_hi should
  partially mask the word with an asterisk (e.g. "स*क्स") so it doesn't
  display in full -- but tts_text_hi must contain the REAL, complete,
  correctly-spelled word so the narration pronounces it naturally and
  correctly. Never mask/censor tts_text_hi -- only script_hi (the visual
  text) gets the partial mask.
- NUMBERS AND DATES: never write digits (0-9) anywhere in script_hi or
  tts_text_hi. Spell every number, date, time, and measurement out in full
  Hindi words. Examples: "100" -> "सौ", "9.39 सेकंड" -> "नौ दशमलव उनतीस
  सेकंड", a date like 20/03/1997 -> "बीस मार्च उन्नीस सौ सत्तानवे". This is
  mandatory, not optional -- bare digits cause the TTS engine to mispronounce
  or fail, and this applies equally to both fields for consistency between
  what's shown and what's spoken.
- SECONDARY PEOPLE: if the story mentions a specific named public figure who
  is NOT the central subject but whose photo would still help illustrate the
  story (e.g. a record being compared to a famous athlete, a quote from a
  known official), list up to 2 such names in "secondary_persons". Only real,
  clearly-named public figures actually mentioned in the source -- never
  invent or guess a name.
- NAME FIELDS: "named_person" and each entry in "secondary_persons" must be
  the person's name in ENGLISH, spelled the way their Wikipedia page title
  would be (e.g. "Usain Bolt", "Narendra Modi") -- this is used to look up
  their photo, and a Hindi spelling won't match. Separately, set
  "named_person_hi" to how that same person's name should be DISPLAYED/
  spoken in Hindi (Devanagari), for use if no photo is found.
- This is a real news channel -- normal news content (disasters, casualties,
  crime, accidents, violence as a reported fact) should NOT be flagged just
  for being a serious or sad topic. Flag ONLY these three specific
  categories, each for a concrete reason, not general caution:
  (1) Content that could sexualize, exploit, or endanger a minor -- e.g. a
      minor as a victim of sexual abuse/exploitation, or content that
      identifies a minor victim in a way that could cause them further harm.
      Factually reporting "a case involving a minor was filed" is fine;
      detail that exploits or identifies the minor is not.
  (2) An unproven criminal allegation against a SPECIFIC NAMED individual who
      has not been convicted -- real defamation risk if the report turns out
      wrong, so this needs a human check before naming them. A story about a
      crime in general, an arrest, or a conviction is NOT this case --only
      flag when the script would assert guilt/wrongdoing of a named person
      based on unproven accusation.
  (3) Communal or religious violence/tension specifically -- India-specific
      misinformation and incitement risk that's well-documented, distinct
      from ordinary crime reporting.
  For all three, set "flag_for_manual_review": true and explain briefly in
  "flag_reason". You may still write the script. Everything else -- natural
  disasters, casualties, general crime, accidents, ongoing investigations,
  violence reported as fact -- is normal news and should NOT be flagged.
- NEEDS REAL FOOTAGE: our visual sources are Wikimedia (named public figures)
  and generic stock libraries (Pexels/Pixabay) -- they do NOT have the actual
  real photo/video of a specific viral clip, a specific unique local incident,
  or "the video everyone is sharing." If the story's whole point IS seeing
  that exact real footage (e.g. "viral video shows X", a specific vehicle/
  object/scene that generic stock B-roll can't stand in for), set
  "flag_for_manual_review": true and explain in "flag_reason" that this needs
  the real image/video added manually -- generic B-roll would misrepresent
  the story rather than illustrate it. Still write visual_queries for
  whatever generic supporting shots make sense (e.g. "traffic police India"),
  just also flag it.
- Do not fabricate details not present in the source summary.

Output JSON schema:
{{
  "hook": string,
  "script_hi": string,
  "tts_text_hi": string,
  "title": string,
  "caption": string,
  "hashtags": [string],
  "visual_queries": [string],
  "named_person": string,
  "named_person_hi": string,
  "secondary_persons": [string],
  "est_duration_sec": number,
  "flag_for_manual_review": boolean,
  "flag_reason": string
}}
(use null for named_person / flag_reason when not applicable, [] for secondary_persons when none)

headline: {headline}
source_summary: {source_summary}
"""


def generate_script_for_topic(topic):
    prompt = CONTENT_PROMPT.format(
        headline=topic["headline"],
        source_summary=topic["source_summary"],
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    try:
        resp = post_to_gemini_with_retry(body)
    except requests.exceptions.RequestException as e:
        print(f"  [error] Gemini content-gen failed after retries: {e}")
        return None
    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"  [error] failed to parse Gemini response: {e}")
        return None


def generate_all_scripts(topics):
    posts = []
    for i, topic in enumerate(topics):
        print(f"Generating script {i+1}/{len(topics)}: {topic['topic_label']}...")
        content = generate_script_for_topic(topic)
        if content is None:
            print(f"  [skip] {topic['topic_label']} -- generation failed, will not be posted")
            continue
        post = {**topic, **content}
        posts.append(post)
        if content.get("flag_for_manual_review"):
            print(f"  [FLAGGED FOR MANUAL REVIEW] {content.get('flag_reason')}")
        time.sleep(0.3)
    return posts


# ---------- Stage 1b: platform-native post copy (title/caption/hashtags) ----------
# Separate from the narration script entirely -- this ONLY touches what's
# posted alongside the video (title, caption, hashtags), never script_hi or
# tts_text_hi, which stay exactly as generated for narration.

PLATFORM_LIMITS = {
    # (max_title_chars, max_caption_chars, max_hashtags) -- verified against
    # each platform's actual current limits, not guessed.
    "youtube": {"title": 100, "caption": 5000, "hashtags": 5},
    "instagram": {"title": None, "caption": 2200, "hashtags": 5},
    "facebook": {"title": None, "caption": 5000, "hashtags": 5},
}

COPYWRITER_PROMPT = """You are a social media copywriter for a fast-paced
Hindi news Shorts channel, writing the POST TEXT that accompanies a video --
NOT the video's narration, which is already finalized and must not be
touched or referenced as needing changes. The whole point of this text is to
get someone scrolling past to actually stop and read it, then watch. A weak
generic caption gets scrolled past regardless of how good the video is.

Structure every caption as HOOK -> VALUE -> (implicit or explicit CTA):
- HOOK: this is the single most important line you write -- it decides
  whether anyone reads past it at all, so it must be genuinely scroll-
  stopping, not just "fine." Under ~15 words. Use one of these proven
  techniques, choosing whichever actually fits the real facts of THIS story
  (never force one that doesn't fit):
    * OPEN LOOP: state a surprising outcome without the explanation yet
      ("पुलिस ने जब बैग खोला तो सबके होश उड़ गए")
    * SPECIFIC NUMBER/STAKE: a real, exact figure from the story hits harder
      than a vague claim ("157 लोगों की मौत" beats "कई लोगों की मौत")
    * CONTRADICTION/TWIST: what happened defies the obvious expectation
      ("सीसीटीवी में दिखा जो हुआ, उसने पुलिस को भी चौंका दिया")
    * DIRECT STAKE TO VIEWER: why this affects them specifically, right now
  Never open with "आज की बड़ी खबर", "ब्रेकिंग न्यूज़" alone, or a flat
  restatement of the headline -- these are exactly what gets scrolled past.
  The first ~100-125 characters are what's visible before any "see more"
  cutoff on every platform, so the hook must fully land within that window.
- VALUE: 1-3 short sentences giving the real substance, in plain natural
  Hindi -- short paragraphs or line breaks, not one dense block.
- CTA: end with something that invites engagement where it fits naturally
  (a question, "पूरी कहानी देखें", etc.) -- skip it if forcing one would feel
  fake for this particular story.

Write like a real person telling someone about news they just saw, not like
a brand or a template. Vary sentence length. Avoid generic AI-sounding
filler phrases and words like "अद्भुत", "अविश्वसनीय", "आइए जानें" used as
crutch openers, "In today's fast-paced world" style throat-clearing, or
repeating the headline verbatim as the caption.

Use natural, relevant emoji (2-5 total, not excessive) placed where they'd
actually help scannability, not stuffed at the end.

CRITICAL: do NOT include any hashtags (no # symbols, no hashtag words)
inside youtube_caption, instagram_caption, or facebook_caption themselves --
hashtags go ONLY in the separate "hashtags" array below. The system appends
them to the caption automatically; writing them into the caption text too
would make them appear twice.

Story:
Headline: {headline}
Hook: {hook}
Title: {title}

BEFORE writing the final captions: internally draft 3 different hook options
using 3 different techniques from the list above, silently judge which one
would actually make a real person stop scrolling, and use ONLY that winning
hook across youtube_title/youtube_caption/instagram_caption/facebook_caption
(adapted per platform, same core hook idea). Do not show the 3 drafts in
your output -- only the final chosen versions below.

Return ONLY a JSON object, no markdown fences, no explanation:
{{
  "youtube_title": string,     // <= 100 chars, hook-first, front-loaded keyword, 1-2 emoji max
  "youtube_caption": string,   // hook->value->CTA, SEO-relevant keywords woven in naturally, hashtags go at the end separately
  "instagram_caption": string, // hook MUST land in first 125 chars, save/share-worthy
  "facebook_caption": string,  // slightly longer-form ok, storytelling tone
  "hashtags": [string]         // 5 max, no # symbol included, specific to this story not generic (#news, #india etc. are weak)
}}
"""


def generate_post_copy(post):
    prompt = COPYWRITER_PROMPT.format(
        headline=post.get("headline", ""),
        hook=post.get("hook", ""),
        title=post.get("title", ""),
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    try:
        resp = post_to_gemini_with_retry(body)
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        copy = json.loads(text)
    except Exception as e:
        print(f"    [warn] post copywriting failed, falling back to title/caption fields: {e}")
        copy = {
            "youtube_title": post.get("title", ""),
            "youtube_caption": post.get("caption", ""),
            "instagram_caption": post.get("caption", ""),
            "facebook_caption": post.get("caption", ""),
            "hashtags": post.get("hashtags", [])[:5],
        }

    # Enforce real platform limits in code -- never trust the model alone
    # for hard limits, since going over silently breaks the actual API call.
    hashtags = (copy.get("hashtags") or [])[:5]
    hashtag_str = " ".join(f"#{h}" for h in hashtags)

    def build_platform_copy(platform, title_field, caption_field):
        limits = PLATFORM_LIMITS[platform]
        title = (copy.get(title_field) or "")[: limits["title"]] if limits["title"] else None
        caption = copy.get(caption_field) or ""
        # Defensive backstop: strip any hashtags the model wrote into the
        # body despite instructions not to, so they can't appear twice once
        # the real hashtag list is appended below.
        caption = re.sub(r"#\S+", "", caption).strip()
        caption = re.sub(r"[ \t]+", " ", caption)
        full_caption = f"{caption}\n\n{hashtag_str}".strip()
        if len(full_caption) > limits["caption"]:
            full_caption = full_caption[: limits["caption"] - 3] + "..."
        return {"title": title, "caption": full_caption}

    return {
        "youtube": build_platform_copy("youtube", "youtube_title", "youtube_caption"),
        "instagram": build_platform_copy("instagram", None, "instagram_caption"),
        "facebook": build_platform_copy("facebook", None, "facebook_caption"),
        "hashtags": hashtags,
    }


def generate_all_post_copy(posts):
    for post in posts:
        if post.get("review_status") != "approved":
            continue
        print(f"  Writing platform post copy for {post['topic_label']}...")
        post["platform_copy"] = generate_post_copy(post)
        time.sleep(0.3)
    return posts


# ---------- Stage 2: asset fetching (licensed, ToS-safe sources only) ----------

def fetch_pexels_photos(query, per_page=2):
    if not PEXELS_API_KEY:
        print(f"    [warn] PEXELS_API_KEY is empty -- skipping Pexels photo search for '{query}'")
        return []
    if not _rate_limit_wait("pexels"):
        return []
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": per_page, "orientation": "portrait"},
            timeout=15,
        )
        if resp.status_code == 429:
            _rate_limit_trip("pexels")
            return []
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"    [warn] Pexels photo fetch failed for '{query}': {e}")
        return []
    return [
        {
            "type": "image",
            "url": p["src"]["large2x"],
            "source": "pexels",
            "query": query,
            "credit": p.get("photographer", ""),
        }
        for p in data.get("photos", [])
    ]


def fetch_pexels_videos(query, per_page=2):
    if not PEXELS_API_KEY:
        print(f"    [warn] PEXELS_API_KEY is empty -- skipping Pexels video search for '{query}'")
        return []
    if not _rate_limit_wait("pexels"):
        return []
    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": per_page, "orientation": "portrait"},
            timeout=15,
        )
        if resp.status_code == 429:
            _rate_limit_trip("pexels")
            return []
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"    [warn] Pexels video fetch failed for '{query}': {e}")
        return []
    results = []
    for v in data.get("videos", []):
        files = sorted(v.get("video_files", []), key=lambda f: f.get("width", 0))
        # pick a reasonably sized file, not the largest 4K one
        pick = next((f for f in files if f.get("width", 0) >= 720), files[-1] if files else None)
        if pick:
            results.append({
                "type": "video",
                "url": pick["link"],
                "source": "pexels",
                "query": query,
                "credit": v.get("user", {}).get("name", ""),
                "source_duration_sec": v.get("duration", 0),  # full clip length as published
            })
    return results


def fetch_pixabay_photos(query, per_page=2):
    if not PIXABAY_API_KEY:
        print(f"    [warn] PIXABAY_API_KEY is empty -- skipping Pixabay search for '{query}'")
        return []
    if not _rate_limit_wait("pixabay"):
        return []
    try:
        resp = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "per_page": per_page,
                "safesearch": "true",
                "image_type": "photo",
            },
            timeout=15,
        )
        if resp.status_code == 429:
            _rate_limit_trip("pixabay")
            return []
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"    [warn] Pixabay fetch failed for '{query}': {e}")
        return []
    return [
        {
            "type": "image",
            "url": h["largeImageURL"],
            "source": "pixabay",
            "query": query,
            "credit": h.get("user", ""),
        }
        for h in data.get("hits", [])
    ]


DOWNLOADED_ASSETS_DIR = Path("downloaded_assets")
DOWNLOADED_ASSETS_DIR.mkdir(exist_ok=True)

# Rate limiting, calibrated per service from each one's actual published
# policy, not arbitrary guesses:
#   - Wikimedia's official robot policy explicitly states: "keep a total
#     concurrency of at most 1, and use a delay between requests of at
#     least 1 second" -- and that this limit is GLOBAL across all their
#     properties (Commons, Wikipedia, etc.), not per-domain. So one shared
#     "wikimedia" bucket covers both commons.wikimedia.org and
#     en.wikipedia.org together.
#   - Pexels' documented limit is a generous 200 req/hour, but has an
#     unspecified per-minute component too ("limited to a certain number
#     of requests per minute") -- a modest delay keeps us safely clear of
#     that without being needlessly slow given the generous hourly budget.
#   - Pixabay publishes no hard number but explicitly asks that requests
#     not be sent "in an automated fashion" / no "systematic mass
#     downloads" -- similar modest spacing applies.
# Each service also gets its own circuit breaker: one 429 from a service
# means we stop calling THAT service for the rest of the run rather than
# continuing to hit an already-complaining server.
_RATE_LIMIT_STATE = {
    "wikimedia": {"blocked": False, "last_request": 0.0, "min_delay": 1.0},
    "pexels": {"blocked": False, "last_request": 0.0, "min_delay": 0.5},
    "pixabay": {"blocked": False, "last_request": 0.0, "min_delay": 0.5},
}


def _rate_limit_wait(service):
    """Call before every request to `service`. Returns False if that
    service's circuit breaker has already tripped this run (skip the
    request entirely), otherwise sleeps just enough to respect the
    service's minimum delay, then returns True."""
    state = _RATE_LIMIT_STATE[service]
    if state["blocked"]:
        return False
    elapsed = time.time() - state["last_request"]
    if elapsed < state["min_delay"]:
        time.sleep(state["min_delay"] - elapsed)
    state["last_request"] = time.time()
    return True


def _rate_limit_trip(service):
    """Call when a service returns 429 -- stops calling it for the rest
    of this run instead of continuing to hammer an already-limited server."""
    if not _RATE_LIMIT_STATE[service]["blocked"]:
        print(f"    [warn] {service} rate-limited us (429) -- skipping {service} for the rest of this run")
    _RATE_LIMIT_STATE[service]["blocked"] = True


def download_asset_locally(url, prefix):
    """
    Downloads an asset with proper headers and saves it locally, returning
    the local path. Necessary for Wikimedia/Commons specifically: their
    servers enforce a User-Agent policy and return 403 to FFmpeg's default
    HTTP client when given the raw URL directly -- downloading via
    `requests` (which already sends a proper User-Agent) and handing FFmpeg
    a local file sidesteps this entirely.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsPipelineBot/1.0)"},
            timeout=20,
        )
        if resp.status_code == 429 and "wikimedia.org" in url:
            _rate_limit_trip("wikimedia")
        resp.raise_for_status()
        ext = ".jpg"
        content_type = resp.headers.get("Content-Type", "")
        if "png" in content_type:
            ext = ".png"
        elif "webp" in content_type:
            ext = ".webp"
        local_path = DOWNLOADED_ASSETS_DIR / f"{prefix}_{hashlib.md5(url.encode()).hexdigest()[:10]}{ext}"
        local_path.write_bytes(resp.content)
        return str(local_path)
    except requests.RequestException as e:
        print(f"    [warn] failed to download asset from {url}: {e}")
        return None


def fetch_wikimedia_person_image(name):
    """
    Look up a named public figure on Wikipedia and return their infobox
    image if found. Only used for named_person -- never for generic
    visual_queries. Returns None (triggering a text-card fallback) if no
    confident match is found, rather than guessing. Shares the same
    "wikimedia" rate-limit bucket as Commons -- Wikimedia's own policy
    states limits are global across their properties, not per-domain.
    """
    if not _rate_limit_wait("wikimedia"):
        return None
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "titles": name,
                "prop": "pageimages",
                "format": "json",
                "pithumbsize": 800,
                "redirects": 1,
            },
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsPipelineBot/1.0)"},
            timeout=15,
        )
        if resp.status_code == 429:
            _rate_limit_trip("wikimedia")
            return None
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page_id, page in pages.items():
            if page_id == "-1":
                continue  # page not found
            thumb = page.get("thumbnail", {}).get("source")
            if thumb:
                local_path = download_asset_locally(thumb, "person")
                if not local_path:
                    return None
                return {
                    "type": "image",
                    "url": local_path,
                    "source": "wikimedia",
                    "query": name,
                    "credit": "Wikipedia",
                }
    except requests.RequestException as e:
        print(f"    [warn] Wikimedia lookup failed for '{name}': {e}")
    return None


def get_verification_frame_bytes(asset):
    """
    Gets a single representative image (bytes, mime_type) from a candidate
    asset for vision verification -- direct read for already-local files
    (Wikimedia/Commons, downloaded locally to sidestep their User-Agent
    policy), download for remote URLs (Pexels/Pixabay), first frame
    extraction via ffmpeg for videos.
    """
    try:
        is_local = os.path.exists(asset["url"])
        if asset["type"] == "image":
            if is_local:
                return Path(asset["url"]).read_bytes()[:5_000_000], "image/jpeg"
            resp = requests.get(asset["url"], timeout=15)
            resp.raise_for_status()
            return resp.content[:5_000_000], "image/jpeg"
        elif asset["type"] == "video":
            if is_local:
                tmp_vid_path = asset["url"]
            else:
                resp = requests.get(asset["url"], timeout=25)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_vid:
                    tmp_vid.write(resp.content)
                    tmp_vid_path = tmp_vid.name
            tmp_frame_path = tmp_vid_path + ".jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_vid_path, "-frames:v", "1", "-update", "1", tmp_frame_path],
                capture_output=True, timeout=30,
            )
            with open(tmp_frame_path, "rb") as f:
                frame_bytes = f.read()
            if not is_local:
                os.remove(tmp_vid_path)
            os.remove(tmp_frame_path)
            return frame_bytes, "image/jpeg"
    except Exception as e:
        print(f"      [warn] could not extract verification frame: {e}")
        return None, None
    return None, None


def verify_asset_relevance(asset, query, region):
    """
    Uses Gemini's vision capability to sanity-check a candidate asset before
    it's accepted -- stock libraries and Commons search results aren't
    reliably tagged by country/context, so we verify the actual image
    content rather than trusting keyword matches alone. Fails OPEN (accepts
    the asset) if verification can't run for any reason, so a transient
    issue never blocks the whole pipeline -- this is a quality filter, not
    a hard gate.
    """
    frame_bytes, mime_type = get_verification_frame_bytes(asset)
    if not frame_bytes:
        return True  # couldn't get a frame to check -- fail open

    context_note = ""
    if region == "india":
        context_note = (
            " This is for an India-focused news story -- reject it if it "
            "clearly shows a DIFFERENT country's military, police, flags, "
            "or unmistakably non-Indian government/uniformed personnel."
        )

    prompt = (
        f"This image was found searching for '{query}' to illustrate a news "
        f"video. Does it plausibly, generically match that search term?"
        f"{context_note} Reply with ONLY 'YES' or 'NO'."
    )
    try:
        b64 = base64.b64encode(frame_bytes).decode("utf-8")
        body = {
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": b64}},
            ]}]
        }
        resp = post_to_gemini_with_retry(body, max_retries=2, timeout=30)
        answer = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
        return answer.startswith("YES")
    except Exception as e:
        print(f"      [warn] vision verification failed, accepting by default: {e}")
        return True  # fail open


def fetch_wikimedia_commons_image(query):
    """
    Search Wikimedia Commons (not just Wikipedia) for a real, properly-
    licensed photo matching a generic visual_query -- e.g. an actual photo
    of a real place, landmark, building, or documented event, rather than
    generic stock B-roll. Commons hosts millions of CC/public-domain images
    including many real newsworthy photos. Tried before Pexels/Pixabay so
    real, specific imagery is preferred over generic stock when it exists.

    Only tries the SINGLE top result, not multiple -- retrying 2-3 more
    results per query when the first fails (especially under rate limiting)
    multiplies request volume for no benefit, since a 429 on result 1 means
    results 2 and 3 will also 429.
    """
    if not _rate_limit_wait("wikimedia"):
        return None
    try:
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrnamespace": 6,  # File namespace
                "gsrlimit": 1,
                "prop": "imageinfo",
                "iiprop": "url|mime",
                "format": "json",
            },
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsPipelineBot/1.0)"},
            timeout=15,
        )
        if resp.status_code == 429:
            _rate_limit_trip("wikimedia")
            return None
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            infos = page.get("imageinfo", [])
            if not infos:
                continue
            info = infos[0]
            mime = info.get("mime", "")
            if not mime.startswith("image/"):
                continue  # skip non-image files (audio, pdf, etc.)
            local_path = download_asset_locally(info["url"], "commons")
            if not local_path:
                continue
            return {
                "type": "image",
                "url": local_path,
                "source": "wikimedia_commons",
                "query": query,
                "credit": "Wikimedia Commons",
            }
    except requests.RequestException as e:
        print(f"    [warn] Wikimedia Commons search failed for '{query}': {e}")
    return None


def get_assets_for_post(post):
    """
    Priority order per query: Pexels video -> Pexels photo -> Pixabay photo.
    Wikimedia is tried first and separately for a named_person, since that's
    a portrait lookup, not a concept search.
    Returns a raw asset pool; plan_asset_timeline() below decides how much of
    it is actually used and for how long.
    """
    assets = []

    if post.get("named_person"):
        name = post["named_person"]  # English, for lookup
        display_name = post.get("named_person_hi") or name  # Hindi, for display
        print(f"    Looking up named person: {name}")
        person_img = fetch_wikimedia_person_image(name)
        if person_img:
            assets.append(person_img)
        else:
            print(f"    [fallback] no confident Wikimedia match for '{name}' -- using text card")
            assets.append({
                "type": "text_card",
                "text": display_name,
                "source": "fallback",
                "query": name,
            })

    for name in (post.get("secondary_persons") or [])[:2]:
        print(f"    Looking up secondary person: {name}")
        person_img = fetch_wikimedia_person_image(name)
        if person_img:
            assets.append(person_img)
        else:
            print(f"    [skip] no confident Wikimedia match for secondary person '{name}'")

    region = post.get("region", "india")
    used_urls = set()

    def fetch_and_verify(query):
        """Fetches candidates for one query and returns the first one that
        passes vision verification AND isn't already used elsewhere in this
        post -- prevents the same image from being reused multiple times in
        one video. Tries image sources FIRST (Commons, then Pexels/Pixabay
        photo) -- stock video libraries structurally don't carry footage of
        specific news events, so attempting video first wastes API calls and
        vision-check calls on candidates unlikely to match. Video is tried
        last, as a fallback for queries generic enough that stock footage
        might genuinely exist."""
        candidates = []

        commons_img = fetch_wikimedia_commons_image(query)
        if commons_img:
            candidates.append(commons_img)

        photo = fetch_pexels_photos(query, per_page=1)
        if not photo:
            photo = fetch_pixabay_photos(query, per_page=1)
        if photo:
            candidates.extend(photo)

        for candidate in candidates:
            if candidate["url"] in used_urls:
                print(f"      [skip] duplicate asset already used elsewhere in this video for '{query}'")
                continue
            print(f"    Verifying {candidate['type']} from {candidate['source']} for '{query}'...")
            if verify_asset_relevance(candidate, query, region):
                used_urls.add(candidate["url"])
                return candidate
            print(f"      [rejected] vision check failed relevance for '{query}' -- trying next source")

        # Only reach for video if no image passed -- last resort, not first.
        video = fetch_pexels_videos(query, per_page=1)
        for candidate in (video or []):
            if candidate["url"] in used_urls:
                continue
            print(f"    Verifying {candidate['type']} from {candidate['source']} for '{query}' (fallback)...")
            if verify_asset_relevance(candidate, query, region):
                used_urls.add(candidate["url"])
                return candidate
            print(f"      [rejected] vision check failed relevance for '{query}'")
        return None

    for query in (post.get("visual_queries") or [])[:6]:
        accepted = fetch_and_verify(query)

        if not accepted:
            # Every source failed vision check for this query -- don't just
            # give up on the slot, ask for ONE different query and try again.
            print(f"    All sources rejected for '{query}' -- asking for an alternative query...")
            alt_queries = suggest_fallback_queries(post, [query], n=1)
            if alt_queries:
                alt_query = alt_queries[0]
                print(f"    Retrying this slot with: '{alt_query}'")
                accepted = fetch_and_verify(alt_query)

        if accepted:
            assets.append(accepted)
        else:
            print(f"    [warn] no verified asset found for query '{query}' (or its alternative) -- skipping (no wrong-asset guess)")
        time.sleep(0.2)

    return assets


def plan_asset_timeline(assets, target_duration=TARGET_DURATION_SEC):
    """
    Turns a raw asset pool into an ordered, timed sequence:
    - Video assets are used close to independently, up to their own length,
      capped at whatever time remains in the target duration.
    - Any remaining time (or all of it, if no usable video was found) is
      filled with images, each shown for IMAGE_MIN-MAX_DURATION seconds,
      using between MIN_IMAGES_IF_NO_VIDEO and MAX_IMAGES_IF_NO_VIDEO images.
    - A leading text_card (named-person fallback) is always kept and given
      a short fixed slot, since it's identity-critical context, not filler.
    """
    text_cards = [a for a in assets if a["type"] == "text_card"]
    videos = [a for a in assets if a["type"] == "video"]
    photos = [a for a in assets if a["type"] == "image"]

    timeline = []
    remaining = float(target_duration)

    # 1. Text card (named person fallback), if present: fixed short slot.
    for tc in text_cards:
        dur = min(4.0, remaining)
        timeline.append({**tc, "duration_sec": round(dur, 1)})
        remaining -= dur

    # 2. Videos: run close to independently, but capped per-clip so one long
    #    clip can't consume the whole budget -- variety matters more than
    #    using a single video's full length.
    for v in videos:
        if remaining <= 0.5:
            break
        clip_len = v.get("source_duration_sec") or target_duration
        dur = min(clip_len, remaining, MAX_SINGLE_ASSET_DURATION)
        timeline.append({**v, "duration_sec": round(dur, 1)})
        remaining -= dur

    # 3. Images: fill whatever time is left, if any, within min/max image
    #    count and min/max per-image duration.
    if remaining > 0.5 and photos:
        # how many images would we need at max duration, and at min duration?
        max_possible = min(len(photos), MAX_IMAGES_IF_NO_VIDEO)
        min_needed = max(1, MIN_IMAGES_IF_NO_VIDEO if not videos else 1)
        n_images = max(min_needed, min(max_possible, int(remaining // IMAGE_MIN_DURATION) or 1))
        n_images = max(1, min(n_images, len(photos), MAX_IMAGES_IF_NO_VIDEO))

        per_image = remaining / n_images
        per_image = max(IMAGE_MIN_DURATION, min(IMAGE_MAX_DURATION, per_image))

        for photo in photos[:n_images]:
            if remaining <= 0.3:
                break
            dur = min(per_image, remaining)
            timeline.append({**photo, "duration_sec": round(dur, 1)})
            remaining -= dur

    return timeline


FALLBACK_QUERY_PROMPT = """A Hindi news Shorts video needs generic B-roll
images/video for this story, but our first attempt at search terms didn't
find enough usable footage. Suggest {n} NEW, BROADER, more genuinely
FINDABLE English search terms (2-4 words each) for stock photo/video
libraries -- think about what generic, widely-available footage categories
would still visually represent this story, even loosely (e.g. if the story
is about a specific viral car, broader terms like "car repair shop India" or
"traffic police checking vehicle" are more findable than anything too
specific). Do not repeat any of these already-tried terms: {tried}.

Return ONLY a JSON array of strings, no markdown fences, no explanation.

Story headline: {headline}
Story summary: {summary}
"""


def suggest_fallback_queries(post, already_tried, n=4):
    prompt = FALLBACK_QUERY_PROMPT.format(
        n=n,
        tried=", ".join(already_tried) or "(none)",
        headline=post.get("headline", ""),
        summary=post.get("source_summary", ""),
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    try:
        resp = post_to_gemini_with_retry(body)
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except Exception as e:
        print(f"    [warn] fallback query generation failed: {e}")
        return []


def attach_assets_and_review_status(posts):
    final_posts = []
    for i, post in enumerate(posts):
        print(f"Fetching assets {i+1}/{len(posts)}: {post['topic_label']}...")
        raw_assets = get_assets_for_post(post)
        timeline = plan_asset_timeline(raw_assets)
        covered = sum(a["duration_sec"] for a in timeline)

        # If under-covered, don't just give up -- ask Gemini for broader,
        # more findable alternative search terms and try again before
        # falling back to manual review.
        if covered < (TARGET_DURATION_SEC * 0.6) and not post.get("flag_for_manual_review"):
            already_tried = list(post.get("visual_queries") or [])
            print(f"    Under-covered ({covered:.1f}s) -- asking for broader fallback queries...")
            fallback_queries = suggest_fallback_queries(post, already_tried)
            if fallback_queries:
                print(f"    Retrying with: {fallback_queries}")
                retry_post = {**post, "visual_queries": fallback_queries}
                extra_assets = get_assets_for_post({**retry_post, "named_person": None, "secondary_persons": []})
                raw_assets = raw_assets + extra_assets
                timeline = plan_asset_timeline(raw_assets)
                covered = sum(a["duration_sec"] for a in timeline)
                print(f"    After retry: {covered:.1f}s covered")

        post["assets"] = timeline
        post["asset_count"] = len(timeline)
        post["timeline_covered_sec"] = round(covered, 1)

        flagged = bool(post.get("flag_for_manual_review"))
        no_assets = len(timeline) == 0
        under_covered = covered < (TARGET_DURATION_SEC * 0.6)  # still under 60% after retry

        if flagged or no_assets or under_covered:
            post["review_status"] = "pending_review"
        elif AUTO_APPROVE:
            post["review_status"] = "approved"
        else:
            post["review_status"] = "pending_review"

        final_posts.append(post)
    return final_posts


def print_review_list(posts):
    print("\n" + "=" * 60)
    print("REVIEW LIST")
    print("=" * 60)
    for i, p in enumerate(posts):
        status = p["review_status"].upper()
        print(f"\n[{i}] {status} -- {p['topic_label']} (score {p.get('virality_score','?')})")
        print(f"    Title: {p.get('title','')}")
        print(f"    Hook: {p.get('hook','')}")
        covered = p.get('timeline_covered_sec', 0)
        print(f"    Timeline: {covered}s / {TARGET_DURATION_SEC}s covered, {p.get('asset_count',0)} assets")
        for a in p.get("assets", []):
            print(f"      - {a['type']} ({a['duration_sec']}s) from {a['source']}: '{a.get('query','')}'")
        if p.get("named_person"):
            print(f"    Named person: {p['named_person']}")
        if p.get("flag_for_manual_review"):
            print(f"    ⚠ FLAGGED: {p.get('flag_reason','')}")
        if covered < TARGET_DURATION_SEC * 0.6:
            print(f"    ⚠ UNDER-COVERED -- only {covered}s of {TARGET_DURATION_SEC}s filled, needs manual asset or skip")
    print("\n" + "=" * 60)
    approved = sum(1 for p in posts if p["review_status"] == "approved")
    pending = sum(1 for p in posts if p["review_status"] == "pending_review")
    print(f"{approved} auto-approved, {pending} pending manual review.")
    if AUTO_APPROVE:
        print("(AUTO_APPROVE is ON -- flagged/no-asset/under-covered posts still held for manual review regardless.)")
    else:
        print("(AUTO_APPROVE is OFF -- all posts held for manual review.)")
    print("=" * 60)


def main():
    print(f"PEXELS_API_KEY present: {bool(PEXELS_API_KEY)}")
    print(f"PIXABAY_API_KEY present: {bool(PIXABAY_API_KEY)}")

    remaining_budget = get_remaining_daily_budget()

    print("Checking for previously-flagged posts you've approved via GitHub Issues...")
    resumed_posts = fetch_approved_review_issues()
    print(f"{len(resumed_posts)} approved post(s) resumed from review issues.")

    # Resumed posts count against today's budget too -- whatever's left
    # after them is what's available for freshly-picked topics this run.
    fresh_budget = max(0, remaining_budget - len(resumed_posts))

    if remaining_budget == 0:
        print(f"Daily cap of {DAILY_POST_CAP} already reached today -- skipping this run entirely.")
        Path("posts_with_assets.json").write_text("[]", encoding="utf-8")
        return []

    if fresh_budget == 0:
        print(f"Resumed posts alone ({len(resumed_posts)}) use up today's remaining budget -- skipping fresh topic picks this run.")

    print("Fetching news from RSS sources...")
    raw = fetch_rss_news()
    raw = dedupe_raw_articles(raw)
    print(f"{len(raw)} unique raw articles fetched.")

    history = load_history()
    print(f"{len(history)} posts in dedup window ({HISTORY_RETENTION_DAYS}d).")

    if fresh_budget == 0:
        picks = []
    else:
        print("Asking Gemini to rank + dedup...")
        picks = rank_topics(raw, history)
        print(f"Gemini returned {len(picks)} topic picks (post-dedup).")
        effective_limit = min(FINAL_TOPIC_LIMIT, fresh_budget) if FINAL_TOPIC_LIMIT else fresh_budget
        picks = picks[:effective_limit]
        print(f"Capped to {len(picks)} picks (min of configured limit and remaining daily budget of {fresh_budget}).")

    # Attach full article data back onto each pick for Stage 1 to consume.
    output = []
    for p in picks:
        article = raw[p["article_index"]]
        output.append({
            "topic_label": p["topic_label"],
            "virality_score": p["virality_score"],
            "reason": p["reason"],
            "region": article.get("region", "india"),
            "headline": article.get("title", ""),
            "source_summary": article.get("description", ""),
            "source_url": article.get("url", ""),
            "source_name": article.get("source", ""),
            "published": article.get("published", ""),
        })

    # Sort by virality score, highest first.
    output.sort(key=lambda x: x["virality_score"], reverse=True)

    out_path = Path("stage0_picked_topics.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(output)} picked topics to {out_path}")

    print("Generating Hindi scripts for each picked topic...")
    posts = generate_all_scripts(output)
    posts_path = Path("posts_ready.json")
    with open(posts_path, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(posts)} ready posts to {posts_path}")

    print("Fetching visual assets for each post...")
    posts = attach_assets_and_review_status(posts)

    # Create a review issue for every FRESH post still pending -- resumed
    # posts already went through this once, don't re-create their issue.
    for post in posts:
        if post.get("review_status") == "pending_review":
            create_review_issue(post)

    # Resumed (already-approved) posts join the fresh batch here, skipping
    # straight past script/asset generation since they already have both.
    posts = resumed_posts + posts

    print("Writing platform-native post copy (title/caption/hashtags) for approved posts...")
    posts = generate_all_post_copy(posts)

    final_path = Path("posts_with_assets.json")
    with open(final_path, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(posts)} posts with assets to {final_path}")

    print_review_list(posts)

    # NOTE: we do NOT write to posted_topics.json here. That happens only
    # after a post is actually rendered + published (Stage 4/5), so a topic
    # that gets picked but fails downstream doesn't falsely block a retry.
    # See stage5_mark_posted.py (to be built) for that step.

    return posts


if __name__ == "__main__":
    main()
