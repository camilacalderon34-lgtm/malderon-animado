"""
AI prompt generation service — batch image & video prompts via OpenRouter.

Uses OpenRouter (Gemini) for all AI calls, same as claude_service.py.
Image generation itself is handled by Pollinations (pollinations_service.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from openai import OpenAI
from ..config import settings

# ── OpenRouter client (shared config with claude_service) ────────────────────

_client = OpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
)
_MODEL_FAST = "google/gemini-2.0-flash-lite-001"


def _chat(system: str, user: str, max_tokens: int = 4096) -> str:
    resp = _client.chat.completions.create(
        model=_MODEL_FAST,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = resp.choices[0].message.content
    if content is None:
        raise RuntimeError("OpenRouter returned empty content (None)")
    return content.strip()


# ── Batch Image Prompt Generation ────────────────────────────────────────────

_BATCH_PROMPT_SYSTEM = """You are a CINEMATOGRAPHER creating SHORT image prompts for AI video generation.

=== BEFORE WRITING EACH PROMPT ===
Read the scene narration and ask yourself:
1. Is this about an ancient/biblical event? → Use the VISUAL STYLE setting.
2. Is this a modern fact, statistic, or present-day concept? → Use a MODERN setting (hospitals, cities, technology, monitors, modern people, etc.)
3. Is this abstract/emotional? → Use symbolic imagery (close-ups, textures, light/shadow, empty spaces).

The VISUAL STYLE is the DEFAULT look, but you MUST override it when the narration demands a different era or context.

=== CORE RULES ===
- 40-60 words per prompt. No exceptions.
- Do NOT copy any text from the VISUAL STYLE into your prompts. Internalize it, don't paste it.
- WHO + WHAT + WHERE + CAMERA + LIGHTING in every prompt.
- Vary compositions: wide/close, interior/exterior, day/night.
- NO text, watermarks, logos, borders.

Return ONLY valid JSON — no markdown fences, no extra text."""

_BATCH_PROMPT_TEMPLATE = """Generate SHORT image prompts (40-60 words each) for these scenes.

══════════════════════════════════════
VISUAL STYLE (internalize — do NOT copy into prompts):
══════════════════════════════════════
{reference_style}

══════════════════════════════════════
FULL SCRIPT (for narrative context):
══════════════════════════════════════
{full_script}

══════════════════════════════════════
SCENES:
══════════════════════════════════════
{scenes_block}

RULES:
- 40-60 words per prompt. No exceptions.
- WHO + WHAT + WHERE + CAMERA + LIGHTING in every prompt.
- Do NOT copy the visual style text. Use your own words inspired by it.
- Do NOT include any character anchor text — it is added automatically.
- Vary composition across scenes.

