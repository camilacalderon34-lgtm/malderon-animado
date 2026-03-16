"""Visual Analyzer — uses local Claude Code CLI to decide what type of visual each scene needs.

Also provides image validation: checks if a downloaded image actually matches the scene.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Dict


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


def _call_claude_local(prompt: str, system: str = "") -> str:
    """Call Claude Code CLI locally (uses your active subscription, no API key needed)."""
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    result = subprocess.run(
        ["claude", "-p", full_prompt],
        capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI error: {result.stderr.strip()}")
    return result.stdout.strip()


# ── Vision via Claude Code CLI (uses your subscription — no API key needed) ──

def _call_claude_vision(image_path: str | Path, prompt: str, max_turns: int = 3) -> str:
    """Call Claude Code CLI with an image file for vision analysis.

    Uses the Read tool so Claude can see the image. This leverages your
    Claude Code subscription (Sonnet 4) — much better vision than Gemini Flash.
    Retries up to 2 times on timeout or error.
    """
    abs_path = str(Path(image_path).resolve())
    full_prompt = f'Read the image at {abs_path} and then answer this:\n\n{prompt}'

    last_error = None
    for attempt in range(1, 3):
        try:
            _safe_print(f"[Vision] Claude vision call attempt {attempt}/2...")
            result = subprocess.run(
                ["claude", "-p", full_prompt,
                 "--allowedTools", "Read",
                 "--max-turns", str(max_turns)],
                capture_output=True, text=True, timeout=90,
                encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                last_error = f"Claude vision error: {result.stderr.strip()[:200]}"
                _safe_print(f"[Vision] Attempt {attempt}/2 failed: {last_error}")
                continue
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            last_error = "Claude vision TIMEOUT (90s)"
            _safe_print(f"[Vision] Attempt {attempt}/2: {last_error}")
            continue
        except Exception as exc:
            last_error = str(exc)
            _safe_print(f"[Vision] Attempt {attempt}/2 error: {last_error}")
            continue

    raise RuntimeError(f"Claude vision failed after 2 attempts: {last_error}")


# ── Image validation with Vision ────────────────────────────────────────────


def validate_image(
    image_path: str | Path,
    scene_text: str,
    search_query: str,
    project_title: str = "",
    flexible: bool = False,
) -> bool:
    """Check if a downloaded image is relevant to the scene using Claude Sonnet Vision.

    Args:
        flexible: If True, use a more permissive validation that accepts images
                  that are broadly related to the topic (not just exact matches).

    Returns True if the image is relevant, False if not.
    Fails open (returns True) on any error to avoid blocking the pipeline.
    """
    try:
        image_path = Path(image_path)
        if not image_path.exists() or image_path.stat().st_size < 1000:
            return True  # Can't validate, accept it

        context = f'This is for a video titled: "{project_title}". ' if project_title else ""

        if flexible:
            prompt = (
                f'{context}\n'
                f'The scene narration says: "{scene_text[:300]}"\n'
                f'We searched for: "{search_query}"\n\n'
                f'You are a PROFESSIONAL VIDEO EDITOR. Would you use this image in this scene?\n'
                f'Think: "Does this image make sense visually while the narrator says this?"\n\n'
                f'- YES: any real photo that visually supports what the narrator is talking about\n'
                f'- YES: behind-the-scenes, cast, awards, events, locations, related imagery\n'
                f'- YES: photos of the movie/person/event mentioned in the video title\n'
                f'- NO ONLY if: completely unrelated (e.g., a cat photo for a movie about space)\n\n'
                f'Be GENEROUS. A pro editor often uses tangentially related imagery.\n'
                f'Answer ONLY "YES" or "NO".'
            )
        else:
            prompt = (
                f'{context}\n'
                f'The scene narration says: "{scene_text[:300]}"\n'
                f'We searched for: "{search_query}"\n\n'
                f'Look at this image carefully. Does it SPECIFICALLY match what the scene is talking about?\n'
                f'- If the video is about a specific movie/person/event, the image MUST show something from that movie/person/event.\n'
                f'- Generic images, illustrations, cartoons, or unrelated content = NO.\n'
                f'- Real photos/screenshots that match the specific topic = YES.\n\n'
                f'Answer ONLY "YES" or "NO".'
            )

        answer = _call_claude_vision(image_path, prompt)
        answer_upper = answer.strip().upper()
        is_relevant = "YES" in answer_upper
        mode = "flexible" if flexible else "strict"
        _safe_print(f"[Validate] Image {'APPROVED' if is_relevant else 'REJECTED'} ({mode}) for query='{search_query}' (answer={answer_upper[:20]})")
        return is_relevant

    except Exception as exc:
        _safe_print(f"[Validate] Error (accepting image): {exc}")
        return True  # Fail open — don't block pipeline on validation errors


def validate_clip_frame(
    frame_path: str | Path,
    scene_text: str,
    search_query: str,
    project_title: str = "",
) -> bool:
    """Validate a video clip frame — stricter than image validation.

    Rejects:
    - Static posters/movie posters (not real footage)
    - Text slides (text on dark/plain background — like "On July 4th...")
    - Title cards, credits, intro/outro slides
    - Completely unrelated content

    Accepts:
    - Real video footage (movies, behind-the-scenes, documentaries)
    - Action scenes, people, locations, events
    """
    try:
        frame_path = Path(frame_path)
        if not frame_path.exists() or frame_path.stat().st_size < 500:
            return True

        context = f'Video project: "{project_title}". ' if project_title else ""

        prompt = (
            f'{context}\n'
            f'Scene narration: "{scene_text[:300]}"\n'
            f'Search query: "{search_query}"\n\n'
            f'This is a frame from a YouTube video clip meant as B-roll footage.\n'
            f'Is this frame suitable as B-roll for this scene?\n\n'
            f'REJECT if:\n'
            f'- It\'s a MOVIE POSTER or promotional image (static, not footage)\n'
            f'- It\'s a TEXT SLIDE (text on dark/plain background, like trailer text)\n'
            f'- It\'s a TITLE CARD, credits, or intro/outro screen\n'
            f'- It\'s a reaction video thumbnail, podcast, or commentary screenshot\n'
            f'- It\'s completely unrelated to the video topic\n\n'
            f'ACCEPT if:\n'
            f'- It shows REAL VIDEO FOOTAGE (movie scenes, behind-the-scenes, documentary)\n'
            f'- It shows people, locations, action, or events related to the topic\n'
            f'- It looks like actual filmed content (not a designed graphic)\n\n'
            f'Answer ONLY "YES" (accept) or "NO" (reject).'
        )

        answer = _call_claude_vision(frame_path, prompt)
        answer_upper = answer.strip().upper()
        is_ok = "YES" in answer_upper
        _safe_print(f"[ValidateClip] Frame {'APPROVED' if is_ok else 'REJECTED'} for scene (answer={answer_upper[:20]})")
        return is_ok

    except Exception as exc:
        _safe_print(f"[ValidateClip] Error (accepting frame): {exc}")
        return True  # Fail open


def analyze_video_cleanliness(
    frame_path: str | Path,
    video_width: int = 0,
    video_height: int = 0,
) -> dict:
    """Analyze a video frame for logos, watermarks, text overlays, and black bars.

    Uses Claude Sonnet Vision (local subscription) to detect visual impurities
    and returns crop/zoom instructions to produce a clean frame.

    Returns dict with:
        - has_issues: bool — whether any issues were detected
        - zoom_percent: int — suggested zoom (100 = no zoom, 125 = 25% zoom in)
        - crop_top_pct: float — % to crop from top (0-20)
        - crop_bottom_pct: float — % to crop from bottom (0-20)
        - crop_left_pct: float — % to crop from left (0-15)
        - crop_right_pct: float — % to crop from right (0-15)
        - issues: list of strings describing detected issues
    """
    try:
        frame_path = Path(frame_path)
        if not frame_path.exists() or frame_path.stat().st_size < 1000:
            return {"has_issues": False, "zoom_percent": 100,
                    "crop_top_pct": 0, "crop_bottom_pct": 0,
                    "crop_left_pct": 0, "crop_right_pct": 0, "issues": []}

        size_info = f"Video resolution: {video_width}x{video_height}. " if video_width else ""

        prompt = f"""{size_info}Analyze this video frame for visual impurities to crop out.

