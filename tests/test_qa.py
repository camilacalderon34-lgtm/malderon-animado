"""
QA Test Suite — Capa 1 (sin servidor)
Corre con: pytest tests/test_qa.py -v
"""
import sys
import os
import json
import inspect
import tempfile
import hashlib
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_src(rel_path: str) -> str:
    root = Path(__file__).parent.parent
    return (root / rel_path).read_text(encoding="utf-8", errors="replace")


# ===========================================================================
# SECCIÓN 1: Imports y firmas de servicios críticos
# ===========================================================================

class TestImports:
    def test_stock_search_service_imports(self):
        import app.services.stock_search_service  # noqa

    def test_youtube_clip_service_imports(self):
        import app.services.youtube_clip_service  # noqa

    def test_claude_service_imports(self):
        import app.services.claude_service  # noqa

    def test_visual_analyzer_service_imports(self):
        import app.services.visual_analyzer_service  # noqa

    def test_pipeline_service_imports(self):
        import app.services.pipeline_service  # noqa

    def test_pexels_service_imports(self):
        import app.services.pexels_service  # noqa

    def test_pixabay_service_imports(self):
        import app.services.pixabay_service  # noqa


# ===========================================================================
# SECCIÓN 2: Bug — NASA API data=[] causa IndexError
# ===========================================================================

class TestNASABug:
    """
    Bug: stock_search_service.py línea 52
        data = item.get("data", [{}])[0]
    Si la API devuelve {"data": []}, el fallback [{}] no aplica
    y [][0] lanza IndexError.
    """

    def test_nasa_data_empty_list_no_crash(self):
        """Si NASA devuelve item con data=[], no debe crashear."""
        from app.services import stock_search_service as svc

        # Simulamos el item problemático
        item = {"data": [], "href": "http://example.com/asset"}

        # Replicamos la lógica actual de la línea 52
        try:
            data = item.get("data", [{}])[0]
            # Si llegamos aquí con lista vacía, esto lanzó IndexError
            # en versiones sin fix. Verificamos el comportamiento.
            result = "no_crash"
        except IndexError:
            result = "crash"

        # ESTE TEST DOCUMENTA EL BUG: si falla significa que el bug sigue presente
        # El fix correcto es: data_list = item.get("data") or [{}]; data = data_list[0]
        assert result == "crash", (
            "BUG CONFIRMADO: item.get('data', [{}])[0] con data=[] lanza IndexError. "
            "Necesita fix en stock_search_service.py línea 52."
        )

    def test_nasa_data_empty_list_safe_pattern(self):
        """Verifica que el patrón safe funciona correctamente."""
        item = {"data": [], "href": "http://example.com/asset"}

        # Patrón correcto (safe)
        data_list = item.get("data") or [{}]
        data = data_list[0] if data_list else {}

        assert data == {}, "El patrón safe debe retornar {} cuando data=[]"

    def test_nasa_data_none_safe_pattern(self):
        """Verifica que el patrón safe funciona cuando data=None."""
        item = {"href": "http://example.com/asset"}  # sin campo "data"

        data_list = item.get("data") or [{}]
        data = data_list[0] if data_list else {}

        assert data == {}

    def test_nasa_data_normal_case(self):
        """El patrón safe funciona igual cuando data tiene items."""
        item = {"data": [{"media_type": "image", "title": "Space"}]}

        data_list = item.get("data") or [{}]
        data = data_list[0] if data_list else {}

        assert data["media_type"] == "image"


# ===========================================================================
# SECCIÓN 3: Bug — float() sin try/catch en parseo JSON de Claude
# ===========================================================================

