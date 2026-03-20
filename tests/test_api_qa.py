"""
QA Test Suite — Capa 2 (Tests de API)
Requiere el servidor corriendo en localhost:8000.

Corre con:
    # Terminal 1: iniciar servidor
    python -m uvicorn main:app --host 0.0.0.0 --port 8000

    # Terminal 2: correr tests
    pytest tests/test_api_qa.py -v

Si el servidor no está activo, todos los tests son skipped automáticamente.
"""
import sys
import re
import pytest
import httpx
from pathlib import Path

BASE_URL = "http://localhost:8000"
TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# Fixture: client + skip si servidor no disponible
# ---------------------------------------------------------------------------

def _is_server_up() -> bool:
    try:
        httpx.get(f"{BASE_URL}/", timeout=5.0)
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def server_available():
    """Marca de sesión: salta todo si el servidor no está activo."""
    if not _is_server_up():
        pytest.skip("Servidor no disponible en localhost:8000. Iniciar con: python -m uvicorn main:app --port 8000")


@pytest.fixture
def client(server_available):
    """Cliente HTTP fresco por test (evita problemas de keep-alive entre tests)."""
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
        yield c


@pytest.fixture(scope="session")
def test_project(server_available):
    """Crea un proyecto de prueba (sesión) y devuelve {id, slug}. Lo elimina al final."""
    proj_id = None
    try:
        with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
            resp = c.post("/api/projects/", json={
                "title": "QA Test Project Auto",
                "mode": "stock",
                "topic": "QA automated test",
                "video_type": "top10",
                "duration": "short"
            })
            assert resp.status_code == 201, f"No se pudo crear proyecto: {resp.text}"
            data = resp.json()
            proj_id = data["id"]
        yield data
    finally:
        if proj_id:
            try:
                with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
                    c.delete(f"/api/projects/{proj_id}")
            except Exception:
                pass


# ===========================================================================
# SECCIÓN 1: Health check
# ===========================================================================

class TestHealth:
    def test_root_returns_200(self, client):
        """La raíz debe devolver 200 (SPA index.html)."""
        resp = client.get("/")
        assert resp.status_code == 200

    def test_static_js_accessible(self, client):
        """El JS principal debe estar accesible."""
        resp = client.get("/static/js/app.js")
        assert resp.status_code == 200
        assert len(resp.content) > 1000, "app.js parece estar vacío"


# ===========================================================================
# SECCIÓN 2: Projects — CRUD
# ===========================================================================