Return JSON:
{{
  "prompts": [
    {{"scene_number": 1, "image_prompt": "A concise 40-60 word cinematic description."}},
    ...
  ]
}}"""

# Max words before we chunk the batch into groups of 10
_MAX_SCRIPT_WORDS_SINGLE_BATCH = 3000
_SCENES_PER_BATCH = 10

# ── Prompt quality filter: block modern/anachronistic content ─────────────────
_BANNED_WORDS = [
    "watermark", "logo", "border", "text overlay", "subtitle",
    "stock photo", "shutterstock", "getty",
]


def _has_banned_words(prompt: str) -> list[str]:
    """Return list of banned words found in a prompt."""
    lower = prompt.lower()
    return [w for w in _BANNED_WORDS if w.lower() in lower]


def _clean_prompt(prompt: str, visual_style: str, max_words: int = 80) -> str:
    """Remove visual_style contamination from a prompt and enforce word limit."""
    import difflib

    if not visual_style or not prompt:
        return prompt

    # 1. Sentence-level dedup: remove prompt sentences too similar to visual_style
    style_sentences = [s.strip() for s in re.split(r'[.!]\s+', visual_style) if len(s.strip()) > 10]
    prompt_sentences = [s.strip() for s in re.split(r'(?<=[.!])\s+', prompt) if s.strip()]

    kept = []
    for ps in prompt_sentences:
        is_copy = False
        for ss in style_sentences:
            ratio = difflib.SequenceMatcher(None, ps.lower(), ss.lower()).ratio()
            if ratio > 0.6:
                is_copy = True
                break
        if not is_copy:
            kept.append(ps)

    cleaned = " ".join(kept)

    # 2. Fragment removal: remove 5+ word verbatim fragments from visual_style
    style_words = visual_style.split()
    for window_size in range(min(8, len(style_words)), 4, -1):
        for i in range(len(style_words) - window_size + 1):
            fragment = " ".join(style_words[i:i + window_size])
            if fragment.lower() in cleaned.lower():
                cleaned = re.sub(re.escape(fragment), "", cleaned, flags=re.IGNORECASE)

    # 3. Clean whitespace artifacts
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    cleaned = re.sub(r'^[,.\s]+', '', cleaned)

    # 4. Word limit enforcement with intelligent truncation
    words = cleaned.split()
    if len(words) > max_words:
        truncated = " ".join(words[:max_words])
        last_period = truncated.rfind(".")
        if last_period > len(truncated) // 2:
            truncated = truncated[:last_period + 1]
        cleaned = truncated

    return cleaned.strip()


def batch_generate_image_prompts(
    scenes: list[dict],
    reference_character: str = "",
    full_script: str = "",
    visual_style: str = "",
) -> dict[int, str]:
    """Send scenes + full script context to Gemini and return {scene_number: prompt}.

    If the script is long (>3000 words), scenes are processed in batches of 10
    but the full script is always included for context.
    Includes automatic validation: any prompt with modern/anachronistic words
    is regenerated up to 2 times.
    """
    style = visual_style or reference_character or "photorealistic cinematic, dramatic lighting, shallow depth of field, film grain, 16:9 widescreen"
    print(f"[ImagePrompts] Visual style: {style[:80]}...")


    script_text = (full_script or "").strip()
    if not script_text:
        script_text = "(No full script provided — use each scene's narration as context.)"

    # Truncate script to ~4000 words max to avoid token limits
    script_words = script_text.split()
    if len(script_words) > 4000:
        script_text = " ".join(script_words[:4000]) + "\n\n[... script truncated for length ...]"

    word_count = len(script_words)
    need_chunking = word_count > _MAX_SCRIPT_WORDS_SINGLE_BATCH and len(scenes) > _SCENES_PER_BATCH

    if need_chunking:
        # Process in batches of 10 scenes, each batch gets the full script
        all_results: dict[int, str] = {}
        for i in range(0, len(scenes), _SCENES_PER_BATCH):
            batch = scenes[i:i + _SCENES_PER_BATCH]
            print(f"[ImagePrompts] Batch {i // _SCENES_PER_BATCH + 1}: scenes {batch[0]['scene_number']}-{batch[-1]['scene_number']}")
            batch_result = _generate_batch(batch, style, script_text)
            all_results.update(batch_result)
    else:
        all_results = _generate_batch(scenes, style, script_text)

    # ── Validate & retry bad prompts (up to 2 retries) ────────────────────────
    scenes_by_num = {s["scene_number"]: s for s in scenes}
    for retry in range(2):
        bad_nums = [n for n, p in all_results.items() if _has_banned_words(p)]
        if not bad_nums:
            break
        bad_scenes = [scenes_by_num[n] for n in bad_nums if n in scenes_by_num]
        if not bad_scenes:
            break
        print(f"[ImagePrompts] Retry {retry + 1}: {len(bad_nums)} prompts with banned words {bad_nums}")
        retry_results = _generate_batch(bad_scenes, style, script_text)
        all_results.update(retry_results)

    # Log any still-bad prompts (but don't block)
    still_bad = {n: _has_banned_words(p) for n, p in all_results.items() if _has_banned_words(p)}
    if still_bad:
        for n, words in still_bad.items():
            print(f"[ImagePrompts] WARNING: scene {n} still has banned words after retries: {words}")

    return all_results


def _generate_batch(scenes: list[dict], style: str, script_text: str) -> dict[int, str]:
    """Generate image prompts for a batch of scenes with full script context."""
    scenes_block = "\n".join(
        f"Scene {s['scene_number']}:\n"
        f"  Narration: {s['narration'][:400]}"
        for s in scenes
    )

    prompt = _BATCH_PROMPT_TEMPLATE.format(
        reference_style=style,
        full_script=script_text,
        scenes_block=scenes_block,
    )

    raw = _chat(_BATCH_PROMPT_SYSTEM, prompt, max_tokens=8192)
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)

    results = {int(item["scene_number"]): item["image_prompt"] for item in data["prompts"]}

    # ── Post-process: clean visual_style contamination and enforce word limit ──
    for scene_num in results:
        original_wc = len(results[scene_num].split())
        results[scene_num] = _clean_prompt(results[scene_num], style)
        new_wc = len(results[scene_num].split())
        if original_wc != new_wc:
            print(f"[ImagePrompts] Scene {scene_num}: {original_wc} -> {new_wc} words (cleaned)")

    # character_anchor removed — Gemini handles character descriptions contextually

    return results


# ── Batch Video Prompt Generation (Motion Instructions) ──────────────────────

_BATCH_VIDEO_SYSTEM = """You are an AI video motion director.
Write extremely concise, literal MOTION instructions for the LTX Video AI model based on the scene's narration.
Do NOT describe the setting or subject (that's the image prompt's job).
ONLY describe the camera movement, action, and physics. Max 10-15 words per scene.
Examples:
- "Slow pan right across the room, soft dust particles floating."
- "Fast zoom into reporter's face, wind blowing hair."
- "Subtle camera shake, character turns head slowly to the left."
Return ONLY valid JSON."""

_BATCH_VIDEO_TEMPLATE = """Generate short motion/animation instructions for each scene.