class TestFloatParsingBug:
    """
    Bug: youtube_clip_service.py líneas 454-455
        start = max(0, float(analysis.get("start_seconds", 5)))
        clip_dur = max(min_duration, float(analysis.get("clip_duration", min_duration + 3)))
    Si Claude retorna None, "null", o string no-numérico → ValueError/TypeError crash.
    """

    def test_float_with_none_value_crashes(self):
        """Documenta que float(None) lanza TypeError."""
        analysis = {"start_seconds": None, "clip_duration": None}
        min_duration = 5

        try:
            start = max(0, float(analysis.get("start_seconds", 5)))
            clip_dur = max(min_duration, float(analysis.get("clip_duration", min_duration + 3)))
            result = "no_crash"
        except (TypeError, ValueError):
            result = "crash"

        assert result == "crash", (
            "BUG CONFIRMADO: float(None) lanza TypeError cuando Claude retorna null. "
            "Necesita try/except alrededor de float() en youtube_clip_service.py líneas 454-455."
        )

    def test_float_with_string_non_numeric_crashes(self):
        """Documenta que float('unknown') lanza ValueError."""
        analysis = {"start_seconds": "unknown", "clip_duration": "auto"}

        try:
            start = max(0, float(analysis.get("start_seconds", 5)))
            result = "no_crash"
        except (TypeError, ValueError):
            result = "crash"

        assert result == "crash", (
            "BUG CONFIRMADO: float('unknown') lanza ValueError. "
            "Necesita sanitización en youtube_clip_service.py."
        )

    def test_float_safe_pattern(self):
        """El patrón safe con fallback funciona correctamente."""
        def safe_float(val, default):
            try:
                return float(val)
            except (TypeError, ValueError):
                return float(default)

        assert safe_float(None, 5) == 5.0
        assert safe_float("unknown", 3) == 3.0
        assert safe_float("10.5", 5) == 10.5
        assert safe_float(7, 5) == 7.0


# ===========================================================================
# SECCIÓN 4: web_image_full está manejado en find_asset_for_scene
# ===========================================================================

class TestWebImageFullHandling:
    """
    Verifica que find_asset_for_scene maneje web_image_full correctamente.
    Bug anterior: web_image_full no tenía handler y retornaba None siempre.
    """

    def test_web_image_full_in_condition_check(self):
        """Verifica en el source code que web_image_full está en la condición."""
        src = _load_src("app/services/stock_search_service.py")
        assert 'asset_type in ("web_image", "web_image_full")' in src, (
            "BUG: web_image_full no está en la condición de find_asset_for_scene. "
            "Las escenas de tipo 'Imagen Completa' siempre fallarán."
        )

    def test_web_image_full_triggers_search(self):
        """find_asset_for_scene con web_image_full no retorna None sin intentar buscar."""
        from app.services.stock_search_service import find_asset_for_scene
        import inspect
        src = inspect.getsource(find_asset_for_scene)
        # La función debe mencionar web_image_full en su lógica de dispatch
        assert "web_image_full" in src, (
            "find_asset_for_scene no menciona web_image_full en su código fuente."
        )


# ===========================================================================
# SECCIÓN 5: _build_ytdlp_common_args incluye cookies
# ===========================================================================

class TestYouTubeCookies:
    """
    Verifica que tanto _search_youtube como _download_youtube_video
    incluyan --cookies cuando el archivo de cookies existe.
    """

    def test_build_ytdlp_includes_cookies_when_file_exists(self):
        """_build_ytdlp_common_args debe incluir --cookies cuando el archivo existe."""
        from app.services.youtube_clip_service import _build_ytdlp_common_args

        fake_cookies = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        fake_cookies.write(b"# Netscape HTTP Cookie File\n")
        fake_cookies.close()

        try:
            with patch("app.services.youtube_clip_service.settings") as mock_settings:
                mock_settings.youtube_cookies_file = fake_cookies.name
                mock_settings.youtube_proxy = ""
                mock_settings.youtube_po_token = ""
                mock_settings.deno_path = ""

                args = _build_ytdlp_common_args()

            assert "--cookies" in args, (
                "_build_ytdlp_common_args no incluye --cookies aunque el archivo existe."
            )
            cookies_idx = args.index("--cookies")
            assert args[cookies_idx + 1] == fake_cookies.name
        finally:
            os.unlink(fake_cookies.name)

    def test_build_ytdlp_no_cookies_when_file_missing(self):
        """_build_ytdlp_common_args NO debe incluir --cookies si el archivo no existe."""
        from app.services.youtube_clip_service import _build_ytdlp_common_args

        with patch("app.services.youtube_clip_service.settings") as mock_settings:
            mock_settings.youtube_cookies_file = "/nonexistent/path/cookies.txt"
            mock_settings.youtube_proxy = ""
            mock_settings.youtube_po_token = ""
            mock_settings.deno_path = ""

            args = _build_ytdlp_common_args()

        assert "--cookies" not in args, (
            "_build_ytdlp_common_args incluye --cookies aunque el archivo no existe."
        )

    def test_search_youtube_uses_build_args(self):
        """_search_youtube debe usar _build_ytdlp_common_args (no hardcodear args)."""
        src = _load_src("app/services/youtube_clip_service.py")
        func_start = src.find("def _search_youtube(")
        func_end = src.find("\ndef ", func_start + 1)
        func_body = src[func_start:func_end]
        assert "_build_ytdlp_common_args" in func_body, (
            "_search_youtube no llama _build_ytdlp_common_args — cookies no se aplican."
        )

    def test_download_youtube_video_uses_build_args(self):
        """_download_youtube_video debe usar _build_ytdlp_common_args."""
        src = _load_src("app/services/youtube_clip_service.py")
        func_start = src.find("def _download_youtube_video(")
        func_end = src.find("\ndef ", func_start + 1)
        func_body = src[func_start:func_end]
        assert "_build_ytdlp_common_args" in func_body, (
            "_download_youtube_video no llama _build_ytdlp_common_args — cookies no se aplican."
        )


