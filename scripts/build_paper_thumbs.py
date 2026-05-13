#!/usr/bin/env python3
"""
Build paper thumbnails from arXiv PDFs into /images/papers/{arxiv_id}.webp.

For each paper in _data/papers.yml:
  1. Skip if `image:` is set explicitly (manual override) and file exists.
  2. Resolve arXiv ID from `arxiv:` field, else extract from `link:`.
  3. Download the PDF (cached under scripts/.cache/).
  4. Score every "Figure N:" caption by keywords; the highest-scoring
     caption's region above it is rendered, auto-trimmed, and saved.
  5. If no caption scores high enough (or no PDF / no captions), fall back
     to a designed title card (USC cardinal accent + title + venue + year).

Re-run after adding a paper. Idempotent: existing thumbnails are kept unless
`--force` is passed.

Usage:
  python scripts/build_paper_thumbs.py
  python scripts/build_paper_thumbs.py --force
  python scripts/build_paper_thumbs.py --only 2604.17299
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
import fitz                                # PyMuPDF
from PIL import Image, ImageDraw, ImageFont
import numpy as np


REPO        = Path(__file__).resolve().parent.parent
PAPERS_YML  = REPO / "_data" / "papers.yml"
OUT_DIR     = REPO / "images" / "papers"
CACHE_DIR   = REPO / "scripts" / ".cache"

CARD_W, CARD_H = 480, 360
ACCENT         = (152, 27, 30)           # #981B1E -- USC cardinal
ACCENT_FG      = (255, 255, 255)
CARD_BG        = (247, 248, 250)         # --surface
CARD_FG        = (24, 24, 27)            # --text
CARD_MUTED     = (107, 114, 128)         # --muted

# Strong "teaser figure" keywords (case-insensitive, in caption text).
KEYWORDS_STRONG  = ("overview", "framework", "architecture", "pipeline",
                    "illustration", "we propose", "our method", "our approach",
                    "our model", "our system", "our framework", "our pipeline")
# Medium teaser hints -- not as definitive but still suggest a "what this is" figure.
KEYWORDS_MEDIUM  = ("examines", "introduces", "presents", "illustrates",
                    "depicts", "shows the", "case study", "training pipeline",
                    "model architecture", "key tasks")
# Soft penalty -- usually means a results figure / bar chart, not a teaser.
KEYWORDS_RESULTS = ("results", "comparison", "ablation", "performance",
                    "accuracy", "hyperparameter", "sensitivity")

# ------------------------------------------------------------------ helpers --

ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.IGNORECASE)


def extract_arxiv_id(paper: dict) -> str | None:
    if paper.get("arxiv"):
        m = ARXIV_RE.search(str(paper["arxiv"]))
        return m.group(1) if m else str(paper["arxiv"]).strip()
    if paper.get("link"):
        m = ARXIV_RE.search(str(paper["link"]))
        if m:
            return m.group(1)
    return None


def download_pdf(arxiv_id: str, max_attempts: int = 4) -> Path:
    """Download arXiv PDF with exponential backoff on 429 / transient errors."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / f"{arxiv_id}.pdf"
    if dest.exists():
        return dest
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    delay = 4
    for attempt in range(1, max_attempts + 1):
        print(f"    downloading {url}  (attempt {attempt})")
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "raymondy.github.io thumbnail builder"},
            )
            with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
                f.write(r.read())
            return dest
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < max_attempts:
                print(f"      HTTP {e.code}; sleeping {delay}s...")
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < max_attempts:
                print(f"      {e!r}; sleeping {delay}s...")
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return dest


def find_captions(doc) -> list[dict]:
    """Return [{fig_num, text, bbox, page_num}], deduped by (fig_num, page)."""
    captions = []
    seen = set()
    cap_re = re.compile(r"^\s*(?:Figure|Fig\.?)\s*(\d+)\s*[.:]\s*(.*)$")
    for page_num in range(min(15, len(doc))):
        page = doc[page_num]
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                t = "".join(s["text"] for s in line["spans"]).strip()
                m = cap_re.match(t)
                if not m:
                    continue
                key = (int(m.group(1)), page_num)
                if key in seen:
                    continue
                seen.add(key)
                captions.append({
                    "fig_num":  int(m.group(1)),
                    "text":     m.group(2)[:300],
                    "bbox":     fitz.Rect(line["bbox"]),
                    "page_num": page_num,
                })
    return captions