Check for:
1. BLACK BARS: letterbox (top/bottom) or pillarbox (left/right)? Estimate % each bar takes.
2. LOGOS/WATERMARKS: channel logo or watermark in any corner?
3. TEXT OVERLAYS: subtitles, "Movie" text, channel name at bottom? Titles at top? Center text?
4. BORDERS/FRAMES: decorative borders?

Rules for cropping:
- Bottom text (subtitles, "Movie"): crop bottom 8-15%
- Corner logo: crop that area 5-12%
- Black bars: crop them completely
- Never crop more than 20% from any side
- If clean, return all zeros

Return ONLY a JSON object (no markdown, no explanation):
{{
    "has_issues": true/false,
    "issues": ["list of issues found"],
    "crop_top_pct": 0,
    "crop_bottom_pct": 0,
    "crop_left_pct": 0,
    "crop_right_pct": 0,
    "zoom_percent": 100
}}"""

        answer = _call_claude_vision(frame_path, prompt)
        # Clean markdown wrapping
        answer = re.sub(r"^```(?:json)?\s*", "", answer)
        answer = re.sub(r"\s*```$", "", answer)
        result = json.loads(answer)

        # Normalize and validate
        crop_top = min(20, max(0, float(result.get("crop_top_pct", 0))))
        crop_bottom = min(20, max(0, float(result.get("crop_bottom_pct", 0))))
        crop_left = min(15, max(0, float(result.get("crop_left_pct", 0))))
        crop_right = min(15, max(0, float(result.get("crop_right_pct", 0))))
        zoom = min(135, max(100, int(result.get("zoom_percent", 100))))
        has_issues = bool(result.get("has_issues", False))
        issues = result.get("issues", [])

        # If total crop is too aggressive (>35% total), reduce proportionally
        total_crop = crop_top + crop_bottom + crop_left + crop_right
        if total_crop > 35:
            factor = 35 / total_crop
            crop_top *= factor
            crop_bottom *= factor
            crop_left *= factor
            crop_right *= factor

        clean_result = {
            "has_issues": has_issues,
            "zoom_percent": zoom,
            "crop_top_pct": round(crop_top, 1),
            "crop_bottom_pct": round(crop_bottom, 1),
            "crop_left_pct": round(crop_left, 1),
            "crop_right_pct": round(crop_right, 1),
            "issues": issues if isinstance(issues, list) else [],
        }

        if has_issues:
            _safe_print(
                f"[CleanCheck] Issues found: {issues}. "
                f"Crop: top={crop_top:.1f}% bot={crop_bottom:.1f}% "
                f"left={crop_left:.1f}% right={crop_right:.1f}% zoom={zoom}%"
            )
        else:
            _safe_print(f"[CleanCheck] Frame is CLEAN — no adjustments needed")

        return clean_result

    except Exception as exc:
        _safe_print(f"[CleanCheck] Analysis error (skipping cleanup): {exc}")
        return {"has_issues": False, "zoom_percent": 100,
                "crop_top_pct": 0, "crop_bottom_pct": 0,
                "crop_left_pct": 0, "crop_right_pct": 0, "issues": []}


def analyze_scenes(
    full_script: str, scenes: List[Dict], collection: str = "general",
    allowed_types: list | None = None,
    project_title: str = "",
) -> List[Dict]:
    """Analyze each scene and decide asset_type + search_query.

    Args:
        full_script: complete narration text (for context)
        scenes: list of dicts with at least 'id' and 'texto'
        collection: project collection name (e.g. 'cine', 'tech') for context
        allowed_types: if set, only these types can be used
        project_title: title of the video project (for search context)

    Returns:
        list of dicts per scene: scene_id, asset_type, search_query, search_query_alt,
        has_overlay_text, overlay_text
    """
    # Process in blocks of 15 scenes
    all_results = []
    for i in range(0, len(scenes), 15):
        block = scenes[i:i + 15]
        block_results = _analyze_block(full_script, block, collection, allowed_types, project_title)
        all_results.extend(block_results)
    return all_results


def _analyze_block(
    full_script: str, scenes: List[Dict], collection: str = "general",
    allowed_types: list | None = None, project_title: str = "",
) -> List[Dict]:
    """Analyze a block of up to 15 scenes."""
    scenes_text = "\n".join(
        f"Escena {s['id']}: \"{s['texto']}\""
        for s in scenes
    )

    # Build collection context hint
    collection_hint = ""
    col_lower = (collection or "").lower()
    if col_lower in ("cine", "peliculas", "movies", "film"):
        collection_hint = (
            "\nCONTEXTO: Este video es de la coleccion CINE. Usa VARIEDAD de tipos: "
            "~40-50% 'clip_bank' para footage especifico de peliculas (trailers, behind-the-scenes, VFX), "
            "~25-35% 'stock_video' para tomas genéricas relacionadas (explosiones, ciudades, tecnologia, naturaleza), "
            "~10-15% 'title_card' para titulos numerados o introducciones de seccion, "
            "~5% 'ai_image' solo para conceptos muy abstractos. "
            "NO pongas todo como clip_bank — mezcla tipos para un video mas interesante."
        )
    elif col_lower in ("tech", "tecnologia", "technology"):
        collection_hint = (
            "\nCONTEXTO: Video de tecnologia. Priorizar 'clip_bank' para footage tech "
            "y 'stock_video' para tomas genericas."
        )
    elif col_lower in ("historia", "history"):
        collection_hint = (
            "\nCONTEXTO: Video historico. Priorizar 'archive_footage' para eventos reales "
            "y 'clip_bank' para footage documental."
        )

    system_prompt = (
        "Eres un editor de video profesional. Analizas cada escena de un video "
        "para decidir que tipo de visual necesita. "
        "Devuelve SOLO un JSON array. Sin markdown, sin explicacion."
    )

    # Build asset type descriptions — only include allowed types
    type_descriptions = {
        "clip_bank": '"clip_bank": footage ESPECIFICO de nuestro banco de clips local. Usar para: escenas que mencionan peliculas especificas, behind-the-scenes, VFX, escenas tematicas de la coleccion.',
        "stock_video": '"stock_video": footage de VIDEO GENERICO buscado en internet (Pexels/Pixabay). Usar para: tomas de ciudades, naturaleza, tecnologia, personas, acciones genericas.',
        "title_card": '"title_card": para titulos de seccion numerados (ej: \'#10 Miniatures Over CGI\'). Se buscara una IMAGEN DE FONDO en internet y se pondra el TEXTO ENCIMA.',
        "web_image": '"web_image": IMAGEN buscada en internet (Pexels/Pixabay/Google). Usar para: fotos reales de objetos, lugares, personas, eventos. NO es video, es una imagen fija de alta calidad.',
        "ai_image": '"ai_image": imagen generada por IA. Usar para conceptos abstractos o cuando no hay otra opcion.',
        "archive_footage": '"archive_footage": eventos historicos reales: guerras, revoluciones, presidentes, documentos antiguos.',
        "space_media": '"space_media": espacio, planetas, NASA, astronomia, cohetes.',
    }

    if allowed_types:
        types_block = "\n".join(f"   - {type_descriptions[t]}" for t in allowed_types if t in type_descriptions)
        types_constraint = f"\nIMPORTANTE: SOLO puedes usar estos tipos: {', '.join(allowed_types)}. NO uses ningun otro tipo."
    else:
        types_block = "\n".join(f"   - {v}" for v in type_descriptions.values())
        types_constraint = ""

    title_context = ""
    if project_title:
        title_context = f"\nTITULO DEL VIDEO: {project_title}\nIMPORTANTE: Las search queries DEBEN ser especificas al tema del video. Si el video es sobre una pelicula, incluye el nombre de la pelicula. Si es sobre una persona, incluye su nombre. Las queries deben ser tan especificas que al buscar en Google encuentres la imagen exacta que necesitas para esa escena.\n"

    user_prompt = f"""Analiza cada escena de este video para decidir que tipo de visual necesita.
{title_context}
GUION COMPLETO (para contexto):
{full_script}
{collection_hint}{types_constraint}

