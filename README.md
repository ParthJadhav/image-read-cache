# image-read-cache

An [Agent Skill](https://agentskills.io) that caches LLM image descriptions as XMP metadata inside image files, reducing token usage by ~92% on repeated reads.

## The Problem

Every time an AI agent reads an image, it sends the full base64-encoded image to the LLM. In a multi-turn conversation where the same image is referenced repeatedly, this compounds fast:

- A single image costs **1,000-6,000+ tokens** per read
- Over 20 turns referencing the same image, that's **20,000-120,000 tokens** wasted on identical content

## The Solution

`image-read-cache` intercepts image reads and stores the LLM's description directly inside the image file as XMP metadata. On subsequent reads, the cached text description is returned instead of the full image.

```
Agent reads image.png
  -> Check XMP metadata for cached description
  -> If found: return text (~200 tokens) instead of image (~4,000 tokens)
  -> If not found: read image normally, cache the result in metadata
```

The cache is embedded in the image file itself -- no external database, no sidecar files (for JPEG/PNG/WebP), and it follows the image wherever it goes.

## How It Works

```
image-read-cache/
  SKILL.md              # Agent instructions (AgentSkills.io format)
  scripts/
    check_cache.py      # Read cached description from XMP metadata
    write_cache.py      # Write description into image XMP metadata
```

### Cache Flow

1. **Before reading an image**, the agent runs `check_cache.py <image-path>`
2. If `CACHED:` -- use the text description, skip the image entirely
3. If `NO_CACHE` -- read the image normally, then run `write_cache.py` to store the result
4. Future reads of the same image return the cached text

### Cache Invalidation

A SHA-256 hash of the image content (excluding the cache metadata) is stored alongside the description. If the image file changes (re-export, new screenshot, pixel edits), the hash won't match and the cache auto-invalidates. No stale descriptions, ever.

## Format Support

| Format | Method | Embedded |
|--------|--------|----------|
| JPEG (.jpg/.jpeg) | XMP in APP1 segment | Yes |
| PNG (.png) | XMP in iTXt chunk | Yes |
| WebP (.webp) | XMP in RIFF chunk | Yes |
| GIF (.gif) | `.ai-cache` sidecar file | No |
| BMP (.bmp) | `.ai-cache` sidecar file | No |

When `exiftool` is available, it's used for all formats. Otherwise, direct byte injection handles JPEG/PNG/WebP natively with zero external dependencies beyond Python 3.9+.

## Installation

```bash
npx skills add ParthJadhav/image-read-cache
```

Works with [30+ compatible agents](https://agentskills.io) including Claude Code, Cursor, OpenCode, Gemini CLI, Goose, Roo Code, GitHub Copilot, and more.

## Benchmark Results

Full benchmark harness available at [image-read-cache-benchmark](https://github.com/ParthJadhav/image-read-cache-benchmark).

### Token Savings (92.1% reduction)

Cumulative token cost when the same image is referenced across multiple LLM turns:

| Image Type | Image Tokens | Cached Tokens | 20-Turn Savings |
|------------|-------------|---------------|-----------------|
| UI Dashboard (1200x800) | 1,220 | 81 | 93.4% |
| Landscape Photo (1600x900) | 1,560 | 57 | 96.3% |
| Code Editor (900x600) | 880 | 65 | 92.6% |
| Data Table (1000x600) | 880 | 98 | 88.9% |
| Mobile App UI (390x844) | 540 | 68 | 87.4% |
| Error Dialog (600x400) | 540 | 65 | 88.0% |

**Aggregate: 127,200 tokens -> 10,000 tokens over 20 turns across 8 test images.**

### Latency

| Metric | Time |
|--------|------|
| Cache check (avg) | 74.8ms |
| Cache write (avg) | 92.7ms |
| LLM image processing (typical) | 2,000-10,000ms |

Cache operations add <100ms overhead vs 2-10 seconds for a fresh LLM image read.

### Reliability

| Test Suite | Result |
|------------|--------|
| Image integrity after injection | 8/8 passed |
| Cache invalidation on modification | 8/8 passed |
| Format coverage (JPEG/PNG/WebP/GIF/BMP) | 5/5 passed |
| **Total** | **21/21 passed** |

## Requirements

- Python 3.9+
- No external dependencies (uses only Python standard library)
- Optional: `exiftool` for enhanced format support
- Optional: `Pillow` for the benchmark harness

## What This Skill Provides

| Capability | Detail |
|---|---|
| Persistent cache | Description stays until the image changes |
| Token cost | ~2-5% of original image tokens per read |
| Cross-session | Cache survives between conversations |
| Cross-agent | Any skills-compatible agent can read the cached description |
| Cross-machine | Metadata is embedded in the file and travels with it |

## License

MIT