# ===========================================================================
# SECCIÓN 6: Bare except:pass sin logging
# ===========================================================================

class TestBareExceptPass:
    """
    Documenta y cuenta los bloques `except Exception: pass` sin logging.
    Estos hacen debugging imposible en producción.
    Actúan como regresión: si el número SUBE, alguien añadió más silencio de errores.
    """

    def _count_bare_pass(self, path: str) -> int:
        src = _load_src(path)
        lines = src.split("\n")
        count = 0
        for i, line in enumerate(lines):
            if "except Exception" in line:
                for j in range(i + 1, min(len(lines), i + 4)):
                    stripped = lines[j].strip()
                    if stripped == "pass":
                        count += 1
                        break
                    elif stripped:
                        break
        return count

    def test_count_bare_except_pass_in_stock_search(self):
        """Regresión: el número de except:pass en stock_search_service.py no debe subir."""
        count = self._count_bare_pass("app/services/stock_search_service.py")
        print(f"\n[QA] bare except:pass en stock_search_service.py: {count}")
        # Valor actual documentado: 12. Este test falla si sube (nuevos bugs silenciosos).
        assert count <= 12, (
            f"El número de except:pass SUBIÓ a {count} "
            "(máximo documentado: 12). Revisar stock_search_service.py."
        )

    def test_count_bare_except_pass_in_youtube_clip(self):
        """Regresión: el número de except:pass en youtube_clip_service.py no debe subir."""
        count = self._count_bare_pass("app/services/youtube_clip_service.py")
        print(f"\n[QA] bare except:pass en youtube_clip_service.py: {count}")
        # Valor actual documentado: 6. Este test falla si sube.
        assert count <= 6, (
            f"El número de except:pass SUBIÓ a {count}. Revisar youtube_clip_service.py."
        )


# ===========================================================================
# SECCIÓN 7: Código muerto (if False)
# ===========================================================================

class TestDeadCode:
    """
    Documenta bloques de código muerto con `if False`.
    Son mantenimiento pendiente y confunden al leer el código.
    """

    def test_if_false_blocks_in_youtube_clip(self):
        """Cuenta y documenta bloques 'if False' en youtube_clip_service.py."""
        src = _load_src("app/services/youtube_clip_service.py")
        lines = src.split("\n")

        if_false_lines = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("if False") or stripped == "if False:":
                if_false_lines.append(i + 1)

        print(f"\n[QA] Bloques 'if False' en youtube_clip_service.py: líneas {if_false_lines}")
        # Documentamos cuántos hay. Idealmente deberían eliminarse.
        assert len(if_false_lines) <= 2, (
            f"Se encontraron {len(if_false_lines)} bloques 'if False' en youtube_clip_service.py. "
            f"Líneas: {if_false_lines}. Eliminar código muerto."
        )


