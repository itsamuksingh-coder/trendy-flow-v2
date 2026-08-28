"""
Stage 3 -- Text-to-speech narration generation using AI4Bharat Indic
Parler-TTS: a self-hosted, open-weight, MIT-licensed Hindi TTS model, run
locally inside this GitHub Actions job.

Why self-hosted instead of an external API:
- Genuinely free, no card, no signup, no daily/monthly quota -- the model
  runs on this runner's own CPU using compute minutes already included in
  your GitHub Actions allowance. Nothing external is called at generation
  time once the model weights are downloaded.
- MIT licensed -- explicitly permits commercial use, unlike some other
  open Indic TTS checkpoints that are non-commercial-only.

Trade-off, stated plainly: CPU inference is slower than a hosted API
(roughly seconds per sentence, not milliseconds) and voice quality is good
open-source quality, not premium-commercial polish. Given the requirement
was "genuinely free, no card," this is that trade-off made explicit.

Input:  posts_with_assets.json -- only posts with review_status ==
        "approved" are synthesized, so compute isn't spent on posts still
        awaiting manual review.
Output: audio/<topic_label>.wav per approved post, plus posts_with_audio.json
        recording each post's audio path and *actual measured* duration
        (needed downstream for accurate subtitle timing).
"""

import json
import os
import re
from pathlib import Path

import soundfile as sf
import torch
from huggingface_hub import login as hf_login
from parler_tts import ParlerTTSForConditionalGeneration
from transformers import AutoTokenizer

HF_TOKEN = os.environ.get("HF_TOKEN", "")
if HF_TOKEN:
    hf_login(token=HF_TOKEN)
else:
    print("[warn] HF_TOKEN is empty -- this will fail, since ai4bharat/indic-parler-tts is a gated model.")
    print("[warn] Create a free Hugging Face account, accept the model's terms, generate a read token,")
    print("[warn] and add it as the HF_TOKEN repo secret.")

MODEL_ID = "ai4bharat/indic-parler-tts"  # if this is too slow on CPU, try
                                          # "ai4bharat/indic-parler-tts-pretrained"
                                          # (the smaller "Mini" checkpoint)
AUDIO_DIR = Path("audio")
AUDIO_DIR.mkdir(exist_ok=True)

# Steers Parler-TTS's voice style -- this model is controlled by natural
# language description rather than a fixed voice-ID parameter.
VOICE_DESCRIPTION = (
    "Rani, a young Indian woman, speaks in a distinctly female, sweet, warm, "
    "and gentle voice at a moderate pace, with a soft and pleasant tone like "
    "a friendly female news presenter, in a very close-sounding, "
    "high-quality studio recording with no background noise."
)


def load_model():
    print(f"Loading {MODEL_ID} (first run downloads the model, cached after that)...")
    device = "cpu"
    model = ParlerTTSForConditionalGeneration.from_pretrained(MODEL_ID).to(device)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    description_tokenizer = AutoTokenizer.from_pretrained(
        model.config.text_encoder._name_or_path
    )
    print("Model loaded.")
    return model, tokenizer, description_tokenizer, device


def synthesize(model, tokenizer, description_tokenizer, device, text, out_path):
    desc_ids = description_tokenizer(
        VOICE_DESCRIPTION, return_tensors="pt"
    ).input_ids.to(device)
    prompt_ids = tokenizer(text, return_tensors="pt").input_ids.to(device)
    generation = model.generate(input_ids=desc_ids, prompt_input_ids=prompt_ids)
    audio_arr = generation.cpu().numpy().squeeze()
    sf.write(out_path, audio_arr, model.config.sampling_rate)
    duration_sec = len(audio_arr) / model.config.sampling_rate
    return duration_sec


def safe_filename(label):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:60]


def main():
    posts = json.loads(Path("posts_with_assets.json").read_text(encoding="utf-8"))
    approved = [p for p in posts if p.get("review_status") == "approved"]
    print(f"{len(approved)} of {len(posts)} posts are approved -- generating audio only for those.")

    if not approved:
        print("Nothing approved yet -- skipping TTS this run.")
        print("(Approve posts, e.g. via auto_approve, then re-run to generate audio for them.)")
        Path("posts_with_audio.json").write_text(
            json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return posts

    model, tokenizer, description_tokenizer, device = load_model()

    for i, post in enumerate(posts):
        if post.get("review_status") != "approved":
            continue
        print(f"Synthesizing {i+1}: {post['topic_label']}...")
        fname = safe_filename(post["topic_label"]) + ".wav"
        out_path = AUDIO_DIR / fname
        try:
            duration = synthesize(
                model, tokenizer, description_tokenizer, device,
                post["tts_text_hi"], out_path,
            )
            post["audio_path"] = str(out_path)
            post["audio_duration_sec"] = round(duration, 2)
            print(f"  -> {out_path} ({duration:.1f}s)")
        except Exception as e:
            print(f"  [error] TTS failed for {post['topic_label']}: {e}")
            post["audio_path"] = None
            post["audio_duration_sec"] = None
            post["review_status"] = "pending_review"  # don't let a broken post slip through

    out_path = Path("posts_with_audio.json")
    out_path.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    return posts


if __name__ == "__main__":
    import sys
    import traceback
    try:
        main()
    except Exception:
        with open("stage3_error.txt", "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        print("Wrote full traceback to stage3_error.txt")
        sys.exit(1)
