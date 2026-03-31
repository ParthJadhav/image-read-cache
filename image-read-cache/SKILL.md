---
name: image-read-cache
description: >
  Optimizes image reading by caching LLM-generated content as XMP metadata
  inside image files. Use BEFORE reading any image file (.png, .jpg,
  .jpeg, .webp, .gif, .bmp). Reduces token usage by ~90% on repeated
  image reads. Triggers on: image, screenshot, picture, photo, .png,
  .jpg, .jpeg, read image, view image, look at image.
compatibility: Requires Python 3.9+
allowed-tools: Bash(python3:*) Read
metadata:
  author: parth-jadhav
  version: "1.0"
---

# Image Cache Skill

## Purpose

Avoid sending the same image to the LLM repeatedly. Instead, cache what the LLM produced the first time it read the image, and return that cached content on subsequent reads. The cached content is stored as XMP metadata directly inside the image file.

## When to use

Before EVERY image file read (.png, .jpg, .jpeg, .webp, .gif, .bmp), run the cache check first.

## Workflow

### Step 1: Check cache before reading

```bash
python3 <skill-dir>/scripts/check_cache.py "<absolute-image-path>"
```

**If output starts with `CACHED:`** — the rest of the output is the exact content the LLM produced last time it read this image. Use it directly as if the image was just read. Do NOT read the actual image file. Skip to Step 3.

**If output is `NO_CACHE`** — no cached content exists, or the image has changed since it was cached. Proceed to Step 2.

### Step 2: Read image and cache the result

Read the image normally using your file-read tool (base64 to the LLM).

After the image has been processed, cache what you received. The content to cache is EXACTLY what the file-read tool returned to you — the raw tool result, unchanged. Do not summarize, reformat, restructure, or add commentary. The cached version must be a faithful replica of the original tool output so that future reads produce identical results.

Since base64 image data cannot be stored in XMP metadata, you must instead produce a comprehensive text representation of everything visible in the image. This text IS the cache — anything you omit will be invisible on future reads. Capture:
- All visible text exactly as written (OCR-accurate)
- Layout structure and spatial relationships (what is where)
- Every UI element, color, shape, icon, and visual detail
- All data: numbers, labels, chart values, table contents
- Context clues: window titles, URLs, timestamps, filenames

Cache it:

```bash
python3 <skill-dir>/scripts/write_cache.py "<absolute-image-path>" "<your-complete-text-representation>"
```

### Step 3: Continue with task

Use the content (cached or fresh) to answer the user's question.

## Important rules

- ALWAYS check cache before reading an image. It costs <1 second and saves thousands of tokens.
- ALWAYS write back after a fresh image read. The next read becomes free.
- The cache includes a file hash. If the image file changes (re-saved, re-exported, new screenshot), the cache auto-invalidates and returns NO_CACHE.
- If write_cache.py fails (read-only file, permissions), continue normally. Caching is best-effort.
- If the user explicitly asks to "re-examine", "look again at", or "re-read" the image, SKIP the cache check and read fresh.
- Do NOT cache images the user is actively editing or generating (e.g., mid-workflow screenshots). Only cache stable assets.
