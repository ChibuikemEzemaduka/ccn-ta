"""
llm_client.py

================================================================================
***  THIS IS THE ONE FILE TO EDIT IF YOU EVER WANT TO SWITCH MODELS/PROVIDER ***
================================================================================
Every other file in this project (ask.py, add_homework.py, generate_variants.py,
student_chat.py, instructor_dashboard.py) imports this module and calls
llm_client.generate(...) / generate_stream(...) -- none of them know or care
whether that's talking to a LOCAL Ollama model on your machine or a CLOUD model
on Ollama's servers. You only ever change settings in THIS file.

--------------------------- LOCAL vs OLLAMA CLOUD ----------------------------
This client supports both, controlled by USE_CLOUD below:

  • LOCAL  (USE_CLOUD = False): talks to http://localhost:11434, no API key,
    models run on your machine. keep_alive keeps the model resident in your GPU.

  • CLOUD  (USE_CLOUD = True): talks to https://ollama.com/api, requires an API
    key (create one at https://ollama.com/settings/keys, then set the
    OLLAMA_API_KEY environment variable, or paste it into OLLAMA_API_KEY below).
    Models run on Ollama's datacenter GPUs -- no local GPU needed. Billing is by
    GPU-time, so the guard-token cap below directly saves money as well as time.

Cloud model names: when hitting the hosted API directly you can use either the
bare name (e.g. "gpt-oss:120b") or the "-cloud" tag (e.g. "gpt-oss:120b-cloud").
Set the model names below to whatever you intend to use.
--------------------------------------------------------------------------------
"""

import base64
import json
import os
import re
import time

import requests

# Load key/config from a local .env file if present. This is the same mechanism
# the rest of the project already uses (INSTRUCTOR_PASSWORD lives in .env too).
# python-dotenv reads a file named ".env" in the project directory and loads its
# KEY=value lines into the environment. The .env file is git-ignored (see the
# .gitignore shipped alongside this code), so your API key is NEVER committed.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # dotenv is optional; env vars set another way still work

# ============================================================================
# ****************************************************************************
# ***  EDIT THIS BLOCK TO CHANGE PROVIDER / MODELS                        ***
# ****************************************************************************
# ============================================================================

# ---- Provider switch --------------------------------------------------------
USE_CLOUD = True        # True = Ollama Cloud (hosted);  False = local Ollama

# ---- Endpoints (usually no need to change these) ---------------------------
LOCAL_BASE_URL = "http://localhost:11434"
CLOUD_BASE_URL = "https://ollama.com"     # hosted API is served under /api here

# ---- API key (CLOUD ONLY) ---------------------------------------------------
# Put your key in a file named ".env" in this directory (same folder as app.py):
#
#     OLLAMA_API_KEY=your-key-here
#
# It's loaded automatically at import (see load_dotenv above) and the .env file
# is git-ignored, so the key is never committed. If you'd rather use a real
# environment variable instead of a file, that works too -- either is read here.
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")

# ---- Models -----------------------------------------------------------------
# The TUTOR writes explanations students read + solves variant problems: keep it
# strong. The GUARD only returns tiny yes/no JSON verdicts (supervisor +
# reviewer): it can be a SMALLER, faster, cheaper model without hurting the
# quality of anything a student sees. On cloud this also lowers GPU-time cost.
#
# IMPORTANT -- VISION: your homework guardrail reads uploaded images and PDFs
# (screenshots, cropped diagrams). Those attachments only work if the model is
# VISION-CAPABLE. Text-only models (like gpt-oss) will REJECT a request that
# carries images with a 400 error. Vision-capable options include the Qwen-VL,
# Llama-Vision, and Gemma families. If you pick a text-only model, set
# MODEL_SUPPORTS_VISION = False below and attachments will be skipped cleanly
# (with a note to the student) instead of crashing.
if USE_CLOUD:
    # NOTE: Ollama model names are lowercase. Gemma 4 is vision-capable, so it
    # CAN read attached homework images/PDFs. Confirm the exact tag your Cloud
    # plan serves with `ollama ls` (signed in) -- the sizes are typically
    # e2b/e4b/e12b/e27b (and a 26b MoE). Use whatever tag `ollama ls` shows,
    # e.g. "gemma4:27b-cloud" or "gemma4:e27b".
    TUTOR_MODEL = "gemma4:cloud"   # <-- vision-capable cloud model for teaching
    GUARD_MODEL = "gemma4:cloud"   # <-- vision-capable so it can see attached homework images
    MODEL_SUPPORTS_VISION = True       # <-- Gemma 4 supports vision