ESCENAS:
{scenes_text}

Para CADA escena, decide:

1. asset_type — Que tipo de visual necesita (USA VARIEDAD, no pongas todo igual):
{types_block}

2. search_query — Termino de busqueda en INGLES, maximo 5-7 palabras, visual y ESPECIFICO AL TEMA DEL VIDEO. Si el video habla de "Independence Day (1996)", la query debe incluir "Independence Day 1996" no solo "explosion". Pensa: que escribirias en Google Images para encontrar exactamente la imagen que necesita esta escena?

3. search_query_alt — Termino alternativo mas generico por si el primero no da resultados.

4. has_overlay_text — true si la escena es un titulo de seccion numerado o una introduccion que necesita texto sobre el visual.

5. overlay_text — Si has_overlay_text es true, un titulo CORTO de 2-5 palabras maximo (ej: '#10 Miniatures Over CGI', 'Independence Day', '20 Hidden Facts', 'The Hidden Truth'). NUNCA uses la frase completa de la escena — genera un titulo BREVE y cinematografico. Si es false, null.

Devuelve SOLO un JSON array:
[
  {{
    "scene_id": 1,
    "asset_type": "clip_bank",
    "search_query": "movie explosion practical effects",
    "search_query_alt": "explosion fire vfx",
    "has_overlay_text": false,
    "overlay_text": null
  }}
]"""

    _safe_print(f"[VisualAnalyzer] Analyzing {len(scenes)} scenes via local Claude Code...")

    raw = _call_claude_local(user_prompt, system=system_prompt)
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    results = json.loads(raw)

    if not isinstance(results, list):
        raise ValueError(f"Expected JSON array, got: {type(results)}")

    # Enforce allowed_types — fix any violations
    if allowed_types:
        allowed_set = set(allowed_types)
        fallback = allowed_types[0]
        for r in results:
            if r.get("asset_type") not in allowed_set:
                _safe_print(f"[VisualAnalyzer] Scene {r.get('scene_id')}: type '{r.get('asset_type')}' not allowed, using '{fallback}'")
                r["asset_type"] = fallback

    for r in results:
        _safe_print(
            f"[VisualAnalyzer] Scene {r.get('scene_id')}: "
            f"type={r.get('asset_type')}, query='{r.get('search_query')}'"
            f"{' [OVERLAY: ' + r.get('overlay_text', '') + ']' if r.get('has_overlay_text') else ''}"
        )

    return results
