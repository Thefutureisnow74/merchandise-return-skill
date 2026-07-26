#!/usr/bin/env python3
"""image_text.py — read the text out of an image attachment (Blueprint M29).

Companion to pdf_text.py. Vendor/insurer decisions sometimes arrive as a photo or a
screenshot (a JPEG/PNG of a denial letter, a chat screenshot), not as selectable text.
pdf_text.py handles PDFs with an embedded text layer via fitz; this module handles RASTER
images, which need optical character recognition (OCR), not text extraction.

HOW THIS WORKS (verified in the container 2026-07-25):
    fitz (PyMuPDF) does NOT OCR, and PIL can decode pixels but cannot read letters. The
    container has no tesseract / pytesseract / easyocr installed. It DOES, however, have
    the openai python lib plus GROQ_API_KEY and GOOGLE_GEMINI_API_KEY in /opt/data/.env,
    and both back vision-capable models. So OCR is done by routing the image bytes to a
    VISION LLM: base64-encode the image and ask the model to transcribe all text verbatim.
    This needs no new system binary and is the lightest path.

    Backends, tried in order (first non-empty result wins):
      1) Google Gemini (REST, GOOGLE_GEMINI_API_KEY) — clean, accurate transcription.
      2) Groq  (OpenAI-compatible, GROQ_API_KEY)      — qwen vision (a reasoning model).
    Order is overridable with the OCR_VISION_ORDER env var (comma list, e.g. "groq,gemini").

    If a real local OCR engine (tesseract/easyocr) is ever installed, it is used FIRST and
    for free — this module detects it with no code change. If neither a local engine nor a
    working vision backend is available, extract_text() raises OCRUnavailableError with the
    exact fix. It never silently returns "" (that would make a denial screenshot look like
    an empty attachment to the classifier).

Usage in the pipeline (mirrors pdf_text.py):
    import sys; sys.path.insert(0, "/opt/data/scripts")
    from image_text import extract_text, OCRUnavailableError
    try:
        text = extract_text(image_bytes)
    except OCRUnavailableError as e:
        # no OCR path worked — log the exact reason, do NOT treat the attachment as empty.
        ...
"""
import base64
import io
import json
import os
import re
import urllib.request


class OCRUnavailableError(RuntimeError):
    """Raised when no OCR path works, so image text cannot be read.

    The message states the smallest concrete fix so the caller/log is actionable.
    """


REMEDIATION = (
    "Could not read text from the image: no local OCR engine is installed AND no vision "
    "LLM backend succeeded. image_text.py can decode the image (PIL/fitz are present) but "
    "reading the letters needs either an OCR engine or a vision model.\n"
    "Paths, cheapest first:\n"
    "  1) Vision LLM (no new binary; keys already in the container env): set a working "
    "GOOGLE_GEMINI_API_KEY (Gemini) or GROQ_API_KEY (Groq vision) in /opt/data/.env. "
    "This is the primary path this module uses; check the per-backend error above.\n"
    "  2) pytesseract: `pip install pytesseract` PLUS the tesseract binary "
    "(`apt-get install -y tesseract-ocr` — a system package, needs root in the container).\n"
    "  3) easyocr: `pip install easyocr` (pure-pip, but pulls in torch — a large download)."
)

# Vision prompt — ask for a verbatim transcription and nothing else.
_TRANSCRIBE_PROMPT = (
    "Transcribe ALL text visible in this image, verbatim and in reading order. "
    "Preserve line breaks. Output ONLY the transcribed text with no commentary, "
    "no explanation, and no markdown fences."
)

# Reasoning models (e.g. Groq qwen) wrap their scratchpad in <think>...</think>.
# Strip it so only the transcription remains.
_THINK_BLOCK = re.compile(r"(?is)<think>.*?</think>")
_DANGLING_THINK = re.compile(r"(?is)^.*?</think>")


def _strip_think(text):
    """Remove <think>...</think> reasoning scratchpad from a model reply."""
    if not text:
        return ""
    out = _THINK_BLOCK.sub("", text)
    if "</think>" in out:  # opening tag lost but closing tag present
        out = _DANGLING_THINK.sub("", out)
    return out.strip()


# ---------------------------------------------------------------------------
# Local OCR engines (used first & free if ever installed; return None if absent)
# ---------------------------------------------------------------------------
def _try_pytesseract(image_bytes):
    """OCR via tesseract if both pytesseract and the tesseract binary are available."""
    import importlib
    if importlib.util.find_spec("pytesseract") is None:
        return None
    import pytesseract
    from PIL import Image
    try:
        # Confirms the tesseract *binary* is actually on PATH, not just the python wrapper.
        pytesseract.get_tesseract_version()
    except Exception:
        return None
    img = Image.open(io.BytesIO(image_bytes))
    return (pytesseract.image_to_string(img) or "").strip()


def _try_easyocr(image_bytes):
    """OCR via easyocr if it is installed."""
    import importlib
    if importlib.util.find_spec("easyocr") is None:
        return None
    import easyocr
    reader = easyocr.Reader(["en"], gpu=False)
    lines = reader.readtext(image_bytes, detail=0)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Vision LLM backends
# ---------------------------------------------------------------------------
def _detect_mime(image_bytes):
    """Best-effort image MIME sniff from magic bytes; defaults to image/png."""
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


# Model candidates per backend. First that responds with usable text wins.
_GEMINI_MODELS = ("gemini-flash-latest", "gemini-2.0-flash", "gemini-1.5-flash")
_GROQ_VISION_MODELS = ("qwen/qwen3.6-27b",)


