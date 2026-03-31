#!/usr/bin/env python3
"""
Check for cached LLM content in image XMP metadata or sidecar file.

Usage: check_cache.py <image-path>

Output:
  CACHED: <content>   — cached content found and still valid
  NO_CACHE             — no cache, or image has changed since caching

Format support:
  JPEG (.jpg/.jpeg) — reads XMP from APP1 segment
  PNG  (.png)       — reads XMP from iTXt chunk
  WebP (.webp)      — reads XMP from RIFF XMP chunk
  GIF  (.gif)       — reads from .ai-cache sidecar file
  BMP  (.bmp)       — reads from .ai-cache sidecar file

Invalidation: The cache stores an image hash computed over the file
bytes with the AI cache XMP stripped out. If the image is modified
(new pixels, re-export, new screenshot), the hash won't match and
the cache is invalidated automatically.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from cache_common import (
    detect_format,
    extract_xmp_description,
    unescape_xml,
    validate_and_extract,
)


# ---------------------------------------------------------------------------
# Sidecar fallback (GIF, BMP)
# ---------------------------------------------------------------------------

def check_sidecar(filepath: str) -> Optional[str]:
    """Check for a .ai-cache sidecar file."""
    sidecar = Path(filepath + ".ai-cache")
    try:
        content = sidecar.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return content or None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: check_cache.py <image-path>", file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]
    try:
        data = Path(filepath).read_bytes()
    except FileNotFoundError:
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Error: failed to read {filepath}: {exc}", file=sys.stderr)
        sys.exit(1)

    fmt = detect_format(data, filepath)

    # GIF/BMP never store embedded XMP in this implementation.
    if fmt not in {"gif", "bmp"}:
        cached = extract_xmp_description(data)
        if cached:
            content = validate_and_extract(cached, data, fmt)
            if content:
                print(f"CACHED: {unescape_xml(content)}")
                return

    cached = check_sidecar(filepath)
    if cached:
        content = validate_and_extract(cached, data, fmt)
        if content:
            print(f"CACHED: {content}")
            return

    print("NO_CACHE")


if __name__ == "__main__":
    main()
