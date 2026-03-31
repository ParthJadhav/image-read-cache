#!/usr/bin/env python3
"""
Write LLM content as XMP metadata into an image file.

Usage: write_cache.py <image-path> <content>

Stores content in XMP dc:description with a file hash prefix for
cache invalidation. The hash is computed over the file bytes with
any existing AI cache XMP stripped, so the hash stays stable across
cache rewrites.

Format support:
  JPEG (.jpg/.jpeg) — XMP injected as APP1 segment
  PNG  (.png)       — XMP injected as iTXt chunk
  WebP (.webp)      — XMP injected as XMP RIFF chunk
  GIF  (.gif)       — sidecar file (no XMP support)
  BMP  (.bmp)       — sidecar file (no XMP support)

Tries exiftool first (all formats), then direct byte injection,
then sidecar file as last resort.

Output:
  OK: <method>
  SIDECAR: <path>
"""
from __future__ import annotations

import struct
import sys
import hashlib
import subprocess
import shutil
import zlib
from pathlib import Path

# Unique marker so we can identify our XMP vs other XMP data
AI_CACHE_MARKER = "x-ai-cache-v1"


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def escape_xml(text: str) -> str:
    """Escape text for safe embedding in XML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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


def detect_format(filepath: str) -> str:
    """Detect image format from magic bytes, not extension."""
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

    # Fall back to extension
    ext = Path(filepath).suffix.lower()
    return {
        ".jpg": "jpeg", ".jpeg": "jpeg",
        ".png": "png", ".webp": "webp",
        ".gif": "gif", ".bmp": "bmp",
    }.get(ext, "unknown")


# ---------------------------------------------------------------------------
# Strip existing AI cache XMP (needed for stable hashing)
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
        chunk_end = pos + 12 + chunk_len  # 4 len + 4 type + data + 4 crc
        if chunk_type == b"iTXt":
            chunk_data = data[pos + 8 : pos + 8 + chunk_len]
            if marker in chunk_data:
                return data[:pos] + data[chunk_end:]
        pos = chunk_end
    return data


def _strip_webp_xmp(data: bytes, marker: bytes) -> bytes:
    """Strip our XMP chunk from WebP RIFF container."""
    pos = 12  # Skip RIFF header + WEBP
    while pos + 8 <= len(data):
        fourcc = data[pos : pos + 4]
        chunk_size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        chunk_end = pos + 8 + chunk_size
        # RIFF chunks are padded to even size
        if chunk_size % 2 != 0:
            chunk_end += 1
        if fourcc in (b"XMP ", b"XMP\x00"):
            chunk_data = data[pos + 8 : pos + 8 + chunk_size]
            if marker in chunk_data:
                # Remove chunk and update RIFF container size
                new_data = data[:pos] + data[chunk_end:]
                # Update RIFF size at offset 4
                new_riff_size = len(new_data) - 8
                new_data = new_data[:4] + struct.pack("<I", new_riff_size) + new_data[8:]
                return new_data
        pos = chunk_end
    return data


def _strip_generic_xmp(data: bytes, marker: bytes) -> bytes:
    """Fallback: strip xmpmeta block by finding packet boundaries."""
    start_tag = b'<?xpacket begin="\xef\xbb\xbf"'
    end_tag = b'<?xpacket end="w"?>'
    # Find the packet that contains our marker
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
# Writers: exiftool -> format-specific -> sidecar
# ---------------------------------------------------------------------------

def write_with_exiftool(filepath: str, description: str) -> bool:
    """Write XMP via exiftool (handles all formats)."""
    if not shutil.which("exiftool"):
        return False

    xmp_packet = build_xmp_packet(description)
    xmp_path = filepath + ".tmp.xmp"

    try:
        Path(xmp_path).write_text(xmp_packet, encoding="utf-8")
        subprocess.run(
            ["exiftool", "-overwrite_original", f"-XMP<={xmp_path}", filepath],
            capture_output=True, check=True, timeout=30,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    finally:
        Path(xmp_path).unlink(missing_ok=True)


def write_xmp_jpeg(filepath: str, description: str) -> bool:
    """Inject XMP APP1 segment into JPEG."""
    xmp_payload = build_xmp_packet(description).encode("utf-8")
    XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"

    with open(filepath, "rb") as f:
        data = f.read()

    if len(data) < 2 or data[:2] != b"\xff\xd8":
        return False

    # Strip any existing AI cache XMP
    data = strip_ai_xmp(data, "jpeg")

    # Build new JPEG with XMP APP1 inserted after existing APP0/APP1 segments
    output = bytearray(data[:2])  # SOI
    pos = 2
    xmp_inserted = False

    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            if not xmp_inserted:
                seg = XMP_NS + xmp_payload
                output += b"\xff\xe1" + (len(seg) + 2).to_bytes(2, "big") + seg
                xmp_inserted = True
            output += data[pos:]
            break

        m = data[pos + 1]

        if m in (0xDA, 0xD9):  # SOS or EOI
            if not xmp_inserted:
                seg = XMP_NS + xmp_payload
                output += b"\xff\xe1" + (len(seg) + 2).to_bytes(2, "big") + seg
                xmp_inserted = True
            output += data[pos:]
            break

        if 0xD0 <= m <= 0xD7:  # RST markers
            output += data[pos : pos + 2]
            pos += 2
            continue

        if pos + 4 > len(data):
            output += data[pos:]
            break

        seg_len = int.from_bytes(data[pos + 2 : pos + 4], "big")
        seg_end = pos + 2 + seg_len
        output += data[pos:seg_end]
        pos = seg_end

    if not xmp_inserted:
        seg = XMP_NS + xmp_payload
        rest = bytes(output[2:])
        output = bytearray(data[:2])
        output += b"\xff\xe1" + (len(seg) + 2).to_bytes(2, "big") + seg
        output += rest

    with open(filepath, "wb") as f:
        f.write(bytes(output))
    return True


def write_xmp_png(filepath: str, description: str) -> bool:
    """Inject XMP as an iTXt chunk into PNG.

    PNG iTXt chunk structure:
      4 bytes: data length (big-endian)
      4 bytes: chunk type ("iTXt")
      N bytes: chunk data
        - keyword (null-terminated): "XML:com.adobe.xmp"
        - compression flag: 0 (no compression)
        - compression method: 0
        - language tag (null-terminated): ""
        - translated keyword (null-terminated): ""
        - text: the XMP packet
      4 bytes: CRC32 of (chunk type + chunk data)
    """
    with open(filepath, "rb") as f:
        data = f.read()

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False

    # Strip existing AI cache iTXt chunk
    data = strip_ai_xmp(data, "png")

    # Build iTXt chunk
    keyword = b"XML:com.adobe.xmp\x00"
    compression_flag = b"\x00"  # not compressed
    compression_method = b"\x00"
    language_tag = b"\x00"  # empty
    translated_keyword = b"\x00"  # empty
    xmp_text = build_xmp_packet(description).encode("utf-8")

    chunk_data = keyword + compression_flag + compression_method + language_tag + translated_keyword + xmp_text
    chunk_len = struct.pack(">I", len(chunk_data))
    chunk_type = b"iTXt"
    chunk_crc = struct.pack(">I", zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF)

    itxt_chunk = chunk_len + chunk_type + chunk_data + chunk_crc

    # Insert before IEND chunk (last 12 bytes of a valid PNG)
    # Find IEND
    iend_pos = data.rfind(b"IEND")
    if iend_pos == -1:
        return False
    iend_start = iend_pos - 4  # 4 bytes length before "IEND"

    new_data = data[:iend_start] + itxt_chunk + data[iend_start:]

    with open(filepath, "wb") as f:
        f.write(new_data)
    return True


def write_xmp_webp(filepath: str, description: str) -> bool:
    """Inject XMP as a RIFF chunk into WebP.

    WebP RIFF structure:
      "RIFF" + 4-byte LE size + "WEBP" + chunks...
    Each chunk:
      4-byte FourCC + 4-byte LE size + data (padded to even)
    """
    with open(filepath, "rb") as f:
        data = f.read()

    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False

    # Strip existing AI cache XMP chunk
    data = strip_ai_xmp(data, "webp")

    # Build XMP chunk
    xmp_data = build_xmp_packet(description).encode("utf-8")
    xmp_chunk = b"XMP " + struct.pack("<I", len(xmp_data)) + xmp_data
    # Pad to even length
    if len(xmp_data) % 2 != 0:
        xmp_chunk += b"\x00"

    # Append chunk to RIFF container
    new_data = data + xmp_chunk

    # Update RIFF container size (bytes 4-8, little-endian)
    new_riff_size = len(new_data) - 8
    new_data = new_data[:4] + struct.pack("<I", new_riff_size) + new_data[8:]

    with open(filepath, "wb") as f:
        f.write(new_data)
    return True


def write_sidecar(filepath: str, description: str) -> str:
    """Last resort for GIF/BMP: write a .ai-cache sidecar file."""
    sidecar_path = filepath + ".ai-cache"
    Path(sidecar_path).write_text(description, encoding="utf-8")
    return sidecar_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print("Usage: write_cache.py <image-path> <content>", file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]
    content = sys.argv[2]

    if not Path(filepath).exists():
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    fmt = detect_format(filepath)

    # Hash BEFORE writing (strips any existing AI cache XMP)
    current_hash = image_hash(filepath, fmt)
    full_content = f"HASH:{current_hash}|{content}"

    # Try exiftool first (handles everything)
    if write_with_exiftool(filepath, full_content):
        print("OK: exiftool")
        return

    # Format-specific direct injection
    if fmt == "jpeg" and write_xmp_jpeg(filepath, full_content):
        print("OK: jpeg-inject")
    elif fmt == "png" and write_xmp_png(filepath, full_content):
        print("OK: png-inject")
    elif fmt == "webp" and write_xmp_webp(filepath, full_content):
        print("OK: webp-inject")
    else:
        # GIF, BMP, or injection failed — sidecar
        sidecar = write_sidecar(filepath, full_content)
        print(f"SIDECAR: {sidecar}")


if __name__ == "__main__":
    main()