Scenes:
{scenes_block}

Return JSON:
{{
  "prompts": [
    {{
      "scene_number": 1,
      "video_prompt": "Camera pushes in slowly, subtle breathing movement."
    }},
    ...
  ]
}}"""

def batch_generate_video_prompts(
    scenes: list[dict],
) -> dict[int, str]:
    """Send all scenes to generate LTX motion instructions via OpenRouter."""
    scenes_block = "\n".join(
        f"Scene {s['scene_number']}:\n"
        f"  Narration (what's happening): {s['narration'][:300]}\n"
        f"  Visual Setting (already known): {s.get('image_prompt', '')[:150]}"
        for s in scenes
    )

    prompt = _BATCH_VIDEO_TEMPLATE.format(scenes_block=scenes_block)
    raw = _chat(_BATCH_VIDEO_SYSTEM, prompt, max_tokens=4096)
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)

    return {int(item["scene_number"]): item["video_prompt"] for item in data["prompts"]}


# ── Image generation — Imagen 3.0 (kept for compatibility but Pollinations is primary) ──

def generate_image(
    prompt: str,
    output_path: Path,
    aspect_ratio: str = "16:9",
    safety_filter_level: str = "block_only_high",
    person_generation: str = "allow_adult",
) -> Path:
    """Generate one image with Imagen 3.0 (requires GOOGLE_API_KEY with credits)."""
    from google import genai
    from google.genai import types

    key = settings.google_api_key
    if not key:
        raise RuntimeError("GOOGLE_API_KEY no configurado.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=key)
    response = client.models.generate_images(
        model="imagen-3.0-generate-002",
        prompt=prompt,
        config=types.GenerateImageConfig(
            number_of_images=1,
            aspect_ratio=aspect_ratio,
            safety_filter_level=safety_filter_level,
            person_generation=person_generation,
        ),
    )

    if not response.generated_images:
        raise RuntimeError("Google Imagen 3 no devolvió imágenes.")

    image_bytes = response.generated_images[0].image.image_bytes
    if not image_bytes:
        raise RuntimeError("Google Imagen 3: imagen recibida pero sin bytes.")

    output_path.write_bytes(image_bytes)
    print(f"[Google Imagen] Imagen guardada: {output_path} ({len(image_bytes):,} bytes)")
    return output_path


# ── Video animation — Veo (stub) ──────────────────────────────────────────────

def animate_image(
    image_path: Path,
    output_path: Path,
    prompt: str = "",
) -> Path:
    """Stub — Veo video generation not yet implemented."""
    import shutil
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(image_path), str(output_path))
    print(f"[Google Veo] STUB — imagen copiada como video estático: {output_path}")
    return output_path