else:
    TUTOR_MODEL = "qwen3.5:9b"       # <-- local model that answers students (qwen3.5 is multimodal)
    GUARD_MODEL = "qwen3.5:9b"       # <-- local guard (can be a smaller pulled model)
    MODEL_SUPPORTS_VISION = True     # <-- qwen3.5 supports vision; set False for a text-only local model
# ============================================================================

# Derived endpoint + auth header (do not edit)
OLLAMA_BASE_URL = CLOUD_BASE_URL if USE_CLOUD else LOCAL_BASE_URL

def _headers():
    """Auth header: Bearer key for cloud, nothing for local (local has no auth)."""
    if USE_CLOUD and OLLAMA_API_KEY:
        return {"Authorization": f"Bearer {OLLAMA_API_KEY}"}
    return {}

REQUEST_TIMEOUT_SECONDS = 180   # generation can be slow -- give it room

# ── performance knobs ────────────────────────────────────────────────────────
# keep_alive keeps the model resident so a returning student doesn't pay the
# model-reload cost. It's the biggest latency win LOCALLY.
#
# IMPORTANT: the value must be a Go duration WITH A UNIT (e.g. "-1m", "24h"),
# NOT a bare "-1". The local server tolerates a bare "-1", but Ollama CLOUD
# rejects it with: {"error": "time: missing unit in duration \"-1\""}. On cloud,
# residency is managed server-side, so keep_alive does nothing useful there --
# we therefore OMIT it entirely on cloud and only send it locally, with a unit.
#   "-1m" = any negative duration keeps the model loaded indefinitely (local).
KEEP_ALIVE = "-1m"

def _keep_alive_field():
    """Return {"keep_alive": ...} to merge into a payload -- but ONLY for local
    Ollama. On cloud we omit it (it's a no-op there and a bare/at-all value can
    be rejected), which avoids the 'missing unit in duration' 400."""
    return {} if USE_CLOUD else {"keep_alive": KEEP_ALIVE}

# Cap tokens for the short guard calls (supervisor/reviewer). They only return a
# tiny JSON verdict, so capping stops over-generation -- cuts latency locally,
# and on cloud directly cuts GPU-time cost since billing scales with duration.
GUARD_MAX_TOKENS = 256

# How many times to retry a call that fails with a transient error (503 "server
# overloaded" / 429 rate limit). Relevant mostly on cloud under classroom load.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 4   # base backoff; grows linearly per attempt


def _is_transient(error_text: str) -> bool:
    t = error_text.lower()
    return ("503" in t or "overloaded" in t or "429" in t
            or "rate" in t or "resource_exhausted" in t or "timeout" in t)


