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
import subprocess
import shutil
import tempfile
from pathlib import Path

from cache_common import (
    atomic_write_bytes,
    atomic_write_text,
    build_png_itxt_chunk,
    build_xmp_packet,
    detect_format,
    image_hash,
    strip_ai_xmp,
)


# ---------------------------------------------------------------------------
# Writers: exiftool -> format-specific -> sidecar
# ---------------------------------------------------------------------------

def write_with_exiftool(filepath: str, description: str) -> bool:
    """Write XMP via exiftool (handles all formats)."""
    if not shutil.which("exiftool"):
        return False

    xmp_packet = build_xmp_packet(description)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".tmp-ai-cache-",
            suffix=".xmp",
            dir=str(Path(filepath).resolve().parent),
            delete=False,
        ) as handle:
            handle.write(xmp_packet)
            handle.flush()
            temp_path = handle.name
        subprocess.run(
            ["exiftool", "-overwrite_original", f"-XMP<={temp_path}", "--", filepath],
            capture_output=True, check=True, timeout=30,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    finally:
        if temp_path is not None:
            Path(temp_path).unlink(missing_ok=True)


def write_xmp_jpeg(filepath: str, data: bytes, description: str) -> bool:
    """Inject XMP APP1 segment into JPEG."""
    xmp_payload = build_xmp_packet(description).encode("utf-8")
    XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"

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

    atomic_write_bytes(filepath, bytes(output))
    return True


def write_xmp_png(filepath: str, data: bytes, description: str) -> bool:
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
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False

    # Strip existing AI cache iTXt chunk
    data = strip_ai_xmp(data, "png")
    itxt_chunk = build_png_itxt_chunk(description)

    # Insert before IEND chunk (last 12 bytes of a valid PNG)
    # Find IEND
    iend_pos = data.rfind(b"IEND")
    if iend_pos == -1:
        return False
    iend_start = iend_pos - 4  # 4 bytes length before "IEND"

    new_data = data[:iend_start] + itxt_chunk + data[iend_start:]

    atomic_write_bytes(filepath, new_data)
    return True


def write_xmp_webp(filepath: str, data: bytes, description: str) -> bool:
    """Inject XMP as a RIFF chunk into WebP.

    WebP RIFF structure:
      "RIFF" + 4-byte LE size + "WEBP" + chunks...
    Each chunk:
      4-byte FourCC + 4-byte LE size + data (padded to even)
    """
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

    atomic_write_bytes(filepath, new_data)
    return True


def write_sidecar(filepath: str, description: str) -> str:
    """Last resort for GIF/BMP: write a .ai-cache sidecar file."""
    sidecar_path = filepath + ".ai-cache"
    atomic_write_text(sidecar_path, description)
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

    try:
        data = Path(filepath).read_bytes()
    except FileNotFoundError:
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Error: failed to read {filepath}: {exc}", file=sys.stderr)
        sys.exit(1)

    fmt = detect_format(data, filepath)

    # Hash BEFORE writing (strips any existing AI cache XMP)
    current_hash = image_hash(data, fmt)
    full_content = f"HASH:{current_hash}|{content}"

    # Try exiftool first (handles everything)
    if write_with_exiftool(filepath, full_content):
        print("OK: exiftool")
        return

    # Format-specific direct injection
    if fmt == "jpeg" and write_xmp_jpeg(filepath, data, full_content):
        print("OK: jpeg-inject")
    elif fmt == "png" and write_xmp_png(filepath, data, full_content):
        print("OK: png-inject")
    elif fmt == "webp" and write_xmp_webp(filepath, data, full_content):
        print("OK: webp-inject")
    else:
        # GIF, BMP, or injection failed — sidecar
        sidecar = write_sidecar(filepath, full_content)
        print(f"SIDECAR: {sidecar}")


if __name__ == "__main__":
    main()
