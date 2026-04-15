"""
Pollinations.ai image generation service.

Models:
  - flux: Free model used for all image generation. Supports optional reference
    image input via the `image` query parameter for style consistency.
    Reference images are uploaded to catbox.moe to obtain a public URL.

API key: POLLINATIONS_API_KEY in .env or Settings.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

import requests


# Cache of (file_hash → public_url) to avoid re-uploading the same image
_upload_cache: dict[str, str] = {}


def generate_image(
    prompt: str,
    output_path: str | Path,
    api_key: str = "",
    reference_character_path: str | Path | None = None,
    reference_style_path: str | Path | None = None,
    width: int = 1920,
    height: int = 1080,
) -> Path:
    """Generate an image with Pollinations and save to disk.

    When reference images are provided, uploads them to get public URLs
    and uses the klein-large model. Otherwise uses flux.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    char_ref = _resolve_ref(reference_character_path)
    style_ref = _resolve_ref(reference_style_path)
    has_refs = char_ref is not None or style_ref is not None

    if has_refs:
        _generate_with_reference(prompt, output_path, api_key, char_ref, style_ref, width, height)
    else:
        _generate_with_flux(prompt, output_path, api_key, width, height)

    return output_path


def _resolve_ref(path: str | Path | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    return p if p.exists() else None


def _upload_to_catbox(image_path: Path) -> str:
    """Upload a local image to catbox.moe and return the public URL.

    Results are cached by file content hash to avoid re-uploads.
    """
    file_hash = hashlib.md5(image_path.read_bytes()).hexdigest()
    if file_hash in _upload_cache:
        return _upload_cache[file_hash]

    with open(str(image_path), "rb") as f:
        resp = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (image_path.name, f, "image/jpeg")},
            timeout=60,
        )
    resp.raise_for_status()
    url = resp.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"catbox.moe upload failed: {url[:200]}")

    _upload_cache[file_hash] = url
    print(f"[Pollinations] Reference uploaded: {url}")
    return url


def _generate_with_reference(
    prompt: str,
    output_path: Path,
    api_key: str,
    char_ref: Path | None,
    style_ref: Path | None,
    width: int,
    height: int,
) -> None:
    """Generate using flux model with reference image(s)."""
    # Upload reference image(s) to get public URLs
    primary_ref = char_ref or style_ref
    ref_url = _upload_to_catbox(primary_ref)

    # Build prompt with reference instructions
    parts = []
    if char_ref and style_ref:
        parts.append(
            "Maintain the exact same character appearance from the reference image "
            "and use a consistent visual style and color palette."
        )
    elif char_ref:
        parts.append("Maintain the exact same character appearance as the reference image.")
    elif style_ref:
        parts.append("Maintain the exact same visual style and color palette as the reference image.")
    parts.append(prompt)
    full_prompt = " ".join(parts)

    print(f"[Pollinations] zimage with ref — prompt: {full_prompt[:120]}...")

    encoded_prompt = quote(full_prompt)
    url = f"https://gen.pollinations.ai/image/{encoded_prompt}"
    params = {
        "model": "zimage",
        "image": ref_url,
        "width": width,
        "height": height,
        "nologo": "true",
    }
    if api_key:
        params["key"] = api_key

    resp = requests.get(url, params=params, timeout=300)
    resp.raise_for_status()

    if len(resp.content) < 1000:
        raise RuntimeError(
            f"Pollinations zimage: respuesta muy pequena ({len(resp.content)} bytes). "
            f"Prompt: {prompt[:120]}"
        )

    output_path.write_bytes(resp.content)
    print(
        f"[Pollinations] zimage+ref Guardada: {output_path.name} "
        f"({len(resp.content):,} bytes)"
    )


def _generate_with_flux(
    prompt: str,
    output_path: Path,
    api_key: str,
    width: int,
    height: int,
) -> None:
    """Generate using flux model via simple GET (no reference image)."""
    encoded_prompt = quote(prompt)
    url = f"https://gen.pollinations.ai/image/{encoded_prompt}"

    params = {
        "width": width,
        "height": height,
        "model": "zimage",
        "nologo": "true",
        "enhance": "false",
    }
    if api_key:
        params["key"] = api_key

    resp = requests.get(url, params=params, timeout=180)
    resp.raise_for_status()

    if len(resp.content) < 1000:
        raise RuntimeError(
            f"Pollinations zimage: respuesta muy pequena ({len(resp.content)} bytes). "
            f"Prompt: {prompt[:120]}"
        )

    output_path.write_bytes(resp.content)
    print(
        f"[Pollinations] zimage Guardada: {output_path.name} "
        f"({len(resp.content):,} bytes)"
    )