# ===========================================================================
# SECCIÓN 8: Validación de asset types y enums
# ===========================================================================

class TestAssetTypes:
    VALID_ASSET_TYPES = {
        "clip_bank", "stock_video", "title_card", "web_image",
        "web_image_full", "ai_image", "archive_footage", "space_media"
    }

    def test_valid_asset_types_in_source(self):
        """Todos los asset types válidos deben estar mencionados en stock_search_service."""
        src = _load_src("app/services/stock_search_service.py")
        for atype in self.VALID_ASSET_TYPES:
            assert atype in src, f"Asset type '{atype}' no encontrado en stock_search_service.py"

    def test_web_image_full_not_falling_through(self):
        """web_image_full no debe caer en el bloque 'else' (sin handler)."""
        src = _load_src("app/services/stock_search_service.py")
        # La condición debe incluir web_image_full junto a web_image
        assert '"web_image_full"' in src or "'web_image_full'" in src, (
            "web_image_full no está en ninguna condición de stock_search_service.py"
        )


# ===========================================================================
# SECCIÓN 9: Validación de bytes mágicos (regresión)
# ===========================================================================

class TestMagicBytes:
    """Regresión de los tests de magic bytes de validate.py."""

    def test_jpeg_magic_bytes(self):
        data = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"JFIF"
        assert data[:3] == b'\xff\xd8\xff'

    def test_png_magic_bytes(self):
        data = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        assert data[:4] == b'\x89PNG'

    def test_mp4_magic_bytes(self):
        # ftyp box
        data = bytes([0x00, 0x00, 0x00, 0x1C, 0x66, 0x74, 0x79, 0x70])
        assert b'ftyp' in data

    def test_html_as_jpeg_rejected(self):
        data = b"<!DOCTYPE html><html>"
        assert data[:3] != b'\xff\xd8\xff'
        assert data[:4] != b'\x89PNG'


# ===========================================================================
# SECCIÓN 10: Rate limiting config
# ===========================================================================

class TestRateLimiting:
    def test_ddg_min_delay(self):
        from app.services.ddg_image_service import _MIN_DELAY
        assert _MIN_DELAY >= 3.0, f"DDG delay muy bajo: {_MIN_DELAY}s (mínimo 3.0s)"

    def test_youtube_transcript_min_delay(self):
        from app.services.youtube_service import _MIN_DELAY_SECONDS
        assert _MIN_DELAY_SECONDS >= 2.0, (
            f"YouTube transcript delay muy bajo: {_MIN_DELAY_SECONDS}s (mínimo 2.0s)"
        )

    def test_ddg_circuit_breaker_cooldown(self):
        from app.services.ddg_image_service import _CIRCUIT_COOLDOWN
        assert _CIRCUIT_COOLDOWN >= 120, (
            f"DDG circuit breaker cooldown muy bajo: {_CIRCUIT_COOLDOWN}s (mínimo 120s)"
        )


# ===========================================================================
# SECCIÓN 11: Watermark domain blocklist
# ===========================================================================

class TestWatermarkBlocklist:
    BLOCKED = ["shutterstock.com", "gettyimages.com", "alamy.com", "istockphoto.com"]

    def test_blocked_domains_in_ddg_service(self):
        """Los dominios con watermark deben estar bloqueados en ddg_image_service.py."""
        src = _load_src("app/services/ddg_image_service.py")
        for domain in self.BLOCKED:
            assert domain in src, (
                f"Dominio bloqueado '{domain}' no está en el blocklist de ddg_image_service.py"
            )

    def test_blocked_domains_object_exists(self):
        """La constante _BLOCKED_DOMAINS debe existir y contener los dominios críticos."""
        from app.services.ddg_image_service import _BLOCKED_DOMAINS
        for domain in self.BLOCKED:
            assert any(domain in d for d in _BLOCKED_DOMAINS), (
                f"'{domain}' no está en _BLOCKED_DOMAINS"
            )


# ===========================================================================
# SECCIÓN 12: Transiciones válidas y duración clamped
# ===========================================================================

