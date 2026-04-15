"""AI service - script generation, image prompts, keyword extraction.
Uses OpenRouter (OpenAI-compatible) for all AI calls.
"""
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional
from openai import OpenAI as _OpenAI
from ..config import settings

# ── Numbered entry detection (for countdown/list videos) ─────────────────────
# Matches: "Number 1,", "Number 10.", "Number one.", "#1 ", "#10:", "10. ", "1. ", etc.
_WORD_NUMBERS = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen)"
_RE_NUMBERED = re.compile(
    rf"^\s*(?:Number\s+(?:\d+|{_WORD_NUMBERS})|#\s*\d+|\d{{1,2}}\.)\s*[,:.]?\s",
    re.IGNORECASE,
)
# Matches through the title portion (everything up to first period or colon after number+title)
_RE_TITLE_END = re.compile(
    rf"^\s*(?:Number\s+(?:\d+|{_WORD_NUMBERS})|#\s*\d+|\d{{1,2}}\.)\s*[,:.]?\s*[^.:]+[.:]",
    re.IGNORECASE,
)


_RE_NUMBER_LABEL = re.compile(
    rf"Number\s+(?:\d+|{_WORD_NUMBERS})\s*[,:.]\s*",
    re.IGNORECASE,
)


def _is_numbered_entry(text: str) -> bool:
    """Check if text starts with a numbered list pattern (Number X, #X, X.)."""
    return bool(_RE_NUMBERED.match(text))


def _find_title_end(text: str) -> int | None:
    """Find where a numbered entry's title ends (first . or : after the title phrase).

    Returns the character index right after the title-ending punctuation,
    or None if no clear title boundary is found.
    """
    m = _RE_TITLE_END.match(text)
    if m and len(text) > m.end() and text[m.end():].strip():
        return m.end()
    return None


def _safe_print(msg: str) -> None:
    """Print that won't crash on Windows when stdout is invalid/piped."""
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass

# OpenRouter client (OpenAI-compatible)
_openrouter = _OpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
)

# Model aliases (OpenRouter format) — using Gemini for cost efficiency
_MODEL_FAST  = "google/gemini-2.0-flash-lite-001"     # cheap + fast (JSON tasks, image prompts)
_MODEL_SMART = "google/gemini-2.5-flash"              # quality (scripts, editing)


def _chat(system: str, user: str, model: str = _MODEL_SMART, max_tokens: int = 8192) -> str:
    """Call AI via OpenRouter API."""
    for attempt in range(1, 4):
        try:
            resp = _openrouter.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            text = resp.choices[0].message.content
            if not text or not text.strip():
                raise RuntimeError("OpenRouter returned empty output")
            return text.strip()
        except Exception as e:
            _safe_print(f"[OpenRouter] Error attempt {attempt}/3: {str(e)[:200]}")
            import time; time.sleep(2)
    raise RuntimeError("OpenRouter API failed after 3 attempts")

# ── Root path (two levels up from this file: app/services/ → root) ────────────
_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Duration config ────────────────────────────────────────────────────────────

# Scene count range per duration (each scene ~80-100 words / 25-30 sec)
DURATION_SCENES = {
    "6-8":   (15, 20),
    "10-12": (25, 30),
    "18-20": (45, 55),
    "30-40": (75, 95),
}

# Talking point ranges for outline generation
DURATION_TALKING_POINTS = {
    "6-8":   (4, 5),
    "10-12": (6, 12),
    "18-20": (12, 18),
    "30-40": (20, 30),
}

# Target word counts per duration
DURATION_WORD_COUNTS = {
    "6-8":   (900, 1200, 8),
    "10-12": (1500, 1800, 12),
    "18-20": (2500, 3000, 20),
    "30-40": (4500, 6000, 40),
}


# ── Prompt guide files ─────────────────────────────────────────────────────────

def _read_guide(filename: str) -> str:
    """Read a .txt guide file from the project root. Returns empty string if missing."""
    path = _ROOT / filename
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


# ── Main script generation (single-call pipeline) ─────────────────────────────

def generate_script_full(
    title: str,
    transcripts: Optional[List[dict]] = None,
    video_type: str = "top10",
    duration: str = "6-8",
) -> str:
    """
    Single-call pipeline:
      1. Internally generates an outline (not shown to user)
      2. Expands it into the final narration script
    Returns ONLY the final narration script, plain text, ready for TTS.
    """
    # ── Load guide files ───────────────────────────────────────────────────────
    promptguide = _read_guide("promptguide.txt")
    if video_type == "documental":
        style_guide = _read_guide("documentary.txt")
    else:
        style_guide = _read_guide("top10style.txt")

    # ── Talking points range ───────────────────────────────────────────────────
    tp_min, tp_max = DURATION_TALKING_POINTS.get(duration, (4, 5))
    n_points = (tp_min + tp_max) // 2  # pick middle value

    # ── Word count target ──────────────────────────────────────────────────────
    min_w, max_w, dur_min = DURATION_WORD_COUNTS.get(duration, (900, 1200, 8))

    # ── Build system prompt ────────────────────────────────────────────────────
    system_parts = [
        "You are an expert YouTube video scriptwriter.",
        "",
        "=== VIDRUSH PROMPTING GUIDE ===",
        promptguide,
        "",
        "=== VIDEO STYLE GUIDE ===",
        style_guide,
        "",
        "=== OUTPUT RULES (STRICTLY ENFORCED) ===",
        "- Return ONLY the narration script as clean, flowing text.",
        "- Do NOT include scene markers, numbering, or any segmentation.",
        "- Do NOT include the outline, talking points header, or any labels.",
        "- Do NOT include: 'NARRATOR:', 'Scene:', '[Music]', '[Pause]', timestamps.",
        "- Do NOT use bold (**), italic (*), or headers (#).",
        "- The output must be pure spoken narration, ready for text-to-speech directly.",
        f"- Target length: {min_w} to {max_w} words (approximately {dur_min} minutes).",
    ]
    system_prompt = "\n".join(system_parts)

    # ── Build transcripts block ────────────────────────────────────────────────
    transcript_block = ""
    if transcripts:
        parts = []
        for t in transcripts:
            title_ref = t.get("title", "Reference Video")
            text = t.get("transcript", "").strip()
            if text:
                parts.append(f"Video reference: {title_ref}\n{text}")
        if parts:
            transcript_block = (
                "\n\n=== REFERENCE TRANSCRIPTS ===\n"
                "Use the following transcripts as style and structure references ONLY. "
                "Do NOT copy content from them.\n\n"
                + "\n\n---\n\n".join(parts)
            )

    # ── Build user prompt ──────────────────────────────────────────────────────
    video_type_label = "Top 10 countdown" if video_type != "documental" else "documentary"
    user_prompt = f"""VIDEO TITLE: {title}
VIDEO TYPE: {video_type_label}
VIDEO DURATION: {dur_min} minutes (~{min_w}-{max_w} words)
{transcript_block}

TASK:
Step 1 (internal only - DO NOT output): Generate an outline with {n_points} detailed talking points about "{title}". Use the reference transcripts (if provided) as a guide for structure and style.

Step 2 (this is what you return): Using the outline you just created internally, write the complete final narration script for this video. Follow all the style guides and output rules above.

RETURN ONLY THE NARRATION SCRIPT. Clean flowing text, no markers, no scene numbers. Nothing else."""

    # ── Call AI via OpenRouter ─────────────────────────────────────────────────
    return _chat(system_prompt, user_prompt, model=_MODEL_SMART, max_tokens=8192)


# ── Image prompt generation ────────────────────────────────────────────────────