def _post(path, payload, stream=False):
    """POST to the Ollama API with auth + transient-error retries.
    Returns the requests.Response (caller handles body). Raises on final failure."""
    url = f"{OLLAMA_BASE_URL}{path}"
    # Server-side statuses worth retrying: overload / rate limit / gateway / a
    # generic 500 (cloud 500s are frequently transient and succeed on retry).
    RETRY_STATUSES = (429, 500, 502, 503, 504)
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=_headers(),
                                 timeout=REQUEST_TIMEOUT_SECONDS, stream=stream)
            # Retry on transient server-side statuses (with backoff)
            if resp.status_code in RETRY_STATUSES:
                try:
                    server_said = resp.json().get("error", "") or resp.text
                except Exception:
                    server_said = (resp.text or "")[:300]
                last_error = f"HTTP {resp.status_code}: {server_said}"
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                # Out of retries on a server error -- surface a clear message
                # with the server's own words instead of a bare stack trace.
                raise requests.RequestException(
                    f"Ollama Cloud returned {resp.status_code} for model "
                    f"'{payload.get('model')}' after {MAX_RETRIES} attempts. "
                    f"This is a server-side error on Ollama's end (not your "
                    f"request). Server said: {server_said or '(no detail)'}. "
                    f"Try again in a moment; if it persists, the model may be "
                    f"temporarily unavailable on your plan -- check "
                    f"https://ollama.com or try a different model.")
            # A 400 is a permanent request problem -- surface the server's OWN
            # error message plus useful diagnostics, instead of a bare stack
            # trace, so the real cause is visible rather than guessed at.
            if resp.status_code == 400:
                # Count images and estimate payload size to distinguish the
                # common causes: text-only model, oversized image payload, or a
                # bad model name.
                import json as _json
                image_count = sum(len(m.get("images", []))
                                  for m in payload.get("messages", []))
                try:
                    payload_kb = len(_json.dumps(payload)) // 1024
                except Exception:
                    payload_kb = -1
                try:
                    server_said = resp.json().get("error", "") or resp.text
                except Exception:
                    server_said = (resp.text or "")[:400]

                bits = [f"400 Bad Request from Ollama for model '{payload.get('model')}'."]
                bits.append(f"Server said: {server_said}")
                if image_count:
                    bits.append(f"(This request carried {image_count} image(s); "
                                f"payload ~{payload_kb} KB. If the server message mentions "
                                f"images/vision, the model can't read images -- use a "
                                f"vision model or set MODEL_SUPPORTS_VISION=False. If it "
                                f"mentions size/too large, the PDF rendered to too many/too "
                                f"big images. If it mentions the model, check the exact "
                                f"model name/tag.)")
                raise requests.RequestException(" ".join(bits))
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_error = str(e)
            if attempt < MAX_RETRIES and _is_transient(last_error):
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise
    # Exhausted retries without an exception
    raise requests.RequestException(f"Ollama request failed after {MAX_RETRIES} "
                                    f"attempts: {last_error}")
    # Exhausted retries on 429/503 without an exception
    raise requests.RequestException(f"Ollama request failed after {MAX_RETRIES} "
                                    f"attempts: {last_error}")


def _build_messages(system_instruction, contents):
    messages = [{"role": "system", "content": system_instruction}]
    for turn in contents:
        text = turn.get("text", "")
        images = []
        for attachment in turn.get("attachments", []) or []:
            images.extend(_attachment_to_base64_images(attachment))

        if images and not MODEL_SUPPORTS_VISION:
            # The configured model can't accept images. Rather than send them
            # (which returns a 400) or silently drop them, tell the model in
            # text that an attachment was provided but can't be read, so it can
            # respond helpfully instead of pretending it saw nothing.
            note = ("\n\n[NOTE: The student attached an image or PDF, but the "
                    "current model cannot read images. Ask them to describe or "
                    "type out the relevant contents so you can help.]")
            text = (text + note) if text else note.strip()
            images = []

        message = {"role": turn["role"], "content": text}
        if images:
            message["images"] = images
        messages.append(message)
    return messages


def generate(model, system_instruction, contents, temperature=0.2, json_mode=False,
             max_tokens=None):
    """The one function every other file calls to talk to the LLM.

    model:              a model name string -- pass TUTOR_MODEL or GUARD_MODEL.
    system_instruction: string -- the system prompt.
    contents:            a list of conversation turns (see module docstring).
    temperature:         float, 0.0-1.0.
    json_mode:           if True, ask the model to return ONLY valid JSON.
    max_tokens:          optional cap on generated tokens (used for guard calls).

    Returns the model's reply as a plain string. Works identically for local and
    cloud -- only the base URL + auth header differ.
    """
    options = {"temperature": temperature}
    if max_tokens is not None:
        options["num_predict"] = max_tokens

    payload = {
        "model": model,
        "messages": _build_messages(system_instruction, contents),
        "stream": False,
        "options": options,
        **_keep_alive_field(),   # only present for local Ollama, with a valid unit
    }
    if json_mode:
        payload["format"] = "json"   # Ollama's native "force valid JSON" mode

    response = _post("/api/chat", payload)
    text = response.json()["message"]["content"]
    return _strip_code_fences(text) if json_mode else text


