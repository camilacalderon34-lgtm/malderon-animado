"""Meta AI Playwright bot – image animation via web automation.

Cookie/session is persisted in `meta_session/` at the project root.
For parallel execution, sessions are cloned to `meta_session_N/` directories.
Meta AI actively detects headless browsers, so we always run in headful mode.

Uses SYNC Playwright API (Python 3.14 on Windows breaks async subprocess).
"""
import asyncio
import shutil
import sys
import time
import threading
import multiprocessing
from pathlib import Path
from playwright.sync_api import sync_playwright


def _ensure_event_loop():
    """Ensure a working asyncio event loop exists in the current thread.

    On Windows, Playwright needs a ProactorEventLoop to spawn browser
    subprocesses. When running inside a daemon thread (e.g. from uvicorn),
    asyncio may not have a loop or may have one that doesn't support
    subprocess_exec. This forces a fresh ProactorEventLoop.
    """
    if sys.platform == "win32":
        try:
            loop = asyncio.ProactorEventLoop()
            asyncio.set_event_loop(loop)
        except Exception:
            pass

# ── Paths ──────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
META_SESSION_DIR = _PROJECT_ROOT / "meta_session"

# ── Timeouts (milliseconds) ───────────────────────────────────────────────────
NAV_TIMEOUT = 120_000       # 2 min
ELEMENT_TIMEOUT = 120_000   # 2 min
GENERATION_TIMEOUT = 360_000  # 6 min

# ── Browser launch arguments ──────────────────────────────────────────────────
_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]

# ── Parallel session management ──────────────────────────────────────────────

def _worker_session_dir(worker_id: int) -> Path:
    """Return session directory for a specific worker."""
    if worker_id == 0:
        return META_SESSION_DIR
    return _PROJECT_ROOT / f"meta_session_{worker_id}"


def prepare_parallel_sessions(num_workers: int = 5):
    """Clone the main meta_session/ to N worker directories."""
    if not META_SESSION_DIR.exists():
        raise RuntimeError(
            f"Meta AI session not found at {META_SESSION_DIR}. "
            "Run `python run_meta_login.py` first."
        )
    for i in range(1, num_workers):
        dst = _worker_session_dir(i)
        if dst.exists():
            continue
        print(f"[META] Cloning session to {dst.name}...")
        shutil.copytree(str(META_SESSION_DIR), str(dst), dirs_exist_ok=True)
    print(f"[META] {num_workers} parallel sessions ready.")


# ── Login helper ──────────────────────────────────────────────────────────────