def score_caption(cap: dict) -> float:
    text  = cap["text"].lower()
    score = 0.0
    if any(kw in text for kw in KEYWORDS_STRONG):
        score += 3
    if any(kw in text for kw in KEYWORDS_MEDIUM):
        score += 1.5
    if any(kw in text for kw in KEYWORDS_RESULTS) and "overview" not in text:
        score -= 1
    if cap["page_num"] <= 1:                          # early pages get a small nudge
        score += 1
    if cap["fig_num"] == 1:                           # mild preference if nothing else wins
        score += 0.5
    return score


def render_above(page: fitz.Page, caption_bbox: fitz.Rect, max_h: int = 620) -> fitz.Pixmap:
    """Render a tall region directly above the caption at 150 DPI.

    Column-aware: if the caption is narrow relative to the page width (signal
    of a two-column paper where the figure sits in one column), the render
    is restricted to that column so the *other* column's text (typically the
    abstract or body prose) doesn't leak in. Otherwise we render full-width
    for single-column / spanning figures."""
    pr = page.rect
    top = max(pr.y0 + 12, caption_bbox.y0 - max_h)

    page_w     = pr.x1 - pr.x0
    cap_w      = caption_bbox.x1 - caption_bbox.x0
    is_narrow  = cap_w < page_w * 0.62

    if is_narrow:
        # Caption is in one column -- render only that column.
        left  = max(pr.x0 + 4, caption_bbox.x0 - 8)
        right = min(pr.x1 - 4, caption_bbox.x1 + 8)
    else:
        # Caption spans the page -- render full-width.
        left, right = pr.x0 + 18, pr.x1 - 18

    fig_rect = fitz.Rect(left, top, right, caption_bbox.y0 - 4)
    return page.get_pixmap(clip=fig_rect, dpi=150)


def pixmap_to_pil(pix: fitz.Pixmap) -> Image.Image:
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def extract_figure_region(img: Image.Image,
                          tol: int = 16,
                          gap_px: int = 26,
                          pad: int = 10) -> Image.Image:
    """Crop the image down to just the figure body.

    Algorithm:
      1. Build a per-row "is this row content?" mask (>0.5% dark pixels).
      2. Walking *up* from the bottom (caption side), keep including content
         rows; once we hit a `gap_px`-pixel-tall whitespace stretch, stop --
         anything above that gap is "not part of the figure" (page header,
         abstract text, title block, etc.).
      3. Then side-trim whitespace, add a small padding back.
    """
    gray = np.array(img.convert("L"))
    h, w = gray.shape
    dark = gray < (255 - tol)
    row_density = dark.sum(axis=1)
    is_content = row_density > max(2, w * 0.005)

    if not is_content.any():
        return img

    # Walk up from the bottom (closest to caption) -- find where the figure ends.
    fig_top = 0
    in_gap = 0
    for r in range(h - 1, -1, -1):
        if is_content[r]:
            in_gap = 0
        else:
            in_gap += 1
            if in_gap >= gap_px:
                fig_top = r + in_gap
                break

    # Find figure bottom (last content row from the top).
    last_content = h - 1 - int(np.argmax(is_content[::-1]))

    # SAFETY: if the gap-detection cropped to less than 25% of the source
    # height, the figure probably contains big internal whitespace between
    # subpanels and we cut it short. Fall back to "all content rows" (no
    # gap-based top trim).
    kept_h = last_content - fig_top + 1
    if kept_h < h * 0.25:
        fig_top = int(np.argmax(is_content))

    # Side trim within [fig_top, last_content]
    sub = dark[fig_top:last_content + 1]
    if sub.size == 0:
        return img
    col_has_content = sub.any(axis=0)
    if not col_has_content.any():
        return img
    left = int(np.argmax(col_has_content))
    right = w - int(np.argmax(col_has_content[::-1]))

    top    = max(0, fig_top - pad)
    bottom = min(h, last_content + 1 + pad)
    left   = max(0, left - pad)
    right  = min(w, right + pad)
    return img.crop((left, top, right, bottom))


