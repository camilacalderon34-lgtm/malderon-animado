"""Test: Copy DICloak cookies to a fresh Chrome profile, then open Google Flow."""
from playwright.sync_api import sync_playwright
from pathlib import Path
import shutil
import time

URL = "https://labs.google/fx/es-419/tools/flow/project/641cdede-6513-4bbe-9ee2-5743bd981ff3"
SCREENSHOT = Path(__file__).parent / "flow_ui_screenshot.png"

DICLOAK_PROFILE = Path(r"D:\.DICloakCache\2031149312501518337\ud_2031149312501518337")
FLOW_SESSION = Path(__file__).parent / "flow_session"


def main():
    # Copy key files from DICloak profile to our session
    FLOW_SESSION.mkdir(parents=True, exist_ok=True)
    default_src = DICLOAK_PROFILE / "Default"
    default_dst = FLOW_SESSION / "Default"
    default_dst.mkdir(parents=True, exist_ok=True)

    # Copy cookies, local storage, session storage
    for item in ["Cookies", "Cookies-journal",
                 "Local Storage", "Session Storage",
                 "Web Data", "Web Data-journal",
                 "Preferences", "Secure Preferences"]:
        src = default_src / item
        dst = default_dst / item
        if src.exists():
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(str(dst))
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
            print(f"  Copied: {item}")

    print(f"[FLOW] Session prepared at {FLOW_SESSION}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(FLOW_SESSION),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            viewport={"width": 1400, "height": 900},
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(f"[FLOW] Navigating to {URL} ...")
        page.goto(URL, timeout=60_000, wait_until="domcontentloaded")

        print("[FLOW] Waiting for page to render...")
        time.sleep(15)

        print(f"[FLOW] Current URL: {page.url}")

        # Screenshot
        try:
            page.screenshot(path=str(SCREENSHOT), full_page=False)
            print(f"[FLOW] Screenshot saved: {SCREENSHOT}")
        except Exception as e:
            print(f"[FLOW] Screenshot failed: {e}")

        # Dump interactive elements
        print("\n=== INTERACTIVE ELEMENTS ===")
        for tag in ["button", "input", "textarea",
                     "[contenteditable]", "[role='textbox']"]:
            try:
                elements = page.locator(tag).all()
                visible = [el for el in elements if el.is_visible()]
                if visible:
                    print(f"\n--- {tag} ({len(visible)} visible) ---")
                    for i, el in enumerate(visible[:20]):
                        try:
                            text = el.inner_text(timeout=2000)[:100]
                            aria = el.get_attribute("aria-label") or ""
                            placeholder = el.get_attribute("placeholder") or ""
                            print(f"  [{i}] text={text!r} aria={aria!r} ph={placeholder!r}")
                        except:
                            print(f"  [{i}] (could not read)")
            except:
                pass

        print("\n[FLOW] Browser open 60s for inspection...")
        time.sleep(60)
        ctx.close()


if __name__ == "__main__":
    main()