def setup_meta_login():
    META_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[META] Session directory: {META_SESSION_DIR}")

    _ensure_event_loop()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(META_SESSION_DIR),
            headless=False,
            args=_BROWSER_ARGS,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.meta.ai/media")

        print("[META] Login window open — please log in and close the browser when done.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        finally:
            ctx.close()

    print(f"[META] Session saved: {META_SESSION_DIR}")


# ── Selector helpers ──────────────────────────────────────────────────────────

def _find_textarea(page):
    selectors = [
        '[aria-label*="message" i][contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
        'textarea[aria-label*="message" i]',
        'textarea[placeholder]',
        'textarea',
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=5_000)
            return el
        except Exception:
            continue
    return None


def _attach_image(page, image_path: str) -> bool:
    # Strategy 1: direct file input
    try:
        fi = page.locator("input[type='file']").first
        fi.wait_for(state="attached", timeout=10_000)
        fi.set_input_files(image_path)
        return True
    except Exception:
        pass

    # Strategy 2: click attach button
    attach_selectors = [
        'button[aria-label*="ttach" i]',
        'button[aria-label*="image" i]',
        'button[aria-label*="photo" i]',
        'div[role="button"][aria-label*="ttach" i]',
        '[data-testid*="attach" i]',
        '[data-testid*="image-upload" i]',
        'label[for*="file"]',
        'label[for*="upload"]',
    ]
    for sel in attach_selectors:
        try:
            btn = page.locator(sel).first
            btn.wait_for(state="visible", timeout=5_000)
            with page.expect_file_chooser(timeout=10_000) as fc_info:
                btn.click()
            fc = fc_info.value
            fc.set_files(image_path)
            return True
        except Exception:
            continue

    # Strategy 3: fallback
    try:
        page.set_input_files("input[type='file']", image_path)
        return True
    except Exception:
        pass
    return False


def _save_debug_screenshot(page, output_path: str, label: str) -> str:
    try:
        debug = str(Path(output_path).parent / f"meta_debug_{label}.png")
        page.screenshot(path=debug, full_page=True)
        return debug
    except Exception:
        return "(screenshot failed)"


def _download_video(page, video_el, output_path: str):
    """Try multiple strategies to download the generated video."""

    # Strategy A: Download button
    dl_selectors = [
        'div[role="button"][aria-label*="ownload" i]',
        'button[aria-label*="ownload" i]',
        'button:has-text("Download")',
        'button:has-text("Descargar")',
        '[data-testid*="download" i]',
        'a[download]',
        'a[href*=".mp4"]',
    ]
    for sel in dl_selectors:
        try:
            candidate = page.locator(sel).last
            candidate.wait_for(state="visible", timeout=5_000)
            with page.expect_download(timeout=ELEMENT_TIMEOUT) as dl_info:
                candidate.click()
            download = dl_info.value
            download.save_as(output_path)
            return
        except Exception:
            continue

    # Strategy B: Extract video src URL
    import httpx as _httpx

    video_src = video_el.get_attribute("src")
    if not video_src:
        try:
            source_el = video_el.locator("source").first
            video_src = source_el.get_attribute("src")
        except Exception:
            pass

    if not video_src:
        video_src = page.evaluate("""
            () => {
                const v = document.querySelector('video');
                return v ? v.src || v.currentSrc : null;
            }
        """)

    if video_src and video_src.startswith("http"):
        import httpx as _httpx
        with _httpx.Client(timeout=120.0) as http:
            r = http.get(video_src)
            r.raise_for_status()
            Path(output_path).write_bytes(r.content)
        return

    scr = _save_debug_screenshot(page, output_path, "no_src")
    raise RuntimeError(f"Could not extract video URL. Debug screenshot: {scr}")


def _switch_to_video_mode(page, tag: str, output_path: str) -> bool:
    """Click the 'Imagen' dropdown then select 'Vídeo'. Returns True on success."""

    # 1. Open the dropdown by clicking the "Imagen" pill
    dropdown_opened = False
    for sel in [
        'button:has-text("Imagen")',
        '[role="button"]:has-text("Imagen")',
        'span:has-text("Imagen")',
        'button:has-text("Image")',
    ]:
        try:
            btn = page.locator(sel).first
            btn.wait_for(state="visible", timeout=5_000)
            btn.click()
            time.sleep(2)
            dropdown_opened = True
            print(f"{tag} Dropdown opened.")
            break
        except Exception:
            continue

    if not dropdown_opened:
        scr = _save_debug_screenshot(page, output_path, "no_dropdown")
        print(f"{tag} WARNING: Could not open Imagen dropdown. Screenshot: {scr}")
        return False

    # 2. Click "Vídeo" via JavaScript
    clicked = page.evaluate("""
        () => {
            const targets = ['Vídeo', 'Video', 'vídeo', 'video'];
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const text = (el.textContent || '').trim();
                const inner = (el.innerText || '').trim();
                const isLeaf = el.children.length === 0 ||
                    (el.children.length <= 2 && inner.length < 20);
                if (isLeaf && targets.includes(text) && el.offsetParent !== null) {
                    el.click();
                    return text;
                }
            }
            return null;
        }
    """)

    if clicked:
        print(f"{tag} Switched to Video mode (clicked '{clicked}').")
        time.sleep(1)
        return True

    # 3. Fallback: Playwright text selectors
    for v_sel in [
        'text="Vídeo"', 'text="Video"',
        'div:text-is("Vídeo")', 'span:text-is("Vídeo")',
        'div:text-is("Video")', 'span:text-is("Video")',
    ]:
        try:
            v_btn = page.locator(v_sel).last
            v_btn.wait_for(state="visible", timeout=2_000)
            v_btn.click()
            time.sleep(1)
            print(f"{tag} Switched to Video mode via fallback selector.")
            return True
        except Exception:
            continue

    scr = _save_debug_screenshot(page, output_path, "no_video_option")
    print(f"{tag} WARNING: Could not select Vídeo option. Screenshot: {scr}")
    return False


# ── Single scene in an existing page ─────────────────────────────────────────

def _animate_in_page(page, image_path: str, motion_prompt: str,
                     output_path: str, tag: str):
    """
    Animate an image using Meta AI in Video mode.
    Navigates to meta.ai/media, attaches image, switches to Video mode, sends prompt, downloads video.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"{tag} Navigating to meta.ai...")
    page.goto(
        "https://www.meta.ai/",
        wait_until="domcontentloaded",
        timeout=NAV_TIMEOUT,
    )
    time.sleep(1)

    # Step 1: Attach image
    attached = _attach_image(page, image_path)
    if not attached:
        scr = _save_debug_screenshot(page, output_path, "no_attach")
        raise RuntimeError(f"Could not attach image. Debug: {scr}")
    time.sleep(1)

    # Step 2: Fill prompt
    textarea = _find_textarea(page)
    if not textarea:
        scr = _save_debug_screenshot(page, output_path, "no_textarea")
        raise RuntimeError(f"Could not find chat input. Debug: {scr}")

    full_prompt = (
        f"{motion_prompt}. "
        "Animate this image with natural smooth motion. "
        "Do not change the art style, colors, or characters."
    )
    textarea.click()
    textarea.fill(full_prompt)
    time.sleep(0.5)

    # Count existing video elements BEFORE submitting
    existing_video_count = page.locator("video").count()

    textarea.press("Enter")
    print(f"{tag} Prompt sent: {full_prompt[:80]}...")

    # Step 3: Wait for a NEW video element
    video_el = None
    deadline = time.monotonic() + (GENERATION_TIMEOUT / 1000)
    while time.monotonic() < deadline:
        try:
            current_count = page.locator("video").count()
            if current_count > existing_video_count:
                vid = page.locator("video").last
                vid.wait_for(state="visible", timeout=10_000)
                video_el = vid
                print(f"{tag} NEW video detected! (was {existing_video_count}, now {current_count})")
                break
        except Exception:
            pass
        time.sleep(2)

    if not video_el:
        scr = _save_debug_screenshot(page, output_path, "no_video")
        raise RuntimeError(
            f"No video after {GENERATION_TIMEOUT // 1000}s. Debug: {scr}"
        )

    time.sleep(2)

    # Step 4: Download
    _download_video(page, video_el, output_path)
    print(f"{tag} Animation saved: {output_path}")


# ── Standalone single-scene entry point (opens/closes browser) ───────────────

def animate_scene(image_path: str, motion_prompt: str, output_path: str,
                  worker_id: int = 0):
    """
    Automate Meta AI to generate a short video clip from a still image.
    Opens and closes its own browser — use animate_batch() for multiple scenes.
    """
    session_dir = _worker_session_dir(worker_id)
    if not session_dir.exists():
        raise RuntimeError(f"Session dir not found: {session_dir}")

    tag = f"[META-W{worker_id}]"

    _ensure_event_loop()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            headless=False,
            args=_BROWSER_ARGS,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(ELEMENT_TIMEOUT)
        try:
            _animate_in_page(page, image_path, motion_prompt,
                             output_path, tag)
        finally:
            ctx.close()


# ── Batch parallel animation ─────────────────────────────────────────────────

def _worker_loop(worker_id: int, tasks: list, results: list, total: int,
                 lock: threading.Lock, on_scene_done=None):
    """
    Single worker thread: opens ONE browser, processes tasks from shared list.
    Resilient: if browser launch fails, worker exits silently without killing others.
    If a task fails with browser-closed error, worker stops taking new tasks.
    """
    # Stagger browser launches to avoid Playwright race conditions
    time.sleep(worker_id * 2)

    tag = f"[META-W{worker_id}]"
    session_dir = _worker_session_dir(worker_id)
    if not session_dir.exists():
        print(f"{tag} Session dir not found: {session_dir}, skipping worker.")
        return

    print(f"{tag} Launching Chromium...")
    try:
        _ensure_event_loop()
        pw = sync_playwright().start()
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            headless=False,
            args=_BROWSER_ARGS,
            viewport={"width": 1280, "height": 800},
        )
    except Exception as exc:
        print(f"{tag} FAILED to launch browser: {exc}")
        return

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(ELEMENT_TIMEOUT)

    try:
        while True:
            with lock:
                if not tasks:
                    break
                item = tasks.pop(0)
            chunk_number, image_path, motion_prompt, output_path = item
            try:
                print(f"{tag} Starting scene #{chunk_number}...")
                _animate_in_page(page, image_path, motion_prompt,
                                 output_path, tag)
                with lock:
                    results.append((chunk_number, None))
                if on_scene_done:
                    on_scene_done(chunk_number, None)
                done = sum(1 for _, e in results if e is None)
                print(f"{tag} Scene #{chunk_number} done ({done}/{total})")
            except Exception as exc:
                err_msg = str(exc)
                with lock:
                    results.append((chunk_number, err_msg))
                if on_scene_done:
                    on_scene_done(chunk_number, err_msg)
                print(f"{tag} Scene #{chunk_number} FAILED: {exc}")
                # If browser died, stop this worker (don't consume more tasks)
                if "closed" in err_msg.lower() or "crashed" in err_msg.lower():
                    print(f"{tag} Browser appears dead, stopping worker.")
                    break
    finally:
        print(f"{tag} All tasks done, closing browser.")
        try:
            ctx.close()
            pw.stop()
        except Exception:
            pass


def animate_batch(tasks_input: list[tuple], num_workers: int = 5,
                  on_scene_done=None):
    """
    Animate scenes using multiple parallel browser workers.

    Each worker gets its own cloned session directory and processes tasks
    from a shared list. Workers run in threads, each with its own browser.

    tasks_input: list of (chunk_number, image_path, motion_prompt, output_path)
    on_scene_done: optional callback(chunk_number, error_or_None) called after each scene
    Returns: list of (chunk_number, error_or_None)
    """
    num_workers = min(num_workers, len(tasks_input))
    prepare_parallel_sessions(num_workers)

    tasks = list(tasks_input)
    results: list[tuple[int, str | None]] = []
    total = len(tasks_input)
    lock = threading.Lock()

    threads = []
    for wid in range(num_workers):
        t = threading.Thread(
            target=_worker_loop,
            args=(wid, tasks, results, total, lock, on_scene_done),
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    return results
