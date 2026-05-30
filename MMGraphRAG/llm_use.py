"""
test_gemini.py  —  Self-contained reference script: Gemini via GCP (text + image).
================================================================================
PURPOSE
  Demonstrates how to call Gemini models (text and multimodal/image) using
  LangChain + Google Generative AI, with two auth paths.  Copy the patterns
  here into any other application — no prior context needed.

DEPENDENCIES  (install once)
  uv sync --extra kag
  # or: pip install langchain-google-genai google-cloud-aiplatform
  #                 google-generativeai python-dotenv

.env KEYS REQUIRED (one auth path must be set)
  # Path 1 — Google AI Studio (API key, no project needed)
  GOOGLE_API_KEY=AIza...

  # Path 2 — Vertex AI (uses ADC from  gcloud auth application-default login)
  MODEL_PROJECT_ID=your-gcp-project-id
  MODEL_LOCATION=us-central1          # optional, defaults to us-central1
  MODEL_NAME=gemini-2.5-flash         # optional, can also be gemini-2.5-pro

IMAGE TEST
  Save the image you want described as  taskbar.png  next to this file,
  then run the script — it will describe the image after the text test.

RUN
  python test_gemini.py
"""
from __future__ import annotations
import os, sys

# ── Load the project .env (same file used by the full app) ──────────────────
from pathlib import Path
_env = Path(__file__).parent / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env, override=True)
    print(f"[env] Loaded {_env}")
else:
    print("[env] No .env found — relying on shell environment variables")

# ── Read config ──────────────────────────────────────────────────────────────
GOOGLE_API_KEY  = os.environ.get("GOOGLE_API_KEY", "").strip().strip('"')
PROJECT_ID      = os.environ.get("MODEL_PROJECT_ID", "").strip().strip('"')
LOCATION        = os.environ.get("MODEL_LOCATION", "us-central1").strip().strip('"')
MODEL_NAME      = "gemini-2.5-flash"  # override: test pro model

print(f"[config] model        = {MODEL_NAME}")
print(f"[config] GOOGLE_API_KEY  = {'SET (' + str(len(GOOGLE_API_KEY)) + ' chars)' if GOOGLE_API_KEY else 'NOT SET'}")
print(f"[config] MODEL_PROJECT_ID = {'SET → ' + PROJECT_ID if PROJECT_ID else 'NOT SET'}")
print(f"[config] MODEL_LOCATION  = {LOCATION}")
print()

# ── Build a LangChain ChatGoogleGenerativeAI client ─────────────────────────
try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_core.messages import SystemMessage, HumanMessage
except ImportError:
    sys.exit("ERROR: langchain-google-genai not installed. Run: uv sync --extra kag")

client = None

if GOOGLE_API_KEY:
    # ── Path 1: Google AI Studio (API key) ──────────────────────────────────
    print("[auth] Using GOOGLE_API_KEY (Google AI Studio)")
    client = ChatGoogleGenerativeAI(
        model=MODEL_NAME,
        google_api_key=GOOGLE_API_KEY,
        max_output_tokens=1000,
    )

elif PROJECT_ID:
    # ── Path 2: Vertex AI (ADC — gcloud auth application-default login) ─────
    print(f"[auth] Using Vertex AI ADC (project={PROJECT_ID}, location={LOCATION})")
    import google.auth
    import google.auth.transport.requests

    try:
        creds, _ = google.auth.default(scopes=[
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/generative-language",
        ])
        creds.refresh(google.auth.transport.requests.Request())
    except Exception as e:
        sys.exit(
            f"ERROR: ADC refresh failed: {e}\n"
            "Fix: run  gcloud auth application-default login"
        )

    client = ChatGoogleGenerativeAI(
        model=MODEL_NAME,
        credentials=creds,
        project=PROJECT_ID,
        location=LOCATION,
        max_output_tokens=2048,
    )

else:
    sys.exit(
        "ERROR: Neither GOOGLE_API_KEY nor MODEL_PROJECT_ID is set.\n"
        "Add one of these to your .env file:\n"
        "  GOOGLE_API_KEY=AIza...         # Google AI Studio key\n"
        "  MODEL_PROJECT_ID=my-project    # GCP project for Vertex AI\n"
    )

import asyncio, base64

# ── Pre-load image (before event loop starts) ────────────────────────────────
IMAGE_PATH = Path(__file__).parent / "image.png"
_image_b64: str | None = None
_image_mime = "image/png"

if IMAGE_PATH.exists():
    _ext = IMAGE_PATH.suffix.lower().lstrip(".")
    _image_mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                   "gif": "image/gif", "webp": "image/webp"}.get(_ext, "image/png")
    with open(IMAGE_PATH, "rb") as _f:
        _image_b64 = base64.b64encode(_f.read()).decode()
    print(f"[image] Loaded {IMAGE_PATH.name} ({len(_image_b64) // 1024} KB base64)\n")
else:
    print(f"[skip-image] {IMAGE_PATH.name} not found — image test will be skipped.\n")


# ── Single event loop: runs text + image tests back-to-back ─────────────────
async def main():
    # --- Part 1: Text ---
    TEXT_PROMPT = "Reply in one sentence: what is a knowledge graph?"
    print(f"[test-text] Sending: {TEXT_PROMPT!r}")
    text_resp = await client.ainvoke([
        SystemMessage(content="You are a concise assistant."),
        HumanMessage(content=TEXT_PROMPT),
    ])
    print("=" * 60)
    print("[text] Gemini response:")
    print(text_resp.content)
    print("=" * 60)
    print("[OK] Text call succeeded.")

    # --- Part 2: Image (multimodal) ---
    # Pattern reusable in any app:
    #   HumanMessage(content=[
    #       {"type": "text",      "text": "your prompt"},
    #       {"type": "image_url", "image_url": {"url": "data:<mime>;base64,<b64>"}},
    #   ])
    if _image_b64 is None:
        return

    print(f"\n[test-image] Describing {IMAGE_PATH.name} ...")
    img_resp = await client.ainvoke([
        HumanMessage(content=[
            {"type": "text", "text": "Describe this image in detail. List every visible UI element."},
            {"type": "image_url", "image_url": {"url": f"data:{_image_mime};base64,{_image_b64}"}},
        ])
    ])
    print("=" * 60)
    print("[image] Gemini description:")
    print(img_resp.content)
    print("=" * 60)
    print("[OK] Image call succeeded.")


try:
    asyncio.run(main())
except Exception as e:
    print(f"\n[FAIL] {e}")
    sys.exit(1)
