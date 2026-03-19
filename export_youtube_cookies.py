"""
Export YouTube cookies from Chrome to a cookies.txt file that yt-dlp can use.

IMPORTANT: Close Chrome completely before running this script.
Run: python export_youtube_cookies.py
"""
import os
import sys
import shutil
import sqlite3
import tempfile
import struct
import json
from pathlib import Path
from datetime import datetime, timezone

OUTPUT_FILE = Path(__file__).parent / "youtube_cookies.txt"

# Try to decrypt Chrome cookies on Windows
def _decrypt_value(encrypted_value: bytes) -> str:
    if not encrypted_value:
        return ""

    # v10/v20 AES-GCM encrypted (Chrome 80+)
    if encrypted_value[:3] == b'v10' or encrypted_value[:3] == b'v20':
        try:
            import win32crypt
            import win32api
        except ImportError:
            print("  [!] pywin32 not installed, trying dpapi fallback...")

        try:
            # Get Chrome encryption key
            local_state_path = os.path.expandvars(
                r"%LOCALAPPDATA%\Google\Chrome\User Data\Local State"
            )
            with open(local_state_path, "r", encoding="utf-8") as f:
                local_state = json.load(f)

            import base64
            encrypted_key = base64.b64decode(
                local_state["os_crypt"]["encrypted_key"]
            )[5:]  # remove DPAPI prefix

            import win32crypt
            key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]

            from Crypto.Cipher import AES
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:-16]
            tag = encrypted_value[-16:]
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
        except Exception as e:
            return f"[decrypt_error:{e}]"

    # Old DPAPI format
    try:
        import win32crypt
        return win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1].decode("utf-8")
    except Exception:
        return ""


def export_cookies():
    chrome_db = os.path.expandvars(
        r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Network\Cookies"
    )
    if not os.path.exists(chrome_db):
        chrome_db = os.path.expandvars(
            r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cookies"
        )

    if not os.path.exists(chrome_db):
        print("ERROR: Chrome cookies database not found.")
        print("Make sure Chrome is installed.")
        sys.exit(1)

    print(f"Reading Chrome cookies from: {chrome_db}")
    print("NOTE: Chrome must be closed for this to work!\n")

    # Copy DB to temp (Chrome may lock it)
    tmp = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy2(chrome_db, tmp)
    except PermissionError:
        print("ERROR: Cannot read cookies file. CLOSE CHROME COMPLETELY and try again.")
        sys.exit(1)

    conn = sqlite3.connect(tmp)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly
            FROM cookies
            WHERE host_key LIKE '%youtube.com%' OR host_key LIKE '%google.com%'
        """)
        rows = cursor.fetchall()
    finally:
        conn.close()
        os.unlink(tmp)

    print(f"Found {len(rows)} YouTube/Google cookies")

    # Write Netscape cookies.txt format
    lines = ["# Netscape HTTP Cookie File", "# Exported by export_youtube_cookies.py", ""]

    count = 0
    for host, name, encrypted_value, path, expires_utc, is_secure, is_httponly in rows:
        value = _decrypt_value(encrypted_value)
        if not value or value.startswith("[decrypt_error"):
            continue

        # Convert Chrome timestamp (microseconds since 1601-01-01) to Unix timestamp
        if expires_utc:
            unix_ts = (expires_utc - 11644473600000000) // 1000000
        else:
            unix_ts = 0

        include_subdomains = "TRUE" if host.startswith(".") else "FALSE"
        secure = "TRUE" if is_secure else "FALSE"

        lines.append(f"{host}\t{include_subdomains}\t{path}\t{secure}\t{unix_ts}\t{name}\t{value}")
        count += 1

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Exported {count} cookies to: {OUTPUT_FILE}")
    print("\nDone! The app will now use these cookies automatically.")
    print("Add to your .env file:")
    print(f"  YOUTUBE_COOKIES_FILE={OUTPUT_FILE}")


if __name__ == "__main__":
    export_cookies()