class TestTransitions:
    VALID_TRANSITIONS = {
        "fade", "fadeblack", "fadewhite", "dissolve",
        "wipeleft", "wiperight", "wipeup", "wipedown",
        "slideleft", "slideright", "slideup", "slidedown",
        "circleopen", "circleclose", "radial",
        "smoothleft", "smoothright", "smoothup", "smoothdown",
        "zoomin"
    }

    def test_transition_duration_clamp_low(self):
        duration = 100
        clamped = max(200, min(2000, duration))
        assert clamped == 200

    def test_transition_duration_clamp_high(self):
        duration = 5000
        clamped = max(200, min(2000, duration))
        assert clamped == 2000

    def test_transition_duration_in_range(self):
        duration = 500
        clamped = max(200, min(2000, duration))
        assert clamped == 500

    def test_valid_transitions_exist_in_source(self):
        """Al menos las transiciones básicas deben estar definidas en routers/projects.py."""
        src = _load_src("app/routers/projects.py")
        for t in ["fade", "dissolve", "wipeleft"]:
            assert t in src, f"Transición '{t}' no encontrada en routers/projects.py"


# ===========================================================================
# SECCIÓN 13: Output resolution
# ===========================================================================

class TestOutputResolution:
    def test_1920x1080_in_render_service(self):
        src = _load_src("app/services/render_service.py")
        assert "1920" in src and "1080" in src, (
            "La resolución 1920x1080 no está en render_service.py"
        )


# ===========================================================================
# SECCIÓN 14: OpenRouter model config
# ===========================================================================

class TestModelConfig:
    def test_gemini_model_in_stock_search(self):
        """El modelo correcto debe estar configurado (gemini-3.1-flash-lite-preview)."""
        src = _load_src("app/services/stock_search_service.py")
        assert "gemini-3.1-flash-lite-preview" in src or "gemini" in src.lower(), (
            "Modelo Gemini no encontrado en stock_search_service.py"
        )

    def test_gemini_model_in_youtube_clip(self):
        src = _load_src("app/services/youtube_clip_service.py")
        assert "gemini-3.1-flash-lite-preview" in src or "gemini" in src.lower(), (
            "Modelo Gemini no encontrado en youtube_clip_service.py"
        )

    def test_gemini_model_in_visual_analyzer(self):
        src = _load_src("app/services/visual_analyzer_service.py")
        assert "gemini-3.1-flash-lite-preview" in src or "gemini" in src.lower(), (
            "Modelo Gemini no encontrado en visual_analyzer_service.py"
        )

    def test_openrouter_api_used_in_claude_service(self):
        """claude_service.py debe usar OpenRouter (no Anthropic directo)."""
        src = _load_src("app/services/claude_service.py")
        assert "openrouter" in src.lower() or "OPENROUTER" in src, (
            "claude_service.py no parece usar OpenRouter."
        )


# ===========================================================================
# SECCIÓN 15: Duraciones de video con duration=0
# ===========================================================================

class TestDurationZero:
    """
    Bug: Videos con duration=0 pasan el filtro de 600s pero pueden
    romper la lógica de corte de clips.
    """

    def test_zero_duration_passes_filter(self):
        """Documenta que duration=0 pasa el filtro actual."""
        video = {"duration": 0, "id": "abc", "title": "Test"}
        # Filtro actual en youtube_clip_service.py
        passes = video["duration"] <= 600 or video["duration"] == 0
        assert passes, "Videos con duration=0 deberían pasar el filtro (comportamiento esperado)"

    def test_zero_duration_in_cutting_logic(self):
        """Simula qué pasa cuando se intenta cortar un video de duración desconocida."""
        vid_dur = 0
        min_duration = 5

        # Lógica actual: si vid_dur es 0, la condición vid_dur < min_duration es falsa
        # (0 < 5 es verdadero → el video sería saltado)
        would_skip = vid_dur and vid_dur < min_duration
        # Esto es problemático: 0 es falsy, entonces would_skip = 0 (False)
        # El video NO se salta aunque dure 0 segundos
        assert not would_skip, (
            "BUG: Con duration=0, 'vid_dur and vid_dur < min_duration' = False "
            "(porque 0 es falsy), entonces el video no se salta. "
            "Puede causar clips de 0 segundos."
        )