def generate_stream(model, system_instruction, contents, temperature=0.2):
    """Streaming variant: yields text chunks as the model produces them.

    Used for the student-facing tutor reply so tokens appear as they're
    generated. On CLOUD this matters MORE than locally, because it hides the
    network round-trip latency -- the student sees text flowing immediately
    instead of waiting for a full request/response cycle.
    """
    payload = {
        "model": model,
        "messages": _build_messages(system_instruction, contents),
        "stream": True,
        "options": {"temperature": temperature},
        **_keep_alive_field(),   # only present for local Ollama, with a valid unit
    }

    with _post("/api/chat", payload, stream=True) as response:
        for line in response.iter_lines():
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            piece = obj.get("message", {}).get("content", "")
            if piece:
                yield piece
            if obj.get("done"):
                break


def _strip_code_fences(text):
    """Some models wrap JSON in ```json ... ``` even when told not to.
    Strip that before the caller's json.loads()."""
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    return match.group(1) if match else stripped


def _attachment_to_base64_images(attachment):
    """Turn one attachment dict into a list of base64-encoded image strings
    for Ollama's `images` field.

    Images pass straight through. PDFs are rendered to page images (every page,
    up to a generous safety cap) using PyMuPDF, so the model sees the ENTIRE
    document -- including embedded images, diagrams, or figures. NOTE: the
    TUTOR_MODEL/GUARD_MODEL you pick must be vision-capable for image/PDF
    attachments to be understood; text-only cloud models will ignore images."""
    data, mime_type = attachment["bytes"], attachment["mime_type"]
    if mime_type == "application/pdf":
        return _pdf_pages_to_base64_images(data)
    return [base64.b64encode(data).decode("ascii")]


def _pdf_pages_to_base64_images(pdf_bytes, max_pages=40):
    """Render every page of the PDF to an image so the vision model can read the
    whole document, embedded images included."""
    try:
        import fitz   # PyMuPDF -- pip install pymupdf
    except ImportError:
        return []
    images = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc[:max_pages]:
            pix = page.get_pixmap(dpi=170)
            images.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
    except Exception:
        return []
    return images


def check_connection():
    """Quick reachability check so a missing server / bad key produces a clear
    message instead of a stack trace. Returns (ok, info): info is the list of
    available model names on success, or an error string.

    LOCAL:  lists the models installed on your machine (/api/tags).
    CLOUD:  /api/tags may be empty for hosted-only accounts, so we treat a
            reachable+authorised endpoint as OK and report the configured models
            (there's nothing "installed" locally to list)."""
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags",
                            headers=_headers(), timeout=5)
        # A 401/403 on cloud means the key is missing/invalid -- surface clearly.
        if resp.status_code in (401, 403):
            return False, ("authentication failed -- check OLLAMA_API_KEY "
                           "(create one at https://ollama.com/settings/keys)")
        resp.raise_for_status()
        names = [m["name"] for m in resp.json().get("models", [])]
        if USE_CLOUD and not names:
            # Hosted accounts often return no local tags; that's fine.
            return True, [TUTOR_MODEL, GUARD_MODEL]
        return True, names
    except Exception as error:
        return False, str(error)


_warmed = False

def warm_up():
    """Preload the tutor model so the FIRST real student message isn't slow.
    LOCAL: loads the model into your GPU. CLOUD: effectively a no-op (residency
    is managed server-side) but harmless. Best-effort; never raises."""
    global _warmed
    if _warmed:
        return
    _warmed = True
    if USE_CLOUD:
        return   # nothing to warm on the client side for cloud
    try:
        requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            headers=_headers(),
            json={
                "model": TUTOR_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "keep_alive": KEEP_ALIVE,
                "options": {"num_predict": 1},
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except Exception:
        pass