def _build_image_system_prompt(style: str = "") -> str:
    """Build a system prompt adapted to the project's visual style."""
    if not style:
        style = "cinematic, photorealistic"

    return f"""You are a visual prompt engineer for cinematic biblical AI image generation.
Create detailed image prompts for YouTube videos in this style: {style}

=== MANDATORY VISUAL STYLE (apply to EVERY prompt) ===
- PHOTOREALISTIC biblical cinema, like "The Chosen" TV series or Ridley Scott's "Exodus"
- Color palette: warm earth tones (ochre, sienna, gold, bronze), deep shadows, golden hour lighting
- Textures: weathered stone, rough linen/wool fabrics, clay pottery, dusty sandstone
- People MUST wear period-accurate ancient Middle Eastern clothing: linen tunics, woolen cloaks, leather sandals, head coverings
- Architecture: mud-brick walls, stone columns, wooden beams, oil lamps, torch-lit interiors
- Lighting: dramatic chiaroscuro — warm oil lamp glow indoors, golden sunset/sunrise outdoors, god-rays through windows or clouds
- Camera: cinematic 16:9 widescreen, shallow depth of field, film grain

=== SCENE VARIETY (rotate between these) ===
1. PEOPLE SCENES: biblical figures interacting, crowds in marketplaces, workers building, soldiers, priests, families
2. CITY/ARCHITECTURE: ancient cities (Babylon, Jerusalem, Egypt), massive walls, temples, ziggurats, gates, aqueducts
3. LANDSCAPE: desert valleys, olive groves, rivers (Euphrates, Jordan), mountains at sunset, starry night skies
4. INTERIOR: stone rooms lit by oil lamps, workshops, throne rooms, synagogues, caves with warm light
5. CLOSE-UPS: hands working clay/stone, ancient scrolls, herbs/spices, bread, wine, tools, weapons
6. DRAMATIC: storms, fire, divine light breaking through clouds, armies, processions

=== RULES ===
- NEVER repeat the same composition or subject type twice in a row
- VARY camera angles: wide establishing, medium two-shot, close-up portrait, overhead, low-angle dramatic
- Every prompt MUST include people unless it's specifically a landscape or object close-up
- NO text, watermarks, logos, modern elements
- NO generic "old man preparing herbs" — each scene must be UNIQUE and specific to the narration

Return ONLY valid JSON - no markdown fences, no extra text."""

IMAGE_PROMPT_TEMPLATE = """Create a unique cinematic biblical image prompt that SPECIFICALLY illustrates this narration:

Narration: {narration}
Visual context: {visual_description}
Style: {reference_character}

IMPORTANT: The image must directly depict what the narration describes.
Analyze the narration carefully and create a scene that a viewer would immediately connect to these words.

STYLE LOCK: Photorealistic biblical cinema. Ancient Middle East. Warm earth tones. Dramatic lighting.
- People in linen tunics, woolen cloaks, leather sandals
- Stone/mud-brick architecture, oil lamps, torches
- Golden hour or chiaroscuro lighting
- Cinematic camera angles, shallow depth of field

Return JSON:
{{
  "image_prompt": "Photorealistic biblical cinema scene: WHO (specific biblical-era people with clothing details), WHERE (specific ancient location with architectural details), WHAT action, CAMERA (angle and framing), LIGHTING (golden hour/oil lamp/dramatic shadows). Warm earth tones, film grain, 16:9 widescreen."
}}"""

KEYWORDS_SYSTEM = """You are a stock footage search specialist.
Extract the best search keywords for finding relevant stock footage.
Return ONLY valid JSON - no markdown fences, no extra text."""