def _try_gemini_vision(image_bytes):
    """OCR via Google Gemini vision (REST). Returns text, or None if key/models unavailable.

    Raises on hard errors only after all candidate models fail; caller treats an exception
    as 'this backend did not work' and moves on.
    """
    key = os.environ.get("GOOGLE_GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    b64 = base64.b64encode(image_bytes).decode()
    mime = _detect_mime(image_bytes)
    body = {
        "contents": [{"parts": [
            {"text": _TRANSCRIBE_PROMPT},
            {"inline_data": {"mime_type": mime, "data": b64}},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 4096},
    }
    last_err = None
    for model in _GEMINI_MODELS:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                j = json.load(resp)
            parts = j["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            text = _strip_think(text)
            if text:
                return text
            last_err = RuntimeError(f"{model}: empty response")
        except Exception as e:  # 404 (no such model), 429 (quota), etc. → try next model
            last_err = RuntimeError(f"{model}: {e}")
            continue
    if last_err:
        raise last_err
    return None


def _try_groq_vision(image_bytes):
    """OCR via Groq's OpenAI-compatible vision endpoint. Returns text, or None if no key."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    import importlib
    if importlib.util.find_spec("openai") is None:
        return None
    import openai
    client = openai.OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
    b64 = base64.b64encode(image_bytes).decode()
    mime = _detect_mime(image_bytes)
    data_url = f"data:{mime};base64,{b64}"
    last_err = None
    for model in _GROQ_VISION_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=4096,  # generous: reasoning models spend tokens before the answer
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": _TRANSCRIBE_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]}],
            )
            text = _strip_think(resp.choices[0].message.content or "")
            if text:
                return text
            last_err = RuntimeError(f"{model}: empty response")
        except Exception as e:  # model_not_found, etc. → try next
            last_err = RuntimeError(f"{model}: {e}")
            continue
    if last_err:
        raise last_err
    return None


_VISION_BACKENDS = {
    "gemini": _try_gemini_vision,
    "groq": _try_groq_vision,
}
# Default order: Gemini first (clean, accurate on the container's test), then Groq.
# Override with OCR_VISION_ORDER="groq,gemini" if desired.
_DEFAULT_VISION_ORDER = ("gemini", "groq")


def _vision_order():
    raw = os.environ.get("OCR_VISION_ORDER", "")
    order = [p.strip().lower() for p in raw.split(",") if p.strip()]
    order = [p for p in order if p in _VISION_BACKENDS]
    return tuple(order) if order else _DEFAULT_VISION_ORDER


def ocr_engine_available():
    """Return the name of an available LOCAL OCR engine, or None. Cheap, no OCR performed."""
    import importlib
    if importlib.util.find_spec("pytesseract") is not None:
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            return "pytesseract"
        except Exception:
            pass
    if importlib.util.find_spec("easyocr") is not None:
        return "easyocr"
    return None


def vision_backend_available():
    """Return a list of vision backends that have a key configured. No network call."""
    out = []
    for name in _vision_order():
        if name == "gemini" and (os.environ.get("GOOGLE_GEMINI_API_KEY")
                                 or os.environ.get("GOOGLE_API_KEY")):
            out.append("gemini")
        elif name == "groq" and os.environ.get("GROQ_API_KEY"):
            out.append("groq")
    return out


def extract_text(image_bytes):
    """Return the text read out of an image given its raw bytes.

    Order of attempts (first non-empty result wins):
      1) local OCR engines (tesseract, easyocr) — used only if installed; free.
      2) vision LLM backends (Gemini, Groq) — the primary path in this container.
    If nothing works, raises OCRUnavailableError with the exact remediation and the
    per-backend errors collected along the way — it does NOT silently return "".
    """
    errors = []

    # 1) free local engines, if ever installed
    for fn in (_try_pytesseract, _try_easyocr):
        try:
            result = fn(image_bytes)
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
            result = None
        if result:
            return result

    # 2) vision LLM backends
    for name in _vision_order():
        fn = _VISION_BACKENDS[name]
        try:
            result = fn(image_bytes)
        except Exception as e:
            errors.append(f"{name}: {e}")
            result = None
        if result:
            return result
        if result is None and f"{name}:" not in " ".join(errors):
            errors.append(f"{name}: no API key configured")

    detail = REMEDIATION
    if errors:
        detail += "\n\nBackend errors this run:\n  - " + "\n  - ".join(errors)
    raise OCRUnavailableError(detail)


if __name__ == "__main__":
    # self-test: synthesize a PNG that mimics a denial screenshot, then read it back.
    # PIL is present in the container, so we can build the test image regardless of OCR.
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        print("PIL not available, cannot run self-test:", e)
        raise SystemExit(1)

    img = Image.new("RGB", (620, 110), "white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), "AMEX Assurance Company  Claim 12393058", fill="black")
    draw.text((10, 45), "Outcome: your refund request was DENIED.", fill="black")
    draw.text((10, 78), "Reason: DENIED - out of time.", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    print(f"synthesized test PNG: {len(data)} bytes")

    print("local OCR engine available:", ocr_engine_available() or "NONE")
    print("vision backends with keys:", vision_backend_available() or "NONE")

    try:
        txt = extract_text(data)
    except OCRUnavailableError as e:
        print("RESULT: OCRUnavailableError raised (no OCR path worked).")
        print("---")
        print(e)
        raise SystemExit(1)

    print("extracted:", repr(txt[:240]))
    up = txt.upper()
    if "DENIED" in up and "12393058" in txt.replace(" ", ""):
        print("PASS — image attachments are now readable; a decision in a photo is visible.")
    else:
        print("WARN — a backend ran but the expected text was not recovered cleanly.")
        raise SystemExit(2)
