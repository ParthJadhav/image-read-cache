#!/usr/bin/env python3
"""Shared utilities for the image-read-cache skill."""
from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Optional

# Unique marker to distinguish our XMP from other XMP data
AI_CACHE_MARKER = "x-ai-cache-v1"
AI_CACHE_MARKER_BYTES = AI_CACHE_MARKER.encode("utf-8")


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_format(data: bytes, filepath: str | None = None) -> str:
    """Detect image format from magic bytes."""
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if filepath:
        ext = Path(filepath).suffix.lower()
        return {
            ".jpg": "jpeg", ".jpeg": "jpeg",
            ".png": "png", ".webp": "webp",
            ".gif": "gif", ".bmp": "bmp",
        }.get(ext, "unknown")
    return "unknown"


# ---------------------------------------------------------------------------
# Strip AI cache XMP (for stable hashing)
# ---------------------------------------------------------------------------

def strip_ai_xmp(data: bytes, fmt: str) -> bytes:
    """Remove our AI cache XMP from file bytes for hashing."""
    if AI_CACHE_MARKER_BYTES not in data:
        return data

    if fmt == "jpeg":
        return _strip_jpeg_xmp(data)
    elif fmt == "png":
        return _strip_png_xmp(data)
    elif fmt == "webp":
        return _strip_webp_xmp(data)
    else:
        return _strip_generic_xmp(data)


def _strip_jpeg_xmp(data: bytes) -> bytes:
    XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"
    pos = 2
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
        if m == 0xE1:
            seg_body = data[pos + 4 : seg_end]
            if seg_body.startswith(XMP_NS) and AI_CACHE_MARKER_BYTES in seg_body:
                return data[:pos] + data[seg_end:]
        pos = seg_end
    return data


def _strip_png_xmp(data: bytes) -> bytes:
    pos = 8
    while pos + 8 <= len(data):
        chunk_len = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk_end = pos + 12 + chunk_len
        if chunk_type == b"iTXt":
            chunk_data = data[pos + 8 : pos + 8 + chunk_len]
            if AI_CACHE_MARKER_BYTES in chunk_data:
                return data[:pos] + data[chunk_end:]
        pos = chunk_end
    return data


def _strip_webp_xmp(data: bytes) -> bytes:
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos : pos + 4]
        chunk_size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        chunk_end = pos + 8 + chunk_size
        if chunk_size % 2 != 0:
            chunk_end += 1
        if fourcc in (b"XMP ", b"XMP\x00"):
            chunk_data = data[pos + 8 : pos + 8 + chunk_size]
            if AI_CACHE_MARKER_BYTES in chunk_data:
                new_data = data[:pos] + data[chunk_end:]
                new_riff_size = len(new_data) - 8
                new_data = new_data[:4] + struct.pack("<I", new_riff_size) + new_data[8:]
                return new_data
        pos = chunk_end
    return data


def _strip_generic_xmp(data: bytes) -> bytes:
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
        if AI_CACHE_MARKER_BYTES in data[pkt_start:pkt_end]:
            return data[:pkt_start] + data[pkt_end:]
        search_from = pkt_end
    return data


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def image_hash(data: bytes, fmt: str) -> str:
    """Hash of file bytes with AI cache XMP stripped."""
    clean = strip_ai_xmp(data, fmt)
    return hashlib.sha256(clean).hexdigest()[:16]


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def escape_xml(text: str) -> str:
    """Escape text for safe embedding in XML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def unescape_xml(text: str) -> str:
    """Reverse XML escaping."""
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )


# ---------------------------------------------------------------------------
# XMP packet builder
# ---------------------------------------------------------------------------

def build_xmp_packet(description: str) -> str:
    """Build a complete XMP packet with our AI cache marker."""
    return (
        '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        f'xmlns:ai="{AI_CACHE_MARKER}">'
        "<dc:description><rdf:Alt>"
        f'<rdf:li xml:lang="x-default">{escape_xml(description)}</rdf:li>'
        "</rdf:Alt></dc:description>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    )


# ---------------------------------------------------------------------------
# XMP extraction / validation
# ---------------------------------------------------------------------------

def extract_xmp_description(data: bytes) -> Optional[str]:
    """Extract dc:description from our AI cache XMP packet."""
    if AI_CACHE_MARKER_BYTES not in data:
        return None

    marker_pos = data.find(AI_CACHE_MARKER_BYTES)
    xmp_start = data.rfind(b"<x:xmpmeta", 0, marker_pos)
    if xmp_start == -1:
        return None

    xmp_end = data.find(b"</x:xmpmeta>", marker_pos)
    if xmp_end == -1:
        return None

    xmp = data[xmp_start : xmp_end + len(b"</x:xmpmeta>")].decode(
        "utf-8", errors="ignore"
    )
    start = '<rdf:li xml:lang="x-default">'
    end = "</rdf:li>"
    start_idx = xmp.find(start)
    if start_idx == -1:
        return None
    start_idx += len(start)
    end_idx = xmp.find(end, start_idx)
    if end_idx == -1:
        return None

    content = xmp[start_idx:end_idx].strip()
    return content if content else None


def validate_and_extract(cached: str, data: bytes, fmt: str) -> Optional[str]:
    """Validate hash and extract content from cached string."""
    if not cached.startswith("HASH:") or "|" not in cached:
        return None

    stored_hash, content = cached.split("|", 1)
    if stored_hash[5:] != image_hash(data, fmt):
        return None
    return content


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

def atomic_write_bytes(path: str, data: bytes) -> None:
    """Atomically replace a file with bytes written in the same directory."""
    directory = str(Path(path).resolve().parent)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp-ai-cache-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: str, text: str) -> None:
    """Atomically replace a file with UTF-8 text."""
    atomic_write_bytes(path, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Chunk builders
# ---------------------------------------------------------------------------

def build_png_itxt_chunk(description: str) -> bytes:
    """Build a PNG iTXt chunk containing our XMP packet."""
    keyword = b"XML:com.adobe.xmp\x00"
    compression_flag = b"\x00"
    compression_method = b"\x00"
    language_tag = b"\x00"
    translated_keyword = b"\x00"
    xmp_text = build_xmp_packet(description).encode("utf-8")
    chunk_data = (
        keyword
        + compression_flag
        + compression_method
        + language_tag
        + translated_keyword
        + xmp_text
    )
    chunk_type = b"iTXt"
    chunk_crc = struct.pack(">I", zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF)
    return struct.pack(">I", len(chunk_data)) + chunk_type + chunk_data + chunk_crc