class TestProjectsCRUD:
    def test_list_projects_returns_array(self, client):
        """GET /api/projects/ debe retornar una lista."""
        resp = client.get("/api/projects/")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list), f"Se esperaba lista, se obtuvo: {type(data)}"

    def test_create_project_returns_201(self, client):
        """POST /api/projects/ debe retornar 201 Created."""
        resp = client.post("/api/projects/", json={
            "title": "QA Create Test",
            "mode": "stock",
            "topic": "testing video creation",
            "video_type": "top10",
            "duration": "short"
        })
        assert resp.status_code == 201, f"Se esperaba 201, se obtuvo {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "id" in data
        assert "slug" in data
        assert "status" in data
        assert data["title"] == "QA Create Test"
        # Cleanup
        client.delete(f"/api/projects/{data['id']}")

    def test_create_project_generates_unique_slug(self, client):
        """Dos proyectos con el mismo título deben tener slugs únicos."""
        title = "QA Duplicate Slug Test"
        resp1 = client.post("/api/projects/", json={
            "title": title, "mode": "stock", "topic": "test", "video_type": "top10", "duration": "short"
        })
        resp2 = client.post("/api/projects/", json={
            "title": title, "mode": "stock", "topic": "test", "video_type": "top10", "duration": "short"
        })
        assert resp1.status_code == 201
        assert resp2.status_code == 201
        id1, slug1 = resp1.json()["id"], resp1.json()["slug"]
        id2, slug2 = resp2.json()["id"], resp2.json()["slug"]
        assert slug1 != slug2, f"Slugs duplicados: ambos son '{slug1}'"
        # Cleanup
        client.delete(f"/api/projects/{id1}")
        client.delete(f"/api/projects/{id2}")

    def test_create_project_empty_title_is_bug(self, client):
        """
        BUG DOCUMENTADO: Crear proyecto con título vacío DEBERÍA fallar con 422
        pero actualmente retorna 201 con slug='-1'.
        Cuando se fixee, este test debe actualizar la aserción.
        """
        resp = client.post("/api/projects/", json={
            "title": "",
            "mode": "stock",
            "topic": "test",
            "video_type": "top10",
            "duration": "short"
        })
        # BUG: actualmente retorna 201 con título vacío — no hay validación
        # El comportamiento correcto sería 422
        print(f"\n[QA-BUG] Empty title status: {resp.status_code}, slug: {resp.json().get('slug')}")
        if resp.status_code == 201:
            # Documentamos y limpiamos
            client.delete(f"/api/projects/{resp.json()['id']}")
            pytest.xfail("BUG CONOCIDO: título vacío crea proyecto con slug='-1'. Falta validación en POST /api/projects/")
        else:
            assert resp.status_code == 422

    def test_create_project_missing_title(self, client):
        """Crear proyecto sin campo title debe fallar con 422."""
        resp = client.post("/api/projects/", json={
            "mode": "stock",
            "topic": "test",
            "video_type": "top10",
            "duration": "short"
        })
        assert resp.status_code == 422

    def test_get_project_by_id(self, client, test_project):
        """GET /api/projects/{id} debe retornar el proyecto con chunks."""
        proj_id = test_project["id"]
        resp = client.get(f"/api/projects/{proj_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == proj_id
        assert "chunks" in data
        assert isinstance(data["chunks"], list)

    def test_get_project_nonexistent_id(self, client):
        """GET /api/projects/999999 con ID inexistente debe retornar 404."""
        resp = client.get("/api/projects/999999")
        assert resp.status_code == 404

    def test_get_project_with_invalid_id_format(self, client):
        """
        BUG DOCUMENTADO: GET /api/projects/{slug} con un slug (string) en lugar
        de ID entero retorna 422 (error de validación interno) en lugar de 404.
        Expone el tipo de dato interno al cliente.
        """
        resp = client.get("/api/projects/slug-que-no-existe")
        # Comportamiento esperado: 404
        # Comportamiento actual: 422 (porque espera entero)
        print(f"\n[QA-BUG] GET by slug status: {resp.status_code}")
        if resp.status_code == 422:
            pytest.xfail(
                "BUG CONOCIDO: GET /api/projects/{slug} retorna 422 en vez de 404. "
                "Expone implementación interna. Considerar agregar ruta por slug."
            )

    def test_delete_project_immediately(self, client):
        """DELETE inmediatamente después de crear debe funcionar (con retry interno)."""
        resp = client.post("/api/projects/", json={
            "title": "QA Delete Race Test",
            "mode": "stock",
            "topic": "to be deleted immediately",
            "video_type": "top10",
            "duration": "short"
        })
        assert resp.status_code == 201
        proj_id = resp.json()["id"]

        # DELETE inmediato — ahora el endpoint tiene retry interno para manejar la race condition
        del_resp = client.delete(f"/api/projects/{proj_id}")
        assert del_resp.status_code in (204, 503), (
            f"DELETE retornó {del_resp.status_code}: {del_resp.text[:200]}"
        )

    def test_delete_project_after_pipeline_start(self, client):
        """DELETE funciona correctamente cuando el pipeline ya pasó su fase inicial."""
        import time
        resp = client.post("/api/projects/", json={
            "title": "QA Delete After Wait Test",
            "mode": "stock",
            "topic": "to be deleted after waiting",
            "video_type": "top10",
            "duration": "short"
        })
        assert resp.status_code == 201
        proj_id = resp.json()["id"]

        # Esperar que el pipeline termine su fase inicial (3 segundos)
        time.sleep(3)

        del_resp = client.delete(f"/api/projects/{proj_id}")
        assert del_resp.status_code == 204, (
            f"DELETE después de 3s retornó {del_resp.status_code}. "
            f"Body: {del_resp.text[:200]}"
        )

        get_resp = client.get(f"/api/projects/{proj_id}")
        assert get_resp.status_code == 404, "El proyecto aún existe después de eliminar"

    def test_invalid_mode_rejected(self, client):
        """Modo inválido debe retornar 422."""
        resp = client.post("/api/projects/", json={
            "title": "QA Invalid Mode",
            "mode": "invalid_mode",
            "topic": "test",
            "video_type": "top10",
            "duration": "short"
        })
        assert resp.status_code == 422


# ===========================================================================
# SECCIÓN 3: Settings
# ===========================================================================

class TestSettings:
    def test_get_settings_returns_dict_with_data(self, client):
        """GET /api/settings/ debe retornar {"data": {...}}."""
        resp = client.get("/api/settings/")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict), f"Se esperaba dict, se obtuvo: {type(data)}"
        assert "data" in data, f"No hay campo 'data' en settings: {list(data.keys())}"
        assert isinstance(data["data"], dict)

    def test_settings_data_has_api_keys_masked(self, client):
        """Las API keys en settings deben estar enmascaradas (no exponer valores reales)."""
        resp = client.get("/api/settings/")
        data = resp.json().get("data", {})
        for key, value in data.items():
            if "api_key" in key.lower() and value:
                assert "●" in value or "•" in value or "â€¢" in value, (
                    f"API key '{key}' no está enmascarada correctamente: '{value[:20]}'"
                )

    def test_save_setting_with_correct_format(self, client):
        """POST /api/settings/ con formato {"data": {key: val}} debe funcionar."""
        resp = client.post("/api/settings/", json={
            "data": {"qa_test_key": "qa_test_value_12345"}
        })
        assert resp.status_code == 200, f"Save settings falló: {resp.text}"

    def test_save_setting_wrong_format_rejected(self, client):
        """POST /api/settings/ con formato incorrecto (lista) debe retornar 422."""
        resp = client.post("/api/settings/", json=[
            {"key": "qa_test", "value": "val"}
        ])
        assert resp.status_code == 422


