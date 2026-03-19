"""Visual Analyzer — uses OpenRouter API (Gemini Flash Lite) to decide what type of visual each scene needs.

Also provides image validation: checks if a downloaded image actually matches the scene.
"""

import base64
import json
import mimetypes
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict

from openai import OpenAI as _OpenAI
from ..config import settings

_MODEL = "google/gemini-3.1-flash-lite-preview"

_openrouter = _OpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
)


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


def _call_claude_api(prompt: str, system: str = "") -> str:
    """Call Claude Sonnet via OpenRouter API (fast, no subprocess overhead)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = _openrouter.chat.completions.create(
        model=_MODEL,
        messages=messages,
        max_tokens=4096,
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()


# Keep legacy alias for any callers that still use _call_claude_local
def _call_claude_local(prompt: str, system: str = "") -> str:
    return _call_claude_api(prompt, system)


# ── Vision via OpenRouter API (Gemini Flash Lite with base64 images) ──

def _call_claude_vision(image_path: str | Path, prompt: str, max_turns: int = 3) -> str:
    """Analyze an image using Gemini Flash Lite via OpenRouter API.

    Reads the image from disk, encodes it as base64, and sends it to the API.
    Retries up to 2 times on error.
    """
    abs_path = Path(image_path).resolve()

    # Read and encode image
    image_data = abs_path.read_bytes()
    b64 = base64.b64encode(image_data).decode("utf-8")
    mime = mimetypes.guess_type(str(abs_path))[0] or "image/jpeg"

    last_error = None
    for attempt in range(1, 3):
        try:
            _safe_print(f"[Vision] API vision call attempt {attempt}/2...")
            resp = _openrouter.chat.completions.create(
                model=_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                max_tokens=1024,
                temperature=0.1,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            last_error = str(exc)
            _safe_print(f"[Vision] Attempt {attempt}/2 error: {last_error}")
            continue

    raise RuntimeError(f"Vision API failed after 2 attempts: {last_error}")


# ── Image validation with Vision ────────────────────────────────────────────


def validate_image(
    image_path: str | Path,
    scene_text: str,
    search_query: str,
    project_title: str = "",
    flexible: bool = False,
    collection: str = "general",
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
        col_lower = (collection or "general").lower()
        _is_comida = col_lower.startswith("comida") or "comida" in col_lower

        if _is_comida:
            # For comida: product packaging, store imagery ARE what we want
            prompt = (
                f'{context}\n'
                f'The scene narration says: "{scene_text[:300]}"\n'
                f'We searched for: "{search_query}"\n\n'
                f'This is for a video about UK supermarket food products.\n'
                f'Does this image show something RELEVANT to what the scene describes?\n\n'
                f'- YES: product packaging, branded food products, store shelves, supermarket aisles\n'
                f'- YES: brand logos, store exteriors, nutrition labels, price tags\n'
                f'- YES: food production, factory, manufacturing process images\n'
                f'- YES: real photos of the specific product or brand mentioned\n'
                f'- NO: cooked food, recipes, plated meals, generic stock food imagery\n'
                f'- NO: completely unrelated content\n'
                f'- NO: images with WATERMARKS (SHUTTERSTOCK, GETTY, etc.)\n\n'
                f'Answer ONLY "YES" or "NO".'
            )
        elif flexible:
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
                f'- Images with WATERMARKS (PREVIEW, SAMPLE, STOCK, SHUTTERSTOCK, GETTY, any semi-transparent text overlay) = NO.\n'
                f'- Images dominated by PRODUCT PACKAGING, store labels, price tags, or commercial branding that covers most of the frame = NO.\n'
                f'- Real photos/screenshots that match the specific topic, WITHOUT watermarks = YES.\n'
                f'- Clean, visually appealing images that show the SUBJECT (not just its packaging) = YES.\n\n'
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
    script_context: str = "",
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

        # Add full script context so AI thinks like an editor
        script_block = ""
        if script_context:
            script_block = (
                f'\nFULL VIDEO SCRIPT (for editorial context):\n{script_context}\n'
                f'As a video editor, this frame should visually fit the narrative above.\n\n'
            )

        prompt = (
            f'{context}\n'
            f'{script_block}'
            f'Scene narration: "{scene_text[:300]}"\n'
            f'Search query: "{search_query}"\n\n'
            f'This is a frame from a YouTube video clip meant as B-roll footage.\n'
            f'We need CLEAN footage with NO text overlays — it will be used as silent B-roll.\n\n'
            f'REJECT ONLY if:\n'
            f'- It has a WATERMARK (text like "PREVIEW", "SAMPLE", "STOCK", "SHUTTERSTOCK", "GETTY", "POND5", "ADOBE STOCK", any semi-transparent text overlay)\n'
            f'- It has BURNED-IN TEXT OVERLAYS: ranking numbers ("NUMBER 8:", "#5", "Top 10"), channel names, captions, lower-thirds, any text graphics added in editing\n'
            f'- It\'s a MOVIE POSTER or promotional image (static, not footage)\n'
            f'- It\'s a TEXT SLIDE (text on dark/plain background, like trailer text)\n'
            f'- It\'s a TITLE CARD, credits, or intro/outro screen\n'
            f'- It\'s a talking head / face close-up with NO relevant context visible\n\n'
            f'ACCEPT if:\n'
            f'- It shows CLEAN REAL VIDEO FOOTAGE with NO text overlays of any kind\n'
            f'- It shows anything related to the OVERALL VIDEO TOPIC (not just this one scene)\n'
            f'- It looks like actual filmed content (not a designed graphic)\n'
            f'- The frame is CLEAN — no watermarks, no burned-in text\n\n'
            f'IMPORTANT: Do NOT reject footage just because it doesn\'t match the exact scene text.\n'
            f'If the footage relates to the overall video topic, ACCEPT it.\n'
            f'Only reject for watermarks, text overlays, or completely unrelated content.\n'
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
    type_weights: dict | None = None,
    project_title: str = "",
) -> List[Dict]:
    """Analyze each scene and decide asset_type + search_query.

    Args:
        full_script: complete narration text (for context)
        scenes: list of dicts with at least 'id' and 'texto'
        collection: project collection name (e.g. 'cine', 'tech') for context
        allowed_types: if set, only these types can be used
        type_weights: dict mapping asset_type -> target % (e.g. {"stock_video": 70, "web_image": 20})
        project_title: title of the video project (for search context)

    Returns:
        list of dicts per scene: scene_id, asset_type, search_query, search_query_alt,
        has_overlay_text, overlay_text
    """
    # Truncate script for context (saves tokens per block call)
    script_context = full_script[:3000] if full_script else ""

    # Split into blocks of 20 scenes
    blocks = [scenes[i:i + 20] for i in range(0, len(scenes), 20)]
    total_blocks = len(blocks)
    _safe_print(f"[VisualAnalyzer] {len(scenes)} scenes → {total_blocks} blocks (parallel via OpenRouter)")

    results_by_index: dict[int, list] = {}

    # Run up to 5 blocks in parallel
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(
                _analyze_block,
                script_context, block, collection, allowed_types, project_title,
                type_weights,
            ): idx
            for idx, block in enumerate(blocks)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            results_by_index[idx] = fut.result()
            done = len(results_by_index)
            _safe_print(f"[VisualAnalyzer] Block {done}/{total_blocks} done")

    # Reassemble in order
    all_results = []
    for idx in range(total_blocks):
        all_results.extend(results_by_index[idx])

    # Post-processing: enforce type_weights distribution
    if type_weights and all_results:
        all_results = _enforce_type_distribution(all_results, type_weights, allowed_types)

    return all_results


def _get_collection_search_guide(collection: str) -> str:
    """Return collection-specific search query guidance for the visual analyzer."""
    guides = {
        "comida": (
            "   GUIA PARA VIDEOS DE COMIDA/PRODUCTOS UK:\n"
            "   REGLA DE ORO: Busca el PRODUCTO EXACTO + MARCA + SUPERMERCADO del guion.\n"
            "   Supermercados UK comunes: Tesco, Morrisons, Co-op, Lidl, Asda, M&S, Sainsbury's, Aldi, Iceland, Waitrose\n\n"
            "   PARA CADA TIPO DE ASSET:\n"
            "   - clip_bank: busca REVIEWS de productos, UNBOXINGS, tours de supermercado, procesos de fabricacion\n"
            "     Ej: 'Tesco beef mince review', 'UK supermarket meat aisle tour', 'how ground beef is made factory'\n"
            "   - web_image / web_image_full: busca FOTOS del packaging real, logos de supermercados, etiquetas nutricionales\n"
            "     Ej: 'Tesco 20% fat beef mince packaging', 'Morrisons store logo', 'beef mince nutrition label UK'\n"
            "   - stock_video: busca procesos de produccion, fabricas, lineas de ensamblaje del producto\n"
            "     Ej: 'ground beef production factory conveyor belt', 'meat processing plant UK'\n\n"
            "   REGLAS CRITICAS:\n"
            "   - SIEMPRE incluye el nombre del SUPERMERCADO o MARCA si se menciona en el guion\n"
            "   - Si habla de un proceso (como se hace la carne molida), busca ESE proceso industrial\n"
            "   - Si habla de grasa/calidad/precio, busca el PRODUCTO con esos detalles especificos\n"
            "   - NUNCA busques comida cocinada, recetas, o platos preparados\n"
            "   - NUNCA busques imagenes genericas de stock de comida\n"
            "   - NUNCA busques conceptos abstractos (health warning, obesity symbol, unhealthy diet)\n"
            "   - Si la escena habla de grasa en la sarten, busca 'beef mince grease drain' o 'fatty mince in pan', NO 'cooking beef recipe'\n"
            "   - Si la escena habla de consecuencias de salud, busca 'beef mince fat content label' NO 'obesity warning'\n"
            "   - MAL: 'cooking pot stew', 'delicious beef meal', 'fresh meat cutting board'\n"
            "   - MAL: 'cooking beef mince in frying pan' (es receta, no producto)\n"
            "   - MAL: 'person eating unhealthy fast food' (generico, no producto)\n"
            "   - MAL: 'obesity health warning symbol' (abstracto, no producto)\n"
            "   - MAL: 'ground beef' (demasiado generico) cuando dice 'Tesco sells mince with 37% fat'\n"
            "   - BIEN: 'Tesco 20 percent fat beef mince packaging UK'\n"
            "   - BIEN: 'frozen beef mince supermarket shelf UK'\n"
            "   - BIEN: 'ground beef factory production line industrial'\n"
            "   - BIEN: 'beef mince grease fat draining pan' (si habla de grasa al cocinar)\n"
            "   - BIEN: 'beef mince high fat content UK label' (si habla de consecuencias)"
        ),
        "cine": (
            "   GUIA PARA VIDEOS DE CINE/PELICULAS:\n"
            "   - Incluye SIEMPRE el nombre de la pelicula/serie en la query\n"
            "   - Si habla de una escena, busca ESA escena (ej: 'Independence Day 1996 White House explosion')\n"
            "   - Si habla de un actor/director, incluye su nombre (ej: 'Tom Hanks Forrest Gump bench scene')\n"
            "   - Si habla de efectos especiales, busca el VFX de ESA pelicula\n"
            "   - NUNCA busques cosas genericas — siempre referencia la pelicula/persona concreta"
        ),
        "tech": (
            "   GUIA PARA VIDEOS DE TECNOLOGIA:\n"
            "   - Busca el PRODUCTO/DISPOSITIVO exacto mencionado (ej: 'iPhone 16 Pro Max camera module')\n"
            "   - Si habla de una empresa, busca ESA empresa (ej: 'NVIDIA headquarters Jensen Huang')\n"
            "   - Si habla de software, busca screenshots o logos de ESE software\n"
            "   - NUNCA busques iconos genericos de tecnologia"
        ),
    }
    # Match by prefix: "comida_uk" → "comida", "cine_terror" → "cine"
    col_lower = (collection or "general").lower()
    guide = None
    for key in guides:
        if col_lower.startswith(key) or key in col_lower:
            guide = guides[key]
            break
    if guide is None:
        guide = (
            "   - Busca el OBJETO, PERSONA, PRODUCTO o LUGAR CONCRETO que el narrador menciona\n"
            "   - NUNCA busques conceptos abstractos o genericos\n"
            "   - USA nombres propios, marcas, titulos especificos del guion"
        )
    return guide


def _enforce_type_distribution(
    results: List[Dict], type_weights: dict, allowed_types: list | None
) -> List[Dict]:
    """Adjust asset_type distribution to match target percentages.
    Only reassigns if a type deviates by more than 10 percentage points."""
    total = len(results)
    if total == 0:
        return results

    # Don't count title_card scenes — those are contextual, not weighted
    non_title = [r for r in results if r.get("asset_type") != "title_card"]
    title_cards = [r for r in results if r.get("asset_type") == "title_card"]
    n = len(non_title)
    if n == 0:
        return results

    # Calculate target counts
    target_counts = {}
    total_weight = sum(type_weights.values())
    if total_weight <= 0:
        return results
    for t, w in type_weights.items():
        target_counts[t] = round(n * w / total_weight)

    # Current counts
    current_counts = {}
    for r in non_title:
        at = r.get("asset_type", "stock_video")
        current_counts[at] = current_counts.get(at, 0) + 1

    # Check deviations
    needs_adjustment = False
    for t, target in target_counts.items():
        current = current_counts.get(t, 0)
        deviation_pp = abs(current - target) / n * 100
        if deviation_pp > 5:
            needs_adjustment = True
            break

    if not needs_adjustment:
        _safe_print("[VisualAnalyzer] Distribution within tolerance, no adjustment needed")
        return results

    # Find over-represented and under-represented types
    over = {}  # type -> excess count
    under = {}  # type -> deficit count
    for t, target in target_counts.items():
        current = current_counts.get(t, 0)
        if current > target:
            over[t] = current - target
        elif current < target:
            under[t] = target - current

    _safe_print(f"[VisualAnalyzer] Adjusting distribution: over={over}, under={under}")

    # Reassign excess scenes to deficit types
    for r in non_title:
        at = r.get("asset_type", "stock_video")
        if at in over and over[at] > 0:
            # Find the most under-represented type to reassign to
            best_target = max(under, key=lambda t: under[t], default=None)
            if best_target and under[best_target] > 0:
                _safe_print(
                    f"[VisualAnalyzer] Scene {r.get('scene_id')}: "
                    f"reassigning {at} → {best_target}"
                )
                r["asset_type"] = best_target
                over[at] -= 1
                under[best_target] -= 1

    return title_cards + non_title


def _analyze_block(
    full_script: str, scenes: List[Dict], collection: str = "general",
    allowed_types: list | None = None, project_title: str = "",
    type_weights: dict | None = None,
) -> List[Dict]:
    """Analyze a block of up to 20 scenes via OpenRouter API."""
    scenes_text = "\n".join(
        f"Escena {s['id']}: \"{s['texto']}\""
        for s in scenes
    )

    # Build distribution hint from type_weights (replaces hardcoded collection hints)
    collection_hint = ""
    if type_weights:
        n_scenes = len(scenes)
        dist_lines = []
        for t, w in type_weights.items():
            if w > 0:
                target_n = max(1, round(n_scenes * w / sum(type_weights.values())))
                dist_lines.append(f"   - {t}: EXACTAMENTE {target_n} escenas ({w}%)")
        dist_text = "\n".join(dist_lines)
        collection_hint = (
            f"\nDISTRIBUCION OBLIGATORIA (NO es una sugerencia, DEBES cumplirla):\n{dist_text}\n"
            "DEBES variar los tipos de asset. NO pongas mas de 2 escenas seguidas del mismo tipo.\n"
            "Alterna entre clip_bank, web_image, web_image_full y stock_video para crear variedad visual.\n"
            "title_card es una excepcion: usalo cuando la escena sea un titulo de seccion, sin importar los porcentajes."
        )

    system_prompt = (
        "Eres un EDITOR DE VIDEO PROFESIONAL con 20 años de experiencia en YouTube. "
        "Tu trabajo es leer el guion COMPLETO, entender el CONTEXTO de cada escena "
        "dentro de la narrativa total, y decidir EXACTAMENTE que imagen o video "
        "debe aparecer en pantalla mientras el narrador dice cada frase.\n\n"
        "REGLA DE ORO: La imagen debe ilustrar LITERALMENTE lo que dice el narrador. "
        "Piensa: 'Si yo fuera un espectador, que esperaria VER en pantalla "
        "mientras escucho esta frase?' La respuesta NUNCA es algo generico — "
        "siempre es el OBJETO, PERSONA, PRODUCTO o LUGAR CONCRETO que se menciona.\n\n"
        "Devuelve SOLO un JSON array. Sin markdown, sin explicacion."
    )

    # Build asset type descriptions — only include allowed types
    type_descriptions = {
        "clip_bank": '"clip_bank": VIDEO buscado en YouTube y nuestro banco de clips. Usar para: escenas que mencionan peliculas, series, behind-the-scenes, trailers, reviews, documentales, VFX, escenas tematicas. Es VIDEO, no imagen.',
        "stock_video": '"stock_video": footage de VIDEO GENERICO buscado en internet (Pexels/Pixabay). Usar para: tomas de ciudades, naturaleza, tecnologia, personas, acciones genericas.',
        "title_card": '"title_card": para titulos de seccion numerados (ej: \'#10 Miniatures Over CGI\'). Se buscara una IMAGEN DE FONDO en internet y se pondra el TEXTO ENCIMA.',
        "web_image": '"web_image": IMAGEN ANIMADA buscada en internet con fondo difuminado, marco y gradiente tematico. Usar para: fotos reales de objetos, lugares, personas, eventos. Estilo enmarcado cinematografico.',
        "web_image_full": '"web_image_full": IMAGEN COMPLETA a pantalla completa con zoom sutil (Ken Burns). Sin marco, sin fondo. Usar para: paisajes, fondos inmersivos, imagenes que deben llenar toda la pantalla.',
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
        title_context = (
            f"\nTITULO DEL VIDEO: {project_title}\n"
            f"CONTEXTO CRITICO: Todo el video trata sobre este tema. CADA search query debe reflejar "
            f"el tema especifico del video. Lee el guion completo para entender de que habla y usa "
            f"nombres propios, marcas, productos, personas o lugares CONCRETOS mencionados en el guion.\n"
            f"NUNCA busques cosas genericas — siempre busca lo ESPECIFICO que el narrador menciona.\n"
        )

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

2. search_query — Termino de busqueda en INGLES, maximo 5-8 palabras. DEBE describir EXACTAMENTE lo que el espectador necesita VER en pantalla mientras escucha esa frase.
   PIENSA: si pegas esta query en Google Images, el PRIMER resultado deberia ser EXACTAMENTE lo que quieres mostrar.
{_get_collection_search_guide(collection)}

3. search_query_alt — Termino alternativo en INGLES, mas generico pero SIEMPRE relacionado al tema del video. Nunca una query completamente diferente.

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

    _safe_print(f"[VisualAnalyzer] Analyzing {len(scenes)} scenes via OpenRouter ({_MODEL})...")

    raw = _call_claude_api(user_prompt, system=system_prompt)
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
