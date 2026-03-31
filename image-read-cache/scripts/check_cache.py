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

import struct
import sys
import hashlib
import re
import zlib
from pathlib import Path
from typing import Optional

# Marker that distinguishes our XMP from other XMP data
AI_CACHE_MARKER = "x-ai-cache-v1"


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_format(filepath: str) -> str:
    """Detect image format from magic bytes."""
    with open(filepath, "rb") as f:
        header = f.read(12)

    if header[:2] == b"\xff\xd8":
        return "jpeg"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if header[:2] == b"BM":
        return "bmp"

    ext = Path(filepath).suffix.lower()
    return {
        ".jpg": "jpeg", ".jpeg": "jpeg",
        ".png": "png", ".webp": "webp",
        ".gif": "gif", ".bmp": "bmp",
    }.get(ext, "unknown")


# ---------------------------------------------------------------------------
# Strip AI cache XMP (for stable hashing)
# ---------------------------------------------------------------------------

def strip_ai_xmp(data: bytes, fmt: str) -> bytes:
    """Remove our AI cache XMP from file bytes for hashing."""
    marker = AI_CACHE_MARKER.encode("utf-8")
    if marker not in data:
        return data

    if fmt == "jpeg":
        return _strip_jpeg_xmp(data, marker)
    elif fmt == "png":
        return _strip_png_xmp(data, marker)
    elif fmt == "webp":
        return _strip_webp_xmp(data, marker)
    else:
        return _strip_generic_xmp(data, marker)


def _strip_jpeg_xmp(data: bytes, marker: bytes) -> bytes:
    """Strip our XMP APP1 segment from JPEG."""
    XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"
    pos = 2  # Skip SOI
    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            break
        m = data[pos + 1]
        if m in (0xDA, 0xD9):
            break
        if 0xD0 <= m <= 0xD7:
            pos += 2
            continue
        if pos + 4 > len(data):
            break
        seg_len = int.from_bytes(data[pos + 2 : pos + 4], "big")
        seg_end = pos + 2 + seg_len
        if m == 0xE1:  # APP1
            seg_body = data[pos + 4 : seg_end]
            if seg_body.startswith(XMP_NS) and marker in seg_body:
                return data[:pos] + data[seg_end:]
        pos = seg_end
    return data


def _strip_png_xmp(data: bytes, marker: bytes) -> bytes:
    """Strip our XMP iTXt chunk from PNG."""
    pos = 8  # Skip PNG signature
    while pos + 8 <= len(data):
        chunk_len = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk_end = pos + 12 + chunk_len
        if chunk_type == b"iTXt":
            chunk_data = data[pos + 8 : pos + 8 + chunk_len]
            if marker in chunk_data:
                return data[:pos] + data[chunk_end:]
        pos = chunk_end
    return data


def _strip_webp_xmp(data: bytes, marker: bytes) -> bytes:
    """Strip our XMP chunk from WebP RIFF container."""
    pos = 12  # Skip RIFF + WEBP
    while pos + 8 <= len(data):
        fourcc = data[pos : pos + 4]
        chunk_size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        chunk_end = pos + 8 + chunk_size
        if chunk_size % 2 != 0:
            chunk_end += 1
        if fourcc in (b"XMP ", b"XMP\x00"):
            chunk_data = data[pos + 8 : pos + 8 + chunk_size]
            if marker in chunk_data:
                new_data = data[:pos] + data[chunk_end:]
                new_riff_size = len(new_data) - 8
                new_data = new_data[:4] + struct.pack("<I", new_riff_size) + new_data[8:]
                return new_data
        pos = chunk_end
    return data


def _strip_generic_xmp(data: bytes, marker: bytes) -> bytes:
    """Fallback: strip xmpmeta block by packet boundaries."""
    start_tag = b'<?xpacket begin="\xef\xbb\xbf"'
    end_tag = b'<?xpacket end="w"?>'
    search_from = 0
    while True:
        pkt_start = data.find(start_tag, search_from)
        if pkt_start == -1:
            break
        pkt_end = data.find(end_tag, pkt_start)
        if pkt_end == -1:
            break
        pkt_end += len(end_tag)
        if marker in data[pkt_start:pkt_end]:
            return data[:pkt_start] + data[pkt_end:]
        search_from = pkt_end
    return data


def image_hash(filepath: str, fmt: str) -> str:
    """Hash of file bytes with AI cache XMP stripped."""
    with open(filepath, "rb") as f:
        data = f.read()
    clean = strip_ai_xmp(data, fmt)
    return hashlib.sha256(clean).hexdigest()[:16]


# ---------------------------------------------------------------------------
# XMP extraction (format-aware)
# ---------------------------------------------------------------------------

def extract_xmp_description(filepath: str, fmt: str) -> Optional[str]:
    """Extract dc:description from our AI cache XMP packet."""
    with open(filepath, "rb") as f:
        data = f.read()

    marker = AI_CACHE_MARKER.encode("utf-8")
    if marker not in data:
        return None

    # Find our xmpmeta block (works regardless of container format,
    # since the XMP XML is the same once extracted)
    marker_pos = data.find(marker)
    xmp_start = data.rfind(b"<x:xmpmeta", 0, marker_pos)
    if xmp_start == -1:
        return None

    xmp_end = data.find(b"</x:xmpmeta>", marker_pos)
    if xmp_end == -1:
        return None

    xmp = data[xmp_start : xmp_end + len(b"</x:xmpmeta>")].decode(
        "utf-8", errors="ignore"
    )

    match = re.search(
        r'<rdf:li xml:lang="x-default">(.*?)</rdf:li>', xmp, re.DOTALL
    )
    if not match:
        return None

    content = match.group(1).strip()
    return content if content else None


# ---------------------------------------------------------------------------
# Sidecar fallback (GIF, BMP)
# ---------------------------------------------------------------------------

def check_sidecar(filepath: str) -> Optional[str]:
    """Check for a .ai-cache sidecar file."""
    sidecar = Path(filepath + ".ai-cache")
    if not sidecar.exists():
        return None
    return sidecar.read_text(encoding="utf-8").strip() or None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def unescape_xml(text: str) -> str:
    """Reverse XML escaping."""
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )


def validate_and_extract(cached: str, filepath: str, fmt: str) -> Optional[str]:
    """Validate hash and extract content from cached string."""
    if not cached.startswith("HASH:") or "|" not in cached:
        return None

    stored_hash, content = cached.split("|", 1)
    stored_hash = stored_hash[5:]

    current_hash = image_hash(filepath, fmt)
    if stored_hash != current_hash:
        return None  # Image changed since caching

    return content


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: check_cache.py <image-path>", file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]
    if not Path(filepath).exists():
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    fmt = detect_format(filepath)

    # Try embedded XMP first (JPEG, PNG, WebP)
    cached = extract_xmp_description(filepath, fmt)
    if cached:
        content = validate_and_extract(cached, filepath, fmt)
        if content:
            print(f"CACHED: {unescape_xml(content)}")
            return

    # Try sidecar file (GIF, BMP, or if injection failed previously)
    cached = check_sidecar(filepath)
    if cached:
        content = validate_and_extract(cached, filepath, fmt)
        if content:
            print(f"CACHED: {content}")
            return

    print("NO_CACHE")


if __name__ == "__main__":
    main()
