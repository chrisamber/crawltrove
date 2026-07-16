"""Tesseract OCR for image-only PDF pages (one signal, resilient by default).

PDF text-layer extraction returns nothing for scanned/image-only
pages, so they otherwise vanish from the corpus. This module renders such a
page to a bitmap and runs local Tesseract OCR over it, auto-detecting the
script per page (Tesseract OSD) to pick the matching language model. Mirrors
the other signal modules: one job, and every error is swallowed into a None
return so a single bad page — or a host with no tesseract binary — never fails
a scrape.
"""
import os
from typing import Any, Dict, List, Optional, Tuple

import pytesseract
from pytesseract import Output
from PIL import Image

# OSD script_conf floor; below it we fall back to default_languages. Tuned
# during verification: real Han detection reports script_conf ~0.78 on a clean
# page, so the original 1.5 guess rejected genuine CJK. 0.5 accepts it with
# margin while still dropping near-zero noise. Non-CJK misdetections (e.g. a
# Latin page read as "Cyrillic") are harmless — they aren't in SCRIPT_LANGS and
# fall back to default_languages anyway. Override via OCR_OSD_MIN_CONF.
OCR_OSD_MIN_CONF = float(os.getenv("OCR_OSD_MIN_CONF", "0.5"))

# OSD script name -> installed Tesseract language packs. Anything not here
# (or a low-confidence / failed OSD) falls back to default_languages.
SCRIPT_LANGS = {
    "Latin": "eng",
    "Han": "chi_sim+chi_tra",
    "Japanese": "jpn",
    "Korean": "kor",
    "Hangul": "kor",
}


def _render(page, dpi: int) -> Image.Image:
    """Render a pypdfium2 page to an independent PIL image."""
    bitmap = page.render(scale=dpi / 72)
    try:
        return bitmap.to_pil().copy()
    finally:
        bitmap.close()


def _detect_languages(img: Image.Image, default: str, use_osd: bool) -> Tuple[str, Optional[str]]:
    """Pick Tesseract languages for a page via OSD script detection.

    Returns (languages, script). Falls back to (default, None) when OSD is
    disabled, errors (e.g. too few characters to orient), or reports
    script_conf below OCR_OSD_MIN_CONF.
    """
    if not use_osd:
        return default, None
    try:
        osd = pytesseract.image_to_osd(img, output_type=Output.DICT)
        if float(osd.get("script_conf", 0)) >= OCR_OSD_MIN_CONF:
            script = osd.get("script")
            return SCRIPT_LANGS.get(script, default), script
    except Exception:
        pass
    return default, None


def _run(img: Image.Image, languages: str) -> Tuple[str, Optional[float]]:
    """OCR an image and return (text, mean_confidence).

    Text is reconstructed in layout order — words joined per line, lines per
    block/paragraph. confidence is the mean of per-word conf values >= 0 on
    Tesseract's 0-100 scale, or None when no words were read.
    """
    data = pytesseract.image_to_data(img, lang=languages, output_type=Output.DICT)
    lines: Dict[Tuple[int, int, int], List[str]] = {}
    order: List[Tuple[int, int, int]] = []
    for i, word in enumerate(data["text"]):
        if not word or not word.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(word)
    text = "\n".join(" ".join(lines[k]) for k in order)
    valid = [v for v in (int(c) for c in data["conf"]) if v >= 0]
    confidence = round(sum(valid) / len(valid), 1) if valid else None
    return text, confidence


def ocr_image(img: Image.Image, *, default_languages: str = "eng",
              use_osd: bool = True) -> Optional[Dict[str, Any]]:
    """OCR a PIL image (a rendered PDF page or a standalone image scrape).

    Returns {"text", "confidence", "languages", "script"} or None on any
    failure (missing tesseract binary, OSD error, …) so the caller keeps
    whatever text it already had and the scrape never fails.
    """
    try:
        languages, script = _detect_languages(img, default_languages, use_osd)
        text, confidence = _run(img, languages)
        return {"text": text, "confidence": confidence,
                "languages": languages, "script": script}
    except Exception:
        return None


def ocr_page(page, *, default_languages: str = "eng", dpi: int = 300,
             use_osd: bool = True) -> Optional[Dict[str, Any]]:
    """OCR a single pypdfium2 page: render it, then `ocr_image` the bitmap.

    Same return contract as `ocr_image` — None on any failure (including a
    render error) so one bad page never fails a scrape.
    """
    try:
        img = _render(page, dpi)
    except Exception:
        return None
    return ocr_image(img, default_languages=default_languages, use_osd=use_osd)