KEYWORDS_TEMPLATE = """Extract stock footage search keywords for this scene:

Narration: {narration}
Visual description: {visual_description}

Return JSON:
{{
  "primary_keyword": "Best 2-3 word search query",
  "secondary_keywords": ["alt1", "alt2", "alt3"]
}}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def generate_image_prompt(
    narration: str, visual_description: str, reference_character: str = ""
) -> str:
    style = reference_character or "cinematic, photorealistic"
    system = _build_image_system_prompt(style)
    prompt = IMAGE_PROMPT_TEMPLATE.format(
        narration=narration,
        visual_description=visual_description,
        reference_character=style,
    )
    return _extract_json(_chat(system, prompt, model=_MODEL_FAST, max_tokens=512))["image_prompt"]


def generate_search_keywords(narration: str, visual_description: str) -> Dict:
    prompt = KEYWORDS_TEMPLATE.format(narration=narration, visual_description=visual_description)
    return _extract_json(_chat(KEYWORDS_SYSTEM, prompt, model=_MODEL_FAST, max_tokens=256))


# ── Legacy clean_script (kept for compatibility) ───────────────────────────────

def clean_script(text: str) -> str:
    """
    Strip ALL non-narration content from a Claude-generated script.
    Returns only the spoken narration paragraphs, ready for TTS.
    """
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_', r'\1', text, flags=re.DOTALL)
    # Strip ALL bracketed labels like [Music], [Pause], [1], etc.
    text = re.sub(r'\[.*?\]', '', text)

    _REMOVE_LINE_RE = re.compile(
        r'^('
        r'#{1,6}\s'
        r'|[-=—–]{2,}\s*$'
        r'|YouTube Video Script'
        r'|Runtime\b'
        r'|Words?\s*:\s*\d'
        r'|Word Count\s*:'
        r'|Estimated (Runtime|Duration)\s*:'
        r'|Total Words?\s*:'
        r'|Script\s*:\s*$'
        r'|Title\s*:\s*\S'
        r'|Topic\s*:\s*\S'
        r'|COLD OPEN[:\s]*$'
        r'|ACT \d+[:\s]*$'
        r'|INTRO[:\s]*$'
        r'|OUTRO[:\s]*$'
        r'|HOOK[:\s]*$'
        r'|CONCLUSION[:\s]*$'
        r'|OPENING[:\s]*$'
        r'|CLOSING[:\s]*$'
        r'|SECTION \d+[:\s]*$'
        r'|PART \d+[:\s]*$'
        r'|SCENE \d+[:\s]*$'
        r'|CHAPTER \d+[:\s]*$'
        r'|TALKING POINT \d+[:\s]*$'
        r'|CTA[:\s]*$'
        r'|FADE (IN|OUT)[:\s]*$'
        r'|MUSIC[:\s]*$'
        r'|PAUSE[:\s]*$'
        r')',
        re.IGNORECASE,
    )

    cleaned_lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append('')
            continue
        if _REMOVE_LINE_RE.match(stripped):
            continue
        cleaned_lines.append(line)

    text = '\n'.join(cleaned_lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def edit_script_with_prompt(current_script: str, user_prompt: str) -> str:
    """Use Claude to edit/revise an existing script based on the user's instruction.
    Returns the revised script as plain narration text."""
    raw = _chat(
        system=(
            "You are an expert YouTube video scriptwriter. "
            "The user will give you an existing narration script and an instruction. "
            "Apply the instruction to revise the script. "
            "Return ONLY the revised narration text, plain prose, ready for text-to-speech. "
            "Do NOT include any headers, stage directions, metadata, word counts, or markdown. "
            "Return clean flowing narration. No scene markers, no numbering."
        ),
        user=(
            f"CURRENT SCRIPT:\n\n{current_script}\n\n"
            f"INSTRUCTION: {user_prompt}\n\n"
            "Return the revised script:"
        ),
        model=_MODEL_SMART,
        max_tokens=8192,
    )
    return clean_script(raw)


# ── Legacy generate_script (kept in case referenced elsewhere) ─────────────────

def generate_script(topic: str, video_type: str = "top10", duration: str = "6-8") -> Dict:
    """Legacy: generates script from a topic string. Kept for backward compatibility."""
    return generate_script_full(
        title=topic,
        transcripts=None,
        video_type=video_type,
        duration=duration,
    )


# ── Legacy outline functions (now no-ops, kept for import compatibility) ────────

def generate_outline(title: str, transcripts: list = None) -> str:
    """Deprecated: outline generation is now internal to generate_script_full()."""
    return f"[Outline for: {title}]"


def generate_script_from_outline(outline: str, duration: str = "6-8") -> str:
    """Deprecated: script is now generated directly by generate_script_full()."""
    return outline


# ── Scene division with SRT timestamps ───────────────────────────────────────

_MODEL_SCENE_DIVISION = "google/gemini-2.5-flash"  # Gemini via OpenRouter
_SCENE_CHUNK_WORDS = 3000  # split script into chunks of this many words for long videos


def _split_srt_into_blocks(srt_content: str, block_duration_ms: int = 60000) -> list:
    """Split SRT content into blocks of ~block_duration_ms each.

    Always cuts at SRT entry boundaries. Returns list of (block_srt_text, start_ms, end_ms).
    """
    def ts_to_ms(h, m, s, ms):
        return int(h)*3600000 + int(m)*60000 + int(s)*1000 + int(ms)

    def ms_to_srt(ms):
        h = ms // 3600000; ms %= 3600000
        m = ms // 60000;   ms %= 60000
        s = ms // 1000;    ms %= 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    pattern = re.compile(
        r"\d+\s*\n"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n"
        r"((?:.+\n?)+)",
        re.MULTILINE
    )
    entries = []
    for m in pattern.finditer(srt_content):
        start = ts_to_ms(m.group(1), m.group(2), m.group(3), m.group(4))
        end   = ts_to_ms(m.group(5), m.group(6), m.group(7), m.group(8))
        entries.append((start, end, m.group(9).strip()))

    if not entries:
        return [(srt_content, 0, 0)]

    blocks = []
    current = []
    block_start = entries[0][0]
    for i, entry in enumerate(entries):
        current.append(entry)
        if entry[1] - block_start >= block_duration_ms:
            blocks.append((current[:], block_start, entry[1]))
            current = []
            if i + 1 < len(entries):
                block_start = entries[i + 1][0]
    if current:
        blocks.append((current, block_start, current[-1][1]))

    result = []
    for block_entries, blk_start, blk_end in blocks:
        srt_lines = [
            f"{i}\n{ms_to_srt(s)} --> {ms_to_srt(e)}\n{t}\n"
            for i, (s, e, t) in enumerate(block_entries, 1)
        ]
        result.append(("\n".join(srt_lines), blk_start, blk_end))
    return result


_WORD_TO_DIGIT = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
}


def _normalize_number_label(label: str) -> str:
    """Convert 'Number two:' to 'number 2' for comparison."""
    low = label.lower().rstrip(" ,.:")
    for word, digit in _WORD_TO_DIGIT.items():
        low = re.sub(rf"\b{word}\b", digit, low)
    return low


def _restore_missing_number_labels(scenes: list, original_text: str) -> list:
    """Restore any 'Number X.' labels that Sonnet dropped during division.

    Compares the original script against the scene output. If 'Number two:' exists
    in the original but no scene starts with 'Number two' or 'Number 2', finds the
    scene containing the text that follows it and prepends the label.
    """
    labels_in_original = list(_RE_NUMBER_LABEL.finditer(original_text))
    _safe_print(f"[SceneDivision] _restore_missing_number_labels: {len(labels_in_original)} labels in original, {len(scenes)} scenes")
    if not labels_in_original:
        return scenes

    repaired = 0
    for match in labels_in_original:
        label = match.group(0).strip()  # e.g. "Number two:"
        norm_label = _normalize_number_label(label)  # "number 2"

        # Check if any scene already starts with this label (word OR digit form)
        # Use word boundary to avoid "number 1" matching "number 10"
        label_pattern = re.compile(rf"^{re.escape(norm_label)}\b", re.IGNORECASE)
        label_found = False
        for scene in scenes:
            text = scene["texto"] if isinstance(scene, dict) else scene
            norm_text = _normalize_number_label(text.strip()[:30])
            if label_pattern.match(norm_text):
                label_found = True
                break
        if not label_found:
            _safe_print(f"[SceneDivision] MISSING label: '{label}' (norm: '{norm_label}')")
        if label_found:
            continue

        # Label is missing — find the text that follows it in the original
        after_label = original_text[match.end():match.end() + 60].strip()
        first_words = " ".join(after_label.split()[:5]).lower()
        if not first_words:
            continue

        # Find which scene starts with (or contains) those words
        for scene in scenes:
            text = scene["texto"] if isinstance(scene, dict) else scene
            scene_lower = text.strip().lower()
            if scene_lower.startswith(first_words):
                if isinstance(scene, dict):
                    scene["texto"] = label + " " + scene["texto"]
                _safe_print(f"[SceneDivision] Restored missing label: '{label}' to scene '{first_words[:40]}'")
                repaired += 1
                break

    if repaired:
        _safe_print(f"[SceneDivision] Restored {repaired} missing number labels")
    return scenes


_DIGIT_TO_WORD = {v: k for k, v in _WORD_TO_DIGIT.items()}  # "1" -> "one", "2" -> "two", etc.


def _fix_title_separators(scenes: list, original_script: str) -> list:
    """Insert missing title-end periods using the original script as reference.

    Whisper SRT often strips the period after numbered entry titles, making
    _force_split_numbered_titles unable to find where the title ends.

    For each numbered scene, looks up the corresponding title in the original
    script and inserts the missing period so the title can be split later.
    """
    if not original_script:
        return scenes

    fixed = 0
    for scene in scenes:
        text = scene["texto"] if isinstance(scene, dict) else scene
        if not _is_numbered_entry(text):
            continue

        # Already has a detectable title end — skip
        if _find_title_end(text) is not None:
            continue

        # Extract the number from the scene (e.g., "Number 1." → "1")
        num_m = re.match(
            rf"^\s*(?:Number\s+(?:(\d+)|({_WORD_NUMBERS})))",
            text, re.IGNORECASE,
        )
        if not num_m:
            continue
        digit = num_m.group(1) or _WORD_TO_DIGIT.get(num_m.group(2).lower(), "")
        if not digit:
            continue

        # Find this number in the original script (try both word and digit forms)
        word_form = _DIGIT_TO_WORD.get(digit, "")
        patterns_to_try = [
            rf"Number\s+{re.escape(digit)}\s*[,:.]\s*",
            rf"Number\s+{re.escape(word_form)}\s*[,:.]\s*" if word_form else None,
        ]
        script_match = None
        for pat in patterns_to_try:
            if pat is None:
                continue
            m = re.search(pat, original_script, re.IGNORECASE)
            if m:
                script_match = m
                break
        if not script_match:
            continue

        # Extract the title from the original script: text until ". " followed by uppercase
        after_label = original_script[script_match.end():]
        title_end_m = re.search(r"\.\s+[A-Z]", after_label)
        if not title_end_m:
            continue

        # Get last 3 words of the original title (lowercased) for fuzzy matching
        original_title_words = after_label[:title_end_m.start()].strip().split()
        if len(original_title_words) < 2:
            continue
        tail_words = [w.lower().rstrip(".,;:!?") for w in original_title_words[-3:]]
        tail_pattern = r"\s+".join(re.escape(w) for w in tail_words)

        # Find those tail words in the scene text and insert period after them
        scene_text = scene["texto"] if isinstance(scene, dict) else scene
        tail_m = re.search(tail_pattern, scene_text, re.IGNORECASE)
        if not tail_m:
            continue

        insert_pos = tail_m.end()
        # Don't insert if remaining text is just punctuation (scene is title-only)
        remaining = scene_text[insert_pos:].strip()
        if not remaining or len(remaining.strip('.,;:!?"\'')) < 3:
            continue
        # Only insert if there's no period already at that position
        if insert_pos < len(scene_text) and scene_text[insert_pos:insert_pos + 2] not in (". ", ".\n"):
            new_text = scene_text[:insert_pos] + "." + scene_text[insert_pos:]
            if isinstance(scene, dict):
                scene["texto"] = new_text
            fixed += 1

    if fixed:
        _safe_print(f"[SceneDivision] Fixed {fixed} missing title separators from original script")
    return scenes


def _merge_short_scenes_by_timestamp(scenes: list, min_dur_ms: int = 6000, max_dur_ms: int = 8000) -> list:
    """Merge scenes shorter than min_dur_ms using actual startMs/endMs timestamps.

    Only merges if the combined duration stays <= max_dur_ms (strict 8s cap).
    If no merge is possible without exceeding max, the scene stays as-is.
    """
    for _pass in range(50):
        merged_any = False
        for i, s in enumerate(scenes):
            dur = s["endMs"] - s["startMs"]
            if dur >= min_dur_ms:
                continue

            # Try merge backward
            if i > 0:
                prev_dur = scenes[i - 1]["endMs"] - scenes[i - 1]["startMs"]
                if prev_dur + dur <= max_dur_ms:
                    scenes[i - 1]["texto"] = scenes[i - 1]["texto"].rstrip() + " " + s["texto"].lstrip()
                    scenes[i - 1]["endMs"] = s["endMs"]
                    scenes.pop(i)
                    merged_any = True
                    break

            # Try merge forward
            if i + 1 < len(scenes):
                next_dur = scenes[i + 1]["endMs"] - scenes[i + 1]["startMs"]
                if next_dur + dur <= max_dur_ms:
                    scenes[i + 1]["texto"] = s["texto"].rstrip() + " " + scenes[i + 1]["texto"].lstrip()
                    scenes[i + 1]["startMs"] = s["startMs"]
                    scenes.pop(i)
                    merged_any = True
                    break

            # Can't merge without exceeding 8s — leave as-is

        if not merged_any:
            break

    return scenes


def divide_script_into_scenes(_script_text: str, srt_content: str, mode: str = "animated", video_pipeline: str = "default") -> list:
    """Divide a script into scenes using Claude Haiku + word-level SRT timestamps.

    Approach:
      1. Parse SRT → build word-level timestamps (interpolate within each entry)
      2. Extract full continuous text from SRT
      3. For long videos (>_SCENE_CHUNK_WORDS), split into ~60s SRT blocks and
         process each block independently; otherwise process all at once
      4. Send text to Haiku → get list of scene text strings
      5. Post-process: merge short scenes (<3s) + split long scenes (>7s)
      6. Map scene texts back to word-level timestamps

    Returns list of dicts: [{"id": 1, "texto": "...", "startMs": 0, "endMs": 6500}, ...]
    """
    entries = _parse_srt_entries_full(srt_content)
    if not entries:
        raise RuntimeError("No SRT entries found.")

    word_ts = _build_word_timestamps(entries)
    full_text = " ".join(e["text"] for e in entries)
    total_duration_ms = entries[-1]["end"]
    total_duration_s = total_duration_ms / 1000
    total_words = len(full_text.split())
    wps = total_words / total_duration_s if total_duration_s > 0 else 2.5

    _safe_print(f"[SceneDivision] mode={mode}, {total_words} palabras, {total_duration_s:.1f}s, {wps:.1f} wps")

    if total_words <= _SCENE_CHUNK_WORDS:
        # Short video — process everything in one call
        scene_texts = _divide_text_with_haiku(full_text, total_duration_s, wps, mode, video_pipeline)
        _safe_print(f"[SceneDivision] Haiku devolvió {len(scene_texts)} escenas (un solo bloque)")
        scene_texts = _postprocess_scenes(scene_texts, wps, mode, video_pipeline)
        all_scenes = _map_scenes_to_timestamps(scene_texts, word_ts)
    else:
        # Long video — split SRT into ~60s blocks, process each, merge
        srt_blocks = _split_srt_into_blocks(srt_content, block_duration_ms=60000)
        _safe_print(f"[SceneDivision] {len(srt_blocks)} bloques de ~60s")
        all_scenes = []

        for block_idx, (block_srt, block_start_ms, block_end_ms) in enumerate(srt_blocks):
            _safe_print(f"[SceneDivision] Bloque {block_idx + 1}/{len(srt_blocks)}: "
                        f"{block_start_ms / 1000:.1f}s - {block_end_ms / 1000:.1f}s")

            block_entries = _parse_srt_entries_full(block_srt)
            if not block_entries:
                continue

            block_word_ts = _build_word_timestamps(block_entries)
            block_text = " ".join(e["text"] for e in block_entries)
            block_dur_s = (block_end_ms - block_start_ms) / 1000
            block_words = len(block_text.split())
            block_wps = block_words / block_dur_s if block_dur_s > 0 else wps

            block_scene_texts = _divide_text_with_haiku(block_text, block_dur_s, block_wps, mode, video_pipeline)
            block_scene_texts = _postprocess_scenes(block_scene_texts, block_wps, mode, video_pipeline)
            block_scenes = _map_scenes_to_timestamps(block_scene_texts, block_word_ts)
            all_scenes.extend(block_scenes)

    # ── Final repair: fix numbered entries split across SRT blocks ─────
    all_scenes = _repair_numbered_scenes(all_scenes)

    # ── Restore any "Number X." labels Sonnet dropped ─────
    # Use original script (has all labels) instead of Whisper SRT (may miss some)
    ref_text = _script_text if _script_text else full_text
    all_scenes = _restore_missing_number_labels(all_scenes, ref_text)

    # ── Fix title separators using original script ─────
    all_scenes = _fix_title_separators(all_scenes, _script_text if _script_text else "")

    # ── Second pass: split titles that now have periods inserted ─────
    all_scenes = _force_split_numbered_titles_with_ts(all_scenes)

    # ── Veo pipeline: merge short scenes using REAL timestamps ────────
    # Strict 6-8s. Only merge if result stays <= 8s. 5s scenes are acceptable.
    if video_pipeline == "veo":
        all_scenes = _merge_short_scenes_by_timestamp(all_scenes, min_dur_ms=6000, max_dur_ms=8000)
        _safe_print(f"[SceneDivision] After Veo merge (6-8s): {len(all_scenes)} escenas.")

    # Renumber IDs sequentially
    for idx, s in enumerate(all_scenes, 1):
        s["id"] = idx

    if not all_scenes:
        raise RuntimeError("No se generaron escenas.")

    _safe_print(f"[SceneDivision] Total: {len(all_scenes)} escenas.")
    return all_scenes


def _repair_numbered_scenes(scenes: list) -> list:
    """Post-process pass: fix numbered entries that were split across SRT blocks.

    Detects two cases:
    1. Numbered entry ends with a hanging word (preposition/article):
       "Number 1, The Rock's entrance into" → merge next scene
    2. Numbered entry has NO title boundary (no colon/period after title):
       "Number 5, The Train" → merge next scene until we find ":"/"."

    Merges subsequent scenes until the title is complete.
    """
    if len(scenes) < 2:
        return scenes

    # Pattern: ends with a preposition, article, or connector
    _HANGING_ENDS = re.compile(
        r"\b(?:into|of|the|a|an|in|on|at|to|for|with|from|by|and|or|but|that|which|was|were|is|are|vs)\s*[.:]?\s*$",
        re.IGNORECASE,
    )

    result = []
    i = 0
    repairs = 0
    while i < len(scenes):
        scene = dict(scenes[i])  # copy

        if _is_numbered_entry(scene["texto"]):
            needs_repair = False
            has_title_boundary = _find_title_end(scene["texto"]) is not None

            # Case 1: text ends with a hanging word BUT only if there's no
            # valid title boundary already (e.g. "Sort Of." has boundary → complete)
            if not has_title_boundary and _HANGING_ENDS.search(scene["texto"]):
                needs_repair = True

            # Case 2: no title boundary found (no : or . after the number+title)
            # This means the title was cut short: "Number 5, The Train"
            if not needs_repair and not has_title_boundary:
                # Check if the text is short (likely an incomplete title)
                word_count = len(scene["texto"].split())
                if word_count < 12:  # Short numbered entry without boundary = likely incomplete
                    needs_repair = True

            if needs_repair:
                merge_count = 0
                while i + 1 < len(scenes) and merge_count < 4:
                    next_scene = scenes[i + 1]
                    # Don't merge into another numbered entry
                    if _is_numbered_entry(next_scene["texto"]):
                        break
                    merged_text = scene["texto"].rstrip() + " " + next_scene["texto"].lstrip()
                    scene["texto"] = merged_text
                    scene["endMs"] = next_scene["endMs"]
                    i += 1
                    merge_count += 1
                    repairs += 1
                    # Stop merging once we find a title boundary (colon/period)
                    if _find_title_end(merged_text) is not None:
                        break

        result.append(scene)
        i += 1

    if repairs > 0:
        _safe_print(f"[SceneDivision] Repaired {repairs} split numbered entries")

    # Post-repair split: if a numbered entry's title part has >6 words, split it
    split_result = []
    for scene in result:
        text = scene["texto"]
        if _is_numbered_entry(text):
            title_end = _find_title_end(text)
            if title_end is not None:
                title = text[:title_end].strip()
                detail = text[title_end:].strip()
                if detail and len(title.split()) > 5:
                    mid_ms = (scene["startMs"] + scene["endMs"]) // 2
                    split_result.append({**scene, "texto": title, "endMs": mid_ms})
                    split_result.append({**scene, "texto": detail, "startMs": mid_ms})
                    continue
        split_result.append(scene)
    return split_result


def _parse_srt_entries_full(srt_text: str) -> list:
    """Parse SRT text into list of dicts: [{idx, start, end, text}, ...]."""
    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n"
        r"((?:.+\n?)+)",
        re.MULTILINE,
    )
    entries = []
    for m in pattern.finditer(srt_text):
        start = int(m.group(2))*3600000 + int(m.group(3))*60000 + int(m.group(4))*1000 + int(m.group(5))
        end   = int(m.group(6))*3600000 + int(m.group(7))*60000 + int(m.group(8))*1000 + int(m.group(9))
        entries.append({"idx": int(m.group(1)), "start": start, "end": end, "text": m.group(10).strip()})
    return entries


def _build_word_timestamps(entries: list) -> list:
    """Build word-level timestamps by distributing time evenly across words in each SRT entry.

    Returns: [{"word": "Hola", "start_ms": 0, "end_ms": 250}, ...]
    """
    words = []
    for e in entries:
        entry_words = e["text"].split()
        if not entry_words:
            continue
        duration = e["end"] - e["start"]
        per_word = duration / len(entry_words)
        for i, w in enumerate(entry_words):
            words.append({
                "word": w,
                "start_ms": e["start"] + int(i * per_word),
                "end_ms": e["start"] + int((i + 1) * per_word),
            })
    return words


def _divide_text_with_haiku(full_text: str, total_duration_s: float, wps: float,
                            mode: str = "animated", video_pipeline: str = "default") -> list:
    """Send continuous narration text to Claude Sonnet (OpenRouter). Returns list of scene text strings."""
    total_words = len(full_text.split())
    print(f"[SceneDivision] model={_MODEL_SCENE_DIVISION} (OpenRouter), mode={mode}, pipeline={video_pipeline}, words={total_words}, dur={total_duration_s:.1f}s")
    print(f"[SceneDivision] STOCK PROMPT: {'YES' if mode == 'stock' else 'NO (animated)'}")

    target_min_s = 6 if video_pipeline == "veo" else 4
    target_max_s = 8 if video_pipeline == "veo" else 7
    words_min = max(3, int(target_min_s * wps))
    words_max = int(target_max_s * wps)

    system_prompt = (
        "You are a professional video editor with 20 years of experience cutting documentaries and YouTube videos. "
        "Your job is to divide narration scripts into visual scenes for stock footage videos. "
        "You think carefully about EVERY cut before deciding where to place it. "
        "Return ONLY a JSON array of strings with the text of each scene. "
        "No markdown, no explanation, no extra text."
    )

    if mode == "stock":
        user_prompt = (
            f"Divide this narration script into visual scenes for a stock footage video.\n\n"
            f"FULL SCRIPT:\n{full_text}\n\n"
            f"TOTAL DURATION: {total_duration_s:.1f}s\n"
            f"TOTAL WORDS: {total_words}\n"
            f"SPEAKING RATE: {wps:.2f} words per second\n\n"
            f"=== SCENE DURATION TARGET ===\n"
            f"- TARGET: {target_min_s}-{target_max_s} seconds per scene\n"
            f"- At {wps:.2f} words/sec → {words_min}-{words_max} words per scene\n"
            f"- HARD MAXIMUM: {target_max_s} seconds. A scene MUST NOT exceed {words_max} words.\n"
            f"- MINIMUM: {target_min_s} seconds. Do NOT create scenes shorter than {words_min} words unless absolutely necessary.\n\n"
            "=== HOW TO THINK ABOUT CUTS (THINK LIKE A VIDEO EDITOR) ===\n"
            "Before deciding where to cut, ask yourself:\n"
            "1. What IMAGE would I put on screen for this text?\n"
            "2. Does the cut happen at a natural pause (comma, period, semicolon)?\n"
            "3. Is the idea visually COMPLETE at this cut point?\n"
            "4. Would a viewer understand the scene without seeing the next one?\n\n"
            "A GOOD CUT happens when:\n"
            "- The narration completes a visual idea (a subject + what it does/is)\n"
            "- There's natural punctuation (period, comma, semicolon, colon)\n"
            "- The next sentence introduces a NEW visual idea\n\n"
            "A BAD CUT happens when:\n"
            "- The sentence is broken mid-idea ('the decision was' → no image possible)\n"
            "- The scene ends with a preposition (about, of, to, for, in, on, with, by)\n"
            "- The scene ends with a conjunction (and, or, but, that, which, who)\n"
            "- The scene ends with an article (a, an, the)\n"
            "- Two different visual concepts are crammed into one long scene\n\n"
            "=== CUTTING STRATEGY ===\n"
            "Step 1: Read the entire script first to understand its structure.\n"
            "Step 2: Identify all hard cut points (numbered list items, major topic changes, intro/outro).\n"
            "Step 3: For each section between hard cuts, divide into 4-7 second visual chunks.\n"
            "Step 4: Verify every scene has a clear visual identity — if you can't picture a stock clip for it, the cut is wrong.\n\n"
            "=== CRITICAL RULE FOR NUMBERED LISTS/COUNTDOWN ===\n"
            "If the script contains list numbers (Number 1, Number 2, Number one, Number two, Fact #10, #9, etc.):\n"
            "- The NUMBER + TITLE + FIRST SENTENCE of the explanation must be ONE SINGLE SCENE.\n"
            "- NEVER create a scene with ONLY the number (e.g. 'Number 10.' alone is WRONG).\n"
            "- NEVER remove or omit the number label. If the script says 'Number two.' it MUST appear as 'Number two.' in the output.\n"
            "- NEVER split the number from its title or first sentence.\n"
            "- The numbered scene should include enough content to fill 4-7 seconds.\n"
            "- After the first sentence, divide the remaining explanation into 4-7 second scenes normally.\n"
            "- CORRECT: Scene 1 = 'Number 10. The film's iconic soundtrack features over 60 songs' | Scene 2 = 'from the 1960s, 70s, and 80s, carefully selected by Scorsese himself'\n"
            "- CORRECT: Scene 1 = 'Number 4. The costume budget for Casino exceeded $3 million, making it one of the most expensive wardrobe productions in film history up to that point.'\n"
            "- WRONG: Scene 1 = 'Number 10.' (too short, number alone) | Scene 2 = 'The film's iconic soundtrack...'\n"
            "- WRONG: Scene 1 = 'Number 10. The film's' (incomplete idea)\n\n"
            "=== EXAMPLE ===\n"
            f"Speaking rate: {wps:.2f} words/sec → 4s ≈ {int(4*wps)} words, 7s ≈ {int(7*wps)} words\n\n"
            "Script: \"Fast Five, released in 2011, is arguably the most pivotal movie in the Fast and Furious franchise, "
            "revitalizing what was once a series about street racing into a full-blown heist thriller. "
            "Directed by Justin Lin, the film brought together almost every major character from the previous installments "
            "and introduced Dwayne Johnson as DSS agent Luke Hobbs.\"\n\n"
            "CORRECT cuts:\n"
            "[\n"
            '  "Fast Five, released in 2011,",\n'
            '  "is arguably the most pivotal movie in the Fast and Furious franchise,",\n'
            '  "revitalizing what was once a series about street racing",\n'
            '  "into a full-blown heist thriller.",\n'
            '  "Directed by Justin Lin,",\n'
            '  "the film brought together almost every major character from the previous installments",\n'
            '  "and introduced Dwayne Johnson as DSS agent Luke Hobbs."\n'
            "]\n\n"
            "WRONG cuts:\n"
            '- "is arguably the most pivotal movie in the" ← ends with preposition, incomplete idea\n'
            '- "Fast Five, released in 2011, is arguably the most pivotal movie in the Fast and Furious franchise, revitalizing what was once a series about street racing into a full-blown heist thriller." ← WAY too long (>{words_max} words)\n'
            '- "the" ← meaningless fragment\n\n'
            "=== OUTPUT ===\n"
            "Return ONLY the JSON array of strings. Every word from the original script must appear exactly once. "
            "Do not omit, add, or repeat any text.\n"
            '["scene 1 text", "scene 2 text", ...]'
        )
    elif video_pipeline == "veo":
        # Veo pipeline — scenes must be 6-8 seconds (Veo generates 8s clips)
        target_min, target_max, abs_max = 6, 8, 8
        user_prompt = (
            f"Dividí este texto narrado en escenas visuales para un video.\n\n"
            f"TEXTO COMPLETO:\n{full_text}\n\n"
            f"DURACIÓN TOTAL: {total_duration_s:.1f}s\n"
            f"TOTAL PALABRAS: {total_words}\n"
            f"VELOCIDAD: {wps:.1f} palabras por segundo\n\n"
            "=== REGLAS ===\n"
            f"1. Cada escena debe durar entre {target_min} y {target_max} segundos.\n"
            f"   (A {wps:.1f} palabras/segundo, eso es ~{int(target_min * wps)}-{int(target_max * wps)} palabras por escena)\n"
            f"2. MÍNIMO ABSOLUTO: {target_min} segundos. NUNCA crear escenas menores a {int(target_min * wps)} palabras.\n"
            f"3. MÁXIMO ABSOLUTO: {abs_max} segundos. NUNCA superar {int(abs_max * wps)} palabras por escena.\n"
            "4. NUNCA cortes a mitad de frase o palabra.\n"
            "5. Cortá preferentemente en puntos (.), signos de exclamación (!) o signos de interrogación (?).\n"
            f"6. Si una oración dura más de {target_max} segundos, DEBÉS dividirla en un punto medio natural como una coma "
            "entre cláusulas.\n"
            f"7. Si dos frases cortas consecutivas duran menos de {target_min} segundos cada una, DEBÉS agruparlas en una sola escena.\n"
            "8. Cada escena debe representar UNA idea visual completa.\n"
            "9. Es preferible tener escenas de 7-8 segundos que escenas de 2-3 segundos. Agrupa texto corto.\n\n"
            "=== REGLA PARA LISTAS NUMERADAS ===\n"
            "Si el script contiene números (Number 1, Number 2, #1, etc.):\n"
            "- El NÚMERO + TÍTULO + PRIMERA ORACIÓN deben ser UNA SOLA ESCENA.\n"
            "- NUNCA crees una escena con SOLO el número.\n"
            "- NUNCA elimines los números del texto.\n\n"
            "=== OUTPUT ===\n"
            "Devolvé SOLO el JSON array de strings. "
            "Todo el texto original debe estar presente, sin omitir ni repetir nada.\n"
            "[\"texto escena 1\", \"texto escena 2\", ...]"
        )
    else:
        # Animated mode — original prompt
        target_min, target_max, abs_max = 3, 5, 6
        user_prompt = (
            f"Dividí este texto narrado en escenas visuales para un video.\n\n"
            f"TEXTO COMPLETO:\n{full_text}\n\n"
            f"DURACIÓN TOTAL: {total_duration_s:.1f}s\n"
            f"TOTAL PALABRAS: {total_words}\n"
            f"VELOCIDAD: {wps:.1f} palabras por segundo\n\n"
            "=== REGLAS ===\n"
            f"1. Cada escena debe durar entre {target_min} y {target_max} segundos.\n"
            f"   (A {wps:.1f} palabras/segundo, eso es ~{int(target_min * wps)}-{int(target_max * wps)} palabras por escena)\n"
            f"2. MÁXIMO ABSOLUTO: {abs_max} segundos. NUNCA superar {abs_max} segundos por escena.\n"
            "3. Calculá la duración estimada de cada escena contando sus palabras y dividiéndolas por la velocidad.\n"
            "4. NUNCA cortes a mitad de frase o palabra.\n"
            "5. Cortá preferentemente en puntos (.), signos de exclamación (!) o signos de interrogación (?).\n"
            f"6. Si una oración dura más de {target_max} segundos, DEBÉS dividirla en un punto medio natural como una coma "
            "entre cláusulas. En ese caso la coma SÍ es un punto de corte válido. "
            f"La regla de no cortar en comas aplica solo para oraciones que caben en {target_max} segundos.\n"
            "7. NUNCA dejes una escena con menos de 4 palabras.\n"
            f"8. Si dos frases cortas consecutivas duran menos de {target_min} segundos cada una, agrupalas en una sola escena.\n"
            "9. Cada escena debe representar UNA idea visual completa.\n\n"
            "=== EJEMPLO ===\n"
            "Texto: \"Objetos que estuvieron en contacto directo con el cuerpo de Cristo "
            "aún existen hoy, custodiados en catedrales, bóvedas seguras y museos "
            "de todo el mundo. No son solo leyendas o cuentos medievales, sino "
            "reliquias físicas con siglos de historia documentada, analizadas por "
            "científicos modernos y veneradas por millones de peregrinos.\"\n\n"
            "BIEN dividido:\n"
            "[\n"
            "  \"Objetos que estuvieron en contacto directo con el cuerpo de Cristo aún existen hoy,\",\n"
            "  \"custodiados en catedrales, bóvedas seguras y museos de todo el mundo.\",\n"
            "  \"No son solo leyendas o cuentos medievales,\",\n"
            "  \"sino reliquias físicas con siglos de historia documentada,\",\n"
            "  \"analizadas por científicos modernos y veneradas por millones de peregrinos.\"\n"
            "]\n\n"
            "MAL dividido:\n"
            "- Una oración de 8 palabras en una sola escena cuando podría dividirse en coma\n"
            "- Repetir texto entre escenas\n"
            "- Cortar a mitad de frase sin puntuación\n\n"
            "=== OUTPUT ===\n"
            "Devolvé SOLO el JSON array de strings. "
            "Todo el texto original debe estar presente, sin omitir ni repetir nada.\n"
            "[\"texto escena 1\", \"texto escena 2\", ...]"
        )

    # Debug: confirm which prompt is being sent
    _has_visual_rules = "NUNCA termines una escena en preposición" in user_prompt
    print(f"[SceneDivision] Prompt has visual-coherence rules: {_has_visual_rules}")
    print(f"[SceneDivision] Prompt first 300 chars: {user_prompt[:300]}")

    _safe_print(f"[SceneDivision] Calling Gemini via OpenRouter...")
    raw = _chat(system_prompt, user_prompt, model=_MODEL_SCENE_DIVISION, max_tokens=16000)
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    # Extract only the JSON array (Sonnet sometimes adds text after the array)
    bracket_start = raw.find("[")
    bracket_end = raw.rfind("]")
    if bracket_start != -1 and bracket_end != -1:
        raw = raw[bracket_start:bracket_end + 1]
    scenes = json.loads(raw)

    if not isinstance(scenes, list) or not all(isinstance(s, str) for s in scenes):
        raise ValueError(f"Expected JSON array of strings, got: {type(scenes)}")

    return scenes


def _force_split_numbered_titles_with_ts(scenes: list) -> list:
    """Like _force_split_numbered_titles but for scene dicts with timestamps.

    Used after _fix_title_separators inserts missing periods — the original
    _force_split_numbered_titles already ran (on plain strings) before the
    period was available, so this second pass catches newly-splittable titles.
    """
    result = []
    for scene in scenes:
        text = scene["texto"]
        if _is_numbered_entry(text):
            title_end = _find_title_end(text)
            if title_end is not None:
                title = text[:title_end].strip()
                detail = text[title_end:].strip()
                # Detail must be meaningful (not just punctuation)
                detail_clean = detail.strip('.,;:!?"\'() ')
                if detail_clean and len(detail_clean.split()) >= 3 and len(title.split()) > 5:
                    total_words = len(text.split())
                    title_words = len(title.split())
                    ratio = title_words / total_words if total_words > 0 else 0.5
                    start = scene["startMs"]
                    end = scene["endMs"]
                    mid = int(start + (end - start) * ratio)
                    _safe_print(f"[PostProc] Split numbered title (ts): '{title[:50]}' | '{detail[:50]}'")
                    result.append({"id": 0, "texto": title, "startMs": start, "endMs": mid})
                    result.append({"id": 0, "texto": detail, "startMs": mid, "endMs": end})
                    continue
        result.append(scene)
    return result


def _force_split_numbered_titles(scene_texts: list) -> list:
    """Force-split any numbered entry where title + explanation are in the same scene.

    GUARD: Only split if the title part has >5 words to avoid creating
    ultra-short scenes like "Number 10." (2 words).

    E.g. "Number 2. Rambo in Beverly Hills. The script was action-heavy..." (6-word title)
      → splits into title + detail
    But "Number 10. The soundtrack." (4-word title)
      → stays together (title too short to stand alone)
    """
    result = []
    for text in scene_texts:
        if _is_numbered_entry(text):
            title_end = _find_title_end(text)
            if title_end is not None:
                title = text[:title_end].strip()
                detail = text[title_end:].strip()
                if detail and len(title.split()) > 5:
                    _safe_print(f"[PostProc] Split numbered title: '{title[:50]}' | detail: '{detail[:50]}'")
                    result.append(title)
                    result.append(detail)
                    continue
        result.append(text)
    return result


def _merge_short_duration_scenes(scenes: list, wps: float, min_dur: float, max_dur: float) -> list:
    """Merge scenes shorter than min_dur into their neighbors (by duration, not word count).

    Used for Veo pipeline where each video clip is 8s — scenes under 6s waste the clip.
    Merges into the shorter neighbor, respecting max_dur to avoid creating oversized scenes.
    """
    for _pass in range(10):
        changed = False
        result = []
        i = 0
        while i < len(scenes):
            text = scenes[i]
            dur = len(text.split()) / wps
            if dur < min_dur and len(scenes) > 1:
                # Try to merge with the shorter neighbor
                prev_dur = len(result[-1].split()) / wps if result else 999
                next_dur = len(scenes[i + 1].split()) / wps if i + 1 < len(scenes) else 999

                # Merge backward if combined doesn't exceed max
                if result and prev_dur <= next_dur and (prev_dur + dur) <= max_dur:
                    result[-1] = result[-1].rstrip() + " " + text.lstrip()
                    changed = True
                    i += 1
                    continue
                # Merge forward if combined doesn't exceed max
                if i + 1 < len(scenes) and (next_dur + dur) <= max_dur:
                    scenes[i + 1] = text.rstrip() + " " + scenes[i + 1].lstrip()
                    changed = True
                    i += 1
                    continue
                # If neither fits under max, merge with the shorter one anyway
                if result and prev_dur <= next_dur:
                    result[-1] = result[-1].rstrip() + " " + text.lstrip()
                    changed = True
                    i += 1
                    continue
                if i + 1 < len(scenes):
                    scenes[i + 1] = text.rstrip() + " " + scenes[i + 1].lstrip()
                    changed = True
                    i += 1
                    continue
            result.append(text)
            i += 1
        scenes = result
        if not changed:
            break
    return scenes


def _postprocess_scenes(scene_texts: list, wps: float, mode: str = "animated", video_pipeline: str = "default") -> list:
    """Post-process scene texts: merge short, split long, validate.

    Runs merge→split→merge cycles until all scenes are 4+ words and within max duration.
    Numbered entries (Number X, #X) get a relaxed max duration to avoid bad splits.
    """
    if video_pipeline == "veo":
        MAX_DUR = 8.0
        MIN_DUR = 6.0           # Veo generates 8s clips — scenes < 6s waste video
        NUMBERED_MAX_DUR = 15.0
    elif mode == "stock":
        MAX_DUR = 7.0
        MIN_DUR = 0.0           # no minimum for stock
        NUMBERED_MAX_DUR = 15.0
    else:
        MAX_DUR = 6.0           # default: Pollinations+Meta AI (DO NOT CHANGE)
        MIN_DUR = 0.0
        NUMBERED_MAX_DUR = 12.0
    min_words = 4

    def _effective_max(text):
        return NUMBERED_MAX_DUR if _is_numbered_entry(text) else MAX_DUR

    scenes = list(scene_texts)

    scenes = _force_split_numbered_titles(scenes)

    # Run up to 10 full cycles of merge+split until everything is clean
    for _cycle in range(10):
        # ── MERGE: absorb any scene with <4 words into its neighbor ──────
        scenes = _merge_short_scenes(scenes, min_words)

        # ── MERGE BY DURATION: merge scenes shorter than MIN_DUR ─────────
        if MIN_DUR > 0:
            scenes = _merge_short_duration_scenes(scenes, wps, MIN_DUR, MAX_DUR)

        # ── SPLIT: break any scene exceeding its max duration ────────────
        scenes = _split_long_scenes(scenes, wps, MAX_DUR, min_words,
                                    numbered_max_dur=NUMBERED_MAX_DUR)

        # ── CHECK: are we done? ──────────────────────────────────────────
        has_short = any(len(t.split()) < min_words for t in scenes)
        has_long = any(len(t.split()) / wps > _effective_max(t) for t in scenes)

        if not has_short and not has_long:
            break

        # If only long scenes remain that can't be split, stop
        if not has_short and has_long:
            # One more attempt with brute-force splits
            scenes = _force_split_long_scenes(scenes, wps, MAX_DUR, min_words,
                                              numbered_max_dur=NUMBERED_MAX_DUR)
            scenes = _merge_short_scenes(scenes, min_words)
            break

    # Final safety log
    for i, t in enumerate(scenes):
        wc = len(t.split())
        dur = wc / wps
        eff_max = _effective_max(t)
        if wc < min_words:
            _safe_print(f"[WARNING] Scene {i+1}: {wc} words (< {min_words}): {t[:60]}")
        if dur > eff_max:
            _safe_print(f"[WARNING] Scene {i+1}: {dur:.1f}s (> {eff_max}s): {t[:60]}")

    return scenes


def _merge_short_scenes(scenes: list, min_words: int) -> list:
    """Merge any scene with fewer than min_words into its neighbor.
    Repeats until no short scenes remain.

    GUARD: Never merge a short scene INTO a numbered entry (would prepend
    random text before "Number X..."). In that case, merge into the PREVIOUS scene.
    """
    for _pass in range(10):
        result = []
        changed = False
        i = 0
        while i < len(scenes):
            text = scenes[i]
            wc = len(text.split())
            if wc < min_words and len(scenes) > 1:
                # Bare number without title (e.g. "Number 14.") → merge forward
                # Check this FIRST because _is_numbered_entry won't match bare
                # numbers (it requires trailing content with a space).
                bare_m = re.match(
                    rf"^\s*(?:Number\s+(?:\d+|{_WORD_NUMBERS})|#\s*\d+|\d{{1,2}})\s*[,:.]\s*$",
                    text,
                    re.IGNORECASE,
                )
                if bare_m and i + 1 < len(scenes):
                    scenes[i + 1] = text.rstrip() + " " + scenes[i + 1].lstrip()
                    changed = True
                    i += 1
                    continue
                # Numbered entries: if still too short, merge forward
                if _is_numbered_entry(text):
                    if i + 1 < len(scenes) and not _is_numbered_entry(scenes[i + 1]):
                        scenes[i + 1] = text.rstrip() + " " + scenes[i + 1].lstrip()
                        changed = True
                        i += 1
                        continue
                    # Can't merge forward — keep as-is
                    result.append(text)
                    i += 1
                    continue
                changed = True
                if i + 1 < len(scenes):
                    # If next scene starts with a numbered entry, merge into previous instead
                    if _is_numbered_entry(scenes[i + 1]) and result:
                        result[-1] = result[-1].rstrip() + " " + text.lstrip()
                        i += 1
                        continue
                    # Merge into next
                    scenes[i + 1] = text.rstrip() + " " + scenes[i + 1].lstrip()
                    i += 1
                    continue
                elif result:
                    # Last scene — merge into previous
                    result[-1] = result[-1].rstrip() + " " + text.lstrip()
                    i += 1
                    continue
            result.append(text)
            i += 1
        scenes = result
        if not changed:
            break
    return scenes


def _split_long_scenes(scenes: list, wps: float, max_dur: float, min_words: int,
                       numbered_max_dur: float | None = None) -> list:
    """Split scenes exceeding max_dur at natural boundaries. Up to 5 passes.
    Numbered entries use numbered_max_dur (relaxed limit) to avoid bad splits."""
    if numbered_max_dur is None:
        numbered_max_dur = max_dur
    for _ in range(5):
        result = []
        changed = False
        for text in scenes:
            words = text.split()
            effective_max = numbered_max_dur if _is_numbered_entry(text) else max_dur
            if len(words) / wps <= effective_max:
                result.append(text)
                continue
            parts = _try_split_scene(text, words, min_words)
            if parts:
                result.extend(parts)
                changed = True
            else:
                result.append(text)
        scenes = result
        if not changed:
            break
    return scenes


def _force_split_long_scenes(scenes: list, wps: float, max_dur: float, min_words: int,
                             numbered_max_dur: float | None = None) -> list:
    """Brute-force split any remaining >max_dur scenes at word midpoint.
    For numbered entries, prefer splitting at title boundary over midpoint."""
    if numbered_max_dur is None:
        numbered_max_dur = max_dur
    result = []
    for text in scenes:
        words = text.split()
        effective_max = numbered_max_dur if _is_numbered_entry(text) else max_dur
        if len(words) / wps <= effective_max:
            result.append(text)
            continue
        # Try smart split first
        parts = _try_split_scene(text, words, min_words)
        if parts:
            result.extend(parts)
            continue
        # For numbered entries: prefer splitting at title boundary
        if _is_numbered_entry(text):
            title_end = _find_title_end(text)
            if title_end is not None:
                title_part = text[:title_end].strip()
                detail_part = text[title_end:].strip()
                if len(title_part.split()) >= min_words and len(detail_part.split()) >= min_words:
                    result.append(title_part)
                    result.append(detail_part)
                    continue
        # Brute force: split at word midpoint
        mid_w = len(words) // 2
        if mid_w >= min_words and (len(words) - mid_w) >= min_words:
            result.append(" ".join(words[:mid_w]))
            result.append(" ".join(words[mid_w:]))
        else:
            result.append(text)
    return result


def _try_split_scene(text: str, words: list, min_words: int = 4) -> list | None:
    """Try to split a scene text at a natural boundary. Returns [left, right] or None.

    For numbered entries (Number X, #X), protects the title from being split:
    splits ONLY at the title boundary (after the colon/period ending the title).
    """
    mid = len(text) // 2

    # ── Numbered entry protection ──────────────────────────────────────
    # If this scene starts with a number pattern, ONLY split at the title boundary.
    # Never split within the title itself ("Number 1, The Rock's entrance into" is WRONG).
    if _is_numbered_entry(text):
        title_end = _find_title_end(text)
        if title_end is not None:
            title_part = text[:title_end].strip()
            detail_part = text[title_end:].strip()
            title_wc = len(title_part.split())
            detail_wc = len(detail_part.split())
            if title_wc >= min_words and detail_wc >= min_words:
                return [title_part, detail_part]
        # No valid title boundary found, or parts too short — don't split at all.
        # The relaxed NUMBERED_MAX_DUR in _postprocess_scenes will tolerate this.
        return None

    # ── Standard split logic (non-numbered scenes) ─────────────────────

    # 1. Period nearest to midpoint
    for radius in range(len(text) // 2):
        for pos in [mid - radius, mid + radius]:
            if 0 <= pos < len(text) and text[pos] == '.':
                left, right = text[:pos + 1].strip(), text[pos + 1:].strip()
                if len(left.split()) >= min_words and len(right.split()) >= min_words:
                    return [left, right]

    # 2. Comma between 30-70%
    lo, hi = int(len(text) * 0.30), int(len(text) * 0.70)
    best_comma, best_dist = None, float("inf")
    for pos in range(lo, hi):
        if text[pos] == ',':
            left, right = text[:pos + 1].strip(), text[pos + 1:].strip()
            if len(left.split()) >= min_words and len(right.split()) >= min_words:
                dist = abs(pos - mid)
                if dist < best_dist:
                    best_dist, best_comma = dist, pos
    if best_comma is not None:
        return [text[:best_comma + 1].strip(), text[best_comma + 1:].strip()]

    # 3. Clause connector nearest to midpoint (30-70%)
    CONNECTORS = {"y", "que", "pero", "porque", "donde", "cuando", "sino", "ni", "o",
                  "and", "but", "or", "that", "which", "where", "when"}
    mid_w = len(words) // 2
    lo_w, hi_w = int(len(words) * 0.30), int(len(words) * 0.70)
    best_pos, best_dist = None, float("inf")
    for wi in range(lo_w, hi_w):
        if words[wi].lower().strip(".,;:!?") in CONNECTORS:
            dist = abs(wi - mid_w)
            if dist < best_dist and len(words[:wi]) >= min_words and len(words[wi:]) >= min_words:
                best_dist, best_pos = dist, wi
    if best_pos is not None:
        return [" ".join(words[:best_pos]), " ".join(words[best_pos:])]

    return None


def _map_scenes_to_timestamps(scene_texts: list, word_timestamps: list) -> list:
    """Map scene texts to timestamps by sequential word counting.

    Walks through scene texts and word_timestamps in parallel, assigning
    startMs/endMs from interpolated word positions.
    """
    scenes = []
    word_idx = 0
    total_words = len(word_timestamps)

    for scene_num, scene_text in enumerate(scene_texts, 1):
        scene_words = scene_text.split()
        if not scene_words:
            continue

        start_idx = word_idx
        end_idx = min(start_idx + len(scene_words), total_words)

        if start_idx >= total_words:
            last_ts = word_timestamps[-1]["end_ms"] if word_timestamps else 0
            scenes.append({"id": scene_num, "texto": scene_text,
                           "startMs": last_ts, "endMs": last_ts})
            continue

        start_ms = word_timestamps[start_idx]["start_ms"]
        end_ms = word_timestamps[min(end_idx - 1, total_words - 1)]["end_ms"]

        scenes.append({"id": scene_num, "texto": scene_text,
                       "startMs": start_ms, "endMs": end_ms})
        word_idx = end_idx

    return scenes


# ── Whisper recalibration ────────────────────────────────────────────────────

import re as _re
import unicodedata as _ud


def _normalize_word(w: str) -> str:
    """Lowercase, strip punctuation, normalize unicode for fuzzy comparison."""
    w = _ud.normalize("NFKD", w).lower()
    return _re.sub(r"[^\w]", "", w)


def recalibrate_chunk_timestamps(
    chunks_data: list[dict],
    whisper_words: list,
) -> list[dict]:
    """Align existing chunk scene_texts to Whisper word-level timestamps.

    Strategy: for each chunk, find its first words in the Whisper stream
    using fuzzy matching around a proportional expected position.
    Each chunk's end_ms = next chunk's start_ms (or audio end for last).
    """
    from difflib import SequenceMatcher

    # Flatten Whisper words
    w_norms = []
    w_times = []
    for w in whisper_words:
        word_text = w.word if hasattr(w, "word") else w.get("word", "")
        start = w.start if hasattr(w, "start") else w.get("start", 0)
        end = w.end if hasattr(w, "end") else w.get("end", 0)
        w_norms.append(_normalize_word(word_text))
        w_times.append({"start_ms": int(start * 1000), "end_ms": int(end * 1000)})

    if not w_times:
        return [
            {"chunk_number": c["chunk_number"], "start_ms": 0, "end_ms": 0}
            for c in chunks_data
        ]

    total_scene = sum(len(c["scene_text"].split()) for c in chunks_data)
    total_w = len(w_norms)

    # ── Pass 1: find anchor for each chunk ──────────────────────────────────
    anchors = []
    cum_words = 0
    min_pos = 0  # Enforce monotonically non-decreasing anchors

    for chunk in chunks_data:
        scene_words = chunk["scene_text"].split()
        n_words = len(scene_words)
        scene_norms = [_normalize_word(w) for w in scene_words]

        # Expected position (proportional hint)
        expected_i = int(cum_words / max(total_scene, 1) * total_w)
        expected_i = max(expected_i, min_pos)
        expected_i = min(expected_i, total_w - 1)

        # Search window: only forward from min_pos, up to +30 from expected
        search_lo = max(min_pos, expected_i - 15)
        search_hi = min(total_w, expected_i + 30)

        # Match first 3 scene words against Whisper stream
        match_n = min(3, len(scene_norms))
        target = " ".join(scene_norms[:match_n])

        best_pos = expected_i
        best_score = -1

        for pos in range(search_lo, search_hi):
            available = min(match_n, total_w - pos)
            if available < 1:
                break
            candidate = " ".join(w_norms[pos + j] for j in range(available))
            score = SequenceMatcher(None, target, candidate).ratio()
            if score > best_score:
                best_score = score
                best_pos = pos
            if score > 0.9:
                break

        anchors.append(best_pos)
        min_pos = best_pos + 1  # Next chunk must start after this one
        cum_words += n_words

    # ── Pass 2: build results ───────────────────────────────────────────────
    audio_end_ms = w_times[-1]["end_ms"]
    results = []

    for i, chunk in enumerate(chunks_data):
        start_ms = w_times[anchors[i]]["start_ms"]
        if i + 1 < len(anchors):
            end_ms = w_times[anchors[i + 1]]["start_ms"]
        else:
            end_ms = audio_end_ms
        end_ms = max(start_ms, end_ms)

        results.append({
            "chunk_number": chunk["chunk_number"],
            "start_ms": start_ms,
            "end_ms": end_ms,
        })

    return results