def fit_card(img: Image.Image,
             size: tuple[int, int] = (CARD_W, CARD_H),
             bg: tuple[int, int, int] = CARD_BG) -> Image.Image:
    canvas = Image.new("RGB", size, bg)
    fitted = img.copy()
    fitted.thumbnail(size, Image.LANCZOS)
    x = (size[0] - fitted.width)  // 2
    y = (size[1] - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas


# ---- title card fallback ---------------------------------------------------

_FONT_CACHE = {}

def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = (
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["/System/Library/Fonts/Supplemental/Arial.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = font
            return font
        except (OSError, IOError):
            continue
    fallback = ImageFont.load_default()
    _FONT_CACHE[key] = fallback
    return fallback


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = (current + " " + word).strip()
        w = draw.textbbox((0, 0), trial, font=font)[2]
        if w <= max_w:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def generate_title_card(paper: dict) -> Image.Image:
    """USC-cardinal label bar + light card with paper title + venue/year."""
    img  = Image.new("RGB", (CARD_W, CARD_H), CARD_BG)
    draw = ImageDraw.Draw(img)

    # Accent bar down the left
    draw.rectangle([(0, 0), (8, CARD_H)], fill=ACCENT)

    # Title (wrapped to ~80% width)
    title_font = get_font(26, bold=True)
    title      = paper.get("title", "Untitled")
    lines      = wrap_text(draw, title, title_font, CARD_W - 80)
    lines      = lines[:4]                # at most 4 lines

    line_h    = 34
    block_h   = len(lines) * line_h
    sub_font  = get_font(15, bold=False)
    venue     = paper.get("venue") or paper.get("journal") or ""
    date      = str(paper.get("date", ""))
    sub       = f"{venue} · {date}".strip(" ·") if venue or date else ""
    sub_h     = 22 if sub else 0

    y = (CARD_H - block_h - sub_h - 16) // 2
    for line in lines:
        draw.text((36, y), line, fill=CARD_FG, font=title_font)
        y += line_h
    if sub:
        draw.text((36, y + 12), sub, fill=CARD_MUTED, font=sub_font)
    return img


# ------------------------------------------------------------------ pipeline -


def process(paper: dict, *, force: bool, only_id: str | None) -> None:
    title    = paper.get("title", "?")[:70]
    arxiv_id = extract_arxiv_id(paper)
    print(f"- {title}")

    if only_id and arxiv_id != only_id:
        print(f"    skip (filter)")
        return

    # 1) explicit manual override always wins
    if paper.get("image") and not force:
        print(f"    manual image: {paper['image']}")
        return

    # 2) need an arxiv id to do anything automatic
    if not arxiv_id:
        print(f"    no arxiv id; user can set arxiv: or image: in papers.yml")
        return

    out_path = OUT_DIR / f"{arxiv_id}.webp"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if out_path.exists() and not force:
        print(f"    already exists: {out_path.name}")
        return

    # 3) try real figure extraction
    try:
        pdf  = download_pdf(arxiv_id)
        doc  = fitz.open(pdf)
        caps = find_captions(doc)

        if not caps:
            print(f"    no captions found -> title card")
            generate_title_card(paper).save(out_path, "WEBP", quality=88)
            doc.close()
            return

        scored = sorted(((score_caption(c), c) for c in caps), key=lambda p: -p[0])
        for s, c in scored[:5]:
            print(f"      fig {c['fig_num']} (p.{c['page_num']+1})  score={s:>4.1f}  "
                  f"{c['text'][:60]}")

        best_score, best = scored[0]
        if best_score < 2:
            print(f"    best score {best_score:.1f} < 2 -> title card")
            generate_title_card(paper).save(out_path, "WEBP", quality=88)
            doc.close()
            return

        pix = render_above(doc[best["page_num"]], best["bbox"])
        img = pixmap_to_pil(pix)
        img = extract_figure_region(img)
        card = fit_card(img)
        card.save(out_path, "WEBP", quality=88)
        print(f"    -> {out_path.name}  (picked Fig {best['fig_num']})")
        doc.close()

    except Exception as e:
        print(f"    ERROR: {e!r} -> title card")
        generate_title_card(paper).save(out_path, "WEBP", quality=88)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="re-generate even when the .webp already exists")
    parser.add_argument("--only", metavar="ARXIV_ID",
                        help="only process this arxiv id (e.g. 2604.17299)")
    args = parser.parse_args()

    with open(PAPERS_YML) as f:
        data = yaml.safe_load(f) or {}

    for section in ("preprints", "conference", "journal"):
        papers = data.get(section) or []
        if not papers:
            continue
        print(f"\n=== {section}  ({len(papers)} papers) ===")
        for paper in papers:
            process(paper, force=args.force, only_id=args.only)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
