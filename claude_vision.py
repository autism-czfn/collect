"""
Vision analysis helper for food photo nutrition extraction.

Uses `claude -p` with the Read tool and a temp file — same auth pattern
as every other LLM call in this codebase (no API key required).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile

log = logging.getLogger(__name__)

NUTRITION_SYSTEM_PROMPT = """\
You are a nutrition analyst helping a caregiver log meals for an autistic child.
Read the image file provided and analyze the meal photo.
Return a single JSON object with EXACTLY these fields:

{
  "foods_identified": ["string"],
  "estimated_calories": integer or null,
  "macros": {
    "protein_g": float or null,
    "carbs_g": float or null,
    "fat_g": float or null,
    "fiber_g": float or null
  },
  "sensory_notes": "string or null",
  "concerns": "string or null",
  "confidence": "high" | "medium" | "low"
}

Guidelines:
- foods_identified: list every distinct food item visible
- estimated_calories: total for the whole plate/meal shown; null if unclear
- macros: per-macro estimates in grams; null for any you cannot estimate
- sensory_notes: texture variety, colour uniformity, food separation — relevant
  to sensory sensitivities (e.g. "foods touching", "all beige", "crunchy textures")
- concerns: allergies, highly processed items, additives worth flagging; null if none
- confidence: high = foods clearly identifiable; medium = partially obscured;
  low = cannot reliably identify what was eaten
- Do NOT hallucinate calorie or macro values — prefer null over a guess
- Return ONLY the JSON object, no markdown, no explanation\
"""

_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png":  ".png",
    "image/heic": ".heic",
    "image/webp": ".webp",
}


def analyze_food_photo(image_bytes: bytes, mime_type: str) -> dict:
    """Write image to a temp file, call `claude -p` with Read tool, return validated dict."""
    ext = _MIME_TO_EXT.get(mime_type, ".jpg")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(image_bytes)
        tmp_path = f.name

    try:
        prompt = (
            f"{NUTRITION_SYSTEM_PROMPT}\n\n"
            f"Now read and analyze the meal photo at: {tmp_path}"
        )
        result = subprocess.run(
            [
                "claude", "-p",
                "--add-dir", os.path.dirname(tmp_path),
                "--tools", "Read",
                "--disable-slash-commands",
                prompt,
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "claude -p exited non-zero")

        raw_text = result.stdout.strip()
        log.info(f"claude -p vision raw output ({len(raw_text)} chars)")

        # Strip markdown fences if Claude wraps the JSON
        if "```" in raw_text:
            fence_start = raw_text.index("```")
            after_fence = raw_text[fence_start + 3:]
            if "\n" in after_fence:
                after_fence = after_fence[after_fence.index("\n") + 1:]
            if "```" in after_fence:
                after_fence = after_fence[:after_fence.rfind("```")].strip()
            raw_text = after_fence
        else:
            brace_start = raw_text.find("{")
            brace_end   = raw_text.rfind("}")
            if brace_start != -1 and brace_end != -1:
                raw_text = raw_text[brace_start:brace_end + 1]

        raw_dict = json.loads(raw_text)
        return _validate_food_response(raw_dict)

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _validate_food_response(raw: dict) -> dict:
    """Clamp and validate LLM nutrition output."""
    cal = raw.get("estimated_calories")
    if cal is not None:
        try:
            cal = int(cal)
            cal = cal if 0 <= cal <= 5000 else None
        except (TypeError, ValueError):
            cal = None
    raw["estimated_calories"] = cal

    macros = raw.get("macros")
    if not isinstance(macros, dict):
        macros = {}
    for key in ("protein_g", "carbs_g", "fat_g", "fiber_g"):
        v = macros.get(key)
        if v is not None:
            try:
                v = float(v)
                macros[key] = v if v >= 0 else None
            except (TypeError, ValueError):
                macros[key] = None
    raw["macros"] = macros

    if raw.get("confidence") not in ("high", "medium", "low"):
        raw["confidence"] = "low"

    foods = raw.get("foods_identified")
    if not isinstance(foods, list):
        foods = []
    raw["foods_identified"] = [str(f) for f in foods if f]

    for field in ("sensory_notes", "concerns"):
        v = raw.get(field)
        raw[field] = str(v).strip() if v else None

    return raw
