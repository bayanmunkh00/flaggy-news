"""Score card image for a football result: team logos, names and the score on one PNG."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("news-bot")

W, H = 900, 450
BG, PANEL, FG, MUTED, ACCENT = "#0f172a", "#1e293b", "#f8fafc", "#94a3b8", "#38bdf8"
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # GitHub Actions (ubuntu)
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",       # macOS
    "/Library/Fonts/Arial Bold.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _logo(url: str, size: int) -> Image.Image | None:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except (requests.RequestException, OSError) as exc:
        log.info("Logo not loaded (%s): %s", url, exc)
        return None
    img.thumbnail((size, size), Image.LANCZOS)
    return img


def _centered(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str, font, fill: str, max_w: int) -> None:
    while draw.textlength(text, font=font) > max_w and len(text) > 4:
        text = text[:-2].rstrip() + "…"
    draw.text((cx - draw.textlength(text, font=font) / 2, y), text, font=font, fill=fill)


def render(card: dict) -> bytes:
    """card: league, status, home/away {name, score, logo}."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 70], fill=PANEL)
    d.rectangle([0, 70, W, 74], fill=ACCENT)
    _centered(d, W // 2, 18, f"{card['league']}  ·  {card['status']}", _font(30), FG, W - 40)

    for side, cx in (("home", 170), ("away", W - 170)):
        team = card[side]
        logo = _logo(team.get("logo", ""), 190)
        if logo:
            img.paste(logo, (cx - logo.width // 2, 105 + (190 - logo.height) // 2), logo)
        else:
            d.ellipse([cx - 80, 120, cx + 80, 280], fill=PANEL)
            _centered(d, cx, 175, team["name"][:3].upper(), _font(54), MUTED, 150)
        _centered(d, cx, 325, team["name"], _font(32), FG, 300)

    score = f"{card['home']['score']} - {card['away']['score']}"
    _centered(d, W // 2, 140, score, _font(110), FG, 340)
    _centered(d, W // 2, 275, "FULL TIME" if card["status"].startswith("FT") else card["status"],
              _font(24), MUTED, 300)

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def save(card: dict, path: Path) -> bool:
    try:
        data = render(card)
    except Exception as exc:  # a broken image must never stop the result being posted
        log.warning("Score card failed: %s", exc)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True