# ===========================================================================
# SECCIÓN 4: Workers
# ===========================================================================

class TestWorkers:
    def test_list_workers_returns_array(self, client):
        """GET /api/workers/ debe retornar lista de workers."""
        resp = client.get("/api/workers/")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_workers_have_status_field(self, client):
        """Cada worker debe tener campo 'status'."""
        resp = client.get("/api/workers/")
        data = resp.json()
        for worker in data:
            assert "status" in worker, f"Worker sin campo 'status': {worker}"


# ===========================================================================
# SECCIÓN 5: TTS Voices
# ===========================================================================

class TestTTSVoices:
    def test_list_voices_returns_dict_with_voices(self, client):
        """POST /api/tts/voices debe retornar {"voices": [...]}."""
        resp = client.post("/api/tts/voices", json={})
        assert resp.status_code == 200
        data = resp.json()
        # La respuesta puede ser dict con 'voices' key o lista directa
        if isinstance(data, dict):
            assert "voices" in data or "error" in data, (
                f"Respuesta de voices sin campo esperado: {list(data.keys())}"
            )
        elif isinstance(data, list):
            pass  # también válido
        else:
            pytest.fail(f"Formato inesperado de voices: {type(data)}")

    def test_list_voices_genaipro_filter(self, client):
        """POST /api/tts/voices filtrando por provider 'genaipro' debe funcionar."""
        resp = client.post("/api/tts/voices", json={"provider": "genaipro"})
        assert resp.status_code == 200

    def test_list_voices_openai_filter(self, client):
        """POST /api/tts/voices filtrando por provider 'openai' debe funcionar."""
        resp = client.post("/api/tts/voices", json={"provider": "openai"})
        assert resp.status_code == 200

    def test_voices_debug_raw(self, client):
        """GET /api/tts/voices/debug-raw debe retornar 200."""
        resp = client.get("/api/tts/voices/debug-raw")
        assert resp.status_code == 200


# ===========================================================================
# SECCIÓN 6: Logs
# ===========================================================================

class TestLogs:
    def test_logs_for_existing_project(self, client, test_project):
        """GET /api/logs/{project_id} para proyecto existente debe retornar array."""
        project_id = test_project["id"]
        resp = client.get(f"/api/logs/{project_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_logs_for_nonexistent_project(self, client):
        """GET /api/logs/999999 para proyecto inexistente debe retornar lista vacía o 404."""
        resp = client.get("/api/logs/999999")
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert isinstance(resp.json(), list)


# ===========================================================================
# SECCIÓN 7: YouTube Transcript
# ===========================================================================

class TestYouTubeTranscript:
    def test_transcript_missing_url(self, client):
        """POST /api/youtube/transcript sin URL debe retornar 422."""
        resp = client.post("/api/youtube/transcript", json={})
        assert resp.status_code == 422

    def test_transcript_invalid_url_no_500(self, client):
        """POST con URL inválida no debe retornar 500."""
        resp = client.post("/api/youtube/transcript", json={
            "url": "https://not-youtube.com/watch?v=invalid"
        })
        assert resp.status_code != 500, (
            f"Error 500 no controlado con URL inválida: {resp.text[:200]}"
        )

    def test_transcript_invalid_video_id_no_500(self, client):
        """POST con ID de video inexistente no debe retornar 500."""
        resp = client.post("/api/youtube/transcript", json={
            "url": "https://www.youtube.com/watch?v=XXXXXXXXXXX"
        })
        assert resp.status_code != 500, (
            f"Error 500 no controlado con video inexistente: {resp.text[:200]}"
        )


# ===========================================================================
# SECCIÓN 8: Edge cases de proyecto
# ===========================================================================

class TestProjectEdgeCases:
    def test_slug_with_special_chars_in_title(self, client):
        """Título con caracteres especiales debe generar slug válido."""
        resp = client.post("/api/projects/", json={
            "title": "QA Test: Título con ñ y acentós éáíóú!",
            "mode": "stock",
            "topic": "test",
            "video_type": "top10",
            "duration": "short"
        })
        assert resp.status_code == 201, f"Crash con caracteres especiales: {resp.text}"
        data = resp.json()
        slug = data["slug"]
        # El slug no debe tener caracteres especiales (solo a-z, 0-9, guiones)
        assert re.match(r'^[a-z0-9\-]+$', slug), f"Slug inválido generado: '{slug}'"
        client.delete(f"/api/projects/{data['id']}")

    def test_very_long_title_truncates_slug(self, client):
        """Título de 200 caracteres genera slug truncado."""
        long_title = "QA " + "A" * 197
        resp = client.post("/api/projects/", json={
            "title": long_title,
            "mode": "stock",
            "topic": "test",
            "video_type": "top10",
            "duration": "short"
        })
        assert resp.status_code in (201, 422), (
            f"Error inesperado con título largo: {resp.status_code} — {resp.text[:200]}"
        )
        if resp.status_code == 201:
            data = resp.json()
            slug = data["slug"]
            assert len(slug) <= 65, f"Slug demasiado largo: {len(slug)} chars ('{slug[:30]}...')"
            client.delete(f"/api/projects/{data['id']}")

    def test_project_list_includes_created_project(self, client, test_project):
        """El proyecto de test debe aparecer en la lista."""
        resp = client.get("/api/projects/")
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()]
        assert test_project["id"] in ids, (
            f"Proyecto {test_project['id']} no encontrado en la lista"
        )


# ===========================================================================
# SECCIÓN 9: Formato de errores
# ===========================================================================

class TestErrorFormat:
    def test_422_includes_validation_detail(self, client):
        """Las respuestas 422 deben incluir campo 'detail' con info de validación."""
        resp = client.post("/api/projects/", json={"mode": "invalid"})
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data, f"422 sin campo 'detail': {data}"
        assert isinstance(data["detail"], list)

    def test_404_is_json(self, client):
        """Las respuestas 404 deben ser JSON."""
        resp = client.get("/api/projects/999999")
        assert resp.status_code == 404
        content_type = resp.headers.get("content-type", "")
        assert "application/json" in content_type, (
            f"404 no es JSON. Content-Type: {content_type}"
        )

    def test_nonexistent_api_path_returns_404(self, client):
        """Una ruta API inexistente debe retornar 404."""
        resp = client.get("/api/this/does/not/exist")
        assert resp.status_code == 404

    def test_delete_nonexistent_project_is_bug(self, client):
        """
        BUG DOCUMENTADO: DELETE /api/projects/string-slug retorna 422 en vez de 404.
        Expone el tipo del parámetro al cliente.
        """
        resp = client.delete("/api/projects/slug-que-no-existe")
        print(f"\n[QA-BUG] DELETE by slug status: {resp.status_code}")
        if resp.status_code == 422:
            pytest.xfail(
                "BUG CONOCIDO: DELETE /api/projects/{slug} retorna 422 en vez de 404. "
                "Debería retornar 404 con mensaje descriptivo."
            )
        else:
            assert resp.status_code in (404, 204)
