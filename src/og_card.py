"""
og_card.py

Generate the 1200x630 social-share (OpenGraph) image for a game: the Steam
banner blurred as an ambient background, a sharp crop of the banner beside the
title, the Steam-style rating pill, and the top praise / top fix themes. Served
by the /og/<appid>.png route so shared links unfurl into a rich card.

The real banner is fetched from Steam's CDN at render time; if it is unavailable
the card still renders on a dark background. Falls back gracefully when no
analysis is cached yet (banner + title, no stats).
"""

import io
import os

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

W, H = 1200, 630
PAD = 56
FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
HEADER_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
HEADERS = {"User-Agent": "SteamSifter/0.1 (social card)"}

BLUE = (102, 192, 244)
WHITE = (255, 255, 255)
SUBTLE = (195, 204, 214)
GREEN = (167, 215, 127)
RED = (232, 134, 141)
DARK = (11, 16, 24)


def _font(name, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def _hex(h):
    h = (h or "#000000").lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _cover(img, w, h):
    """Scale + center-crop img to exactly fill w x h (CSS object-fit: cover)."""
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    nw, nh = int(iw * scale + 0.5), int(ih * scale + 0.5)
    img = img.resize((nw, nh), Image.LANCZOS)
    x, y = (nw - w) // 2, (nh - h) // 2
    return img.crop((x, y, x + w, y + h))


def _fetch_banner(appid):
    r = requests.get(HEADER_URL.format(appid=appid), headers=HEADERS, timeout=10)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _truncate(draw, text, font, max_w):
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    while text and draw.textlength(text + ell, font=font) > max_w:
        text = text[:-1]
    return (text.rstrip() + ell) if text else ell


def _vgrad(top_a, bot_a):
    g = Image.new("L", (1, H))
    for y in range(H):
        g.putpixel((0, y), int(top_a + (bot_a - top_a) * (y / (H - 1))))
    return g.resize((W, H))


def _rating(analysis):
    if not analysis:
        return "", None, None
    from report import steam_rating
    st = analysis.get("sentiment_totals", {}) or {}
    label, bg, fg = steam_rating(st.get("positive", 0), st.get("negative", 0),
                                 analysis.get("total_reviews", 0))
    if not label:
        return "", None, None
    return label, _hex(bg), _hex(fg)


def _top_theme(recs):
    for rec in recs or []:
        th = rec.get("theme", "")
        if th and th not in ("noise", "unclear"):
            return th
    return ""


def render(appid, title, analysis=None, banner=None):
    """Return PNG bytes for the share card. `banner` is for testing/overrides."""
    if banner is None:
        try:
            banner = _fetch_banner(appid)
        except Exception:
            banner = None

    base = Image.new("RGB", (W, H), (22, 32, 45))
    if banner is not None:
        bg = _cover(banner, W, H).filter(ImageFilter.GaussianBlur(28))
        bg = ImageEnhance.Brightness(bg).enhance(0.55)
        base.paste(bg, (0, 0))
    base = Image.composite(Image.new("RGB", (W, H), DARK), base, _vgrad(120, 205))

    draw = ImageDraw.Draw(base)
    f_brand = _font("DejaVuSans-Bold.ttf", 26)
    f_title = _font("DejaVuSans-Bold.ttf", 46)
    f_label = _font("DejaVuSans-Bold.ttf", 23)
    f_value = _font("DejaVuSans.ttf", 27)
    f_pill = _font("DejaVuSans-Bold.ttf", 23)
    f_sub = _font("DejaVuSans.ttf", 26)

    # Wordmark: magnifier + STEAMSIFTER (letter-spaced), top-left.
    brand_cy = PAD + 4
    draw.ellipse((PAD, brand_cy - 9, PAD + 16, brand_cy + 7), outline=BLUE, width=3)
    draw.line((PAD + 14, brand_cy + 5, PAD + 21, brand_cy + 12), fill=BLUE, width=3)
    bx = PAD + 32
    for ch in "STEAMSIFTER":
        draw.text((bx, brand_cy), ch, font=f_brand, fill=BLUE, anchor="lm")
        bx += draw.textlength(ch, font=f_brand) + 3

    # Rating pill, top-right.
    label, rbg, rfg = _rating(analysis)
    if label:
        tw = draw.textlength(label, font=f_pill)
        pill_w, pill_h = tw + 34, 40
        px = W - PAD - pill_w
        py = PAD - 16
        draw.rounded_rectangle((px, py, px + pill_w, py + pill_h), radius=8, fill=rbg)
        draw.text((px + pill_w / 2, py + pill_h / 2), label, font=f_pill, fill=rfg, anchor="mm")

    # Bottom block: sharp banner thumbnail + title + insights.
    by1 = H - PAD
    text_x = PAD
    if banner is not None:
        thumb_w = 300
        thumb_h = int(thumb_w * banner.size[1] / banner.size[0])
        thumb = banner.resize((thumb_w, thumb_h), Image.LANCZOS)
        ty0 = by1 - thumb_h
        base.paste(thumb, (PAD, ty0))
        draw.rectangle((PAD - 1, ty0 - 1, PAD + thumb_w, ty0 + thumb_h),
                       outline=(210, 216, 224), width=1)
        text_x = PAD + thumb_w + 30
        block_top = ty0
        block_h = thumb_h
    else:
        block_top = by1 - 150
        block_h = 150

    max_w = W - PAD - text_x
    title_cy = block_top + 26
    draw.text((text_x, title_cy), _truncate(draw, title, f_title, max_w),
              font=f_title, fill=WHITE, anchor="lm")

    praise = _top_theme(analysis.get("positive")) if analysis else ""
    fix = _top_theme(analysis.get("negative")) if analysis else ""
    label_col = 224
    iy = title_cy + 62
    if analysis is not None and (praise or fix):
        if praise:
            draw.text((text_x, iy), "▲ Top praise", font=f_label, fill=GREEN, anchor="lm")
            draw.text((text_x + label_col, iy), _truncate(draw, praise, f_value, max_w - label_col),
                      font=f_value, fill=WHITE, anchor="lm")
            iy += 46
        if fix:
            draw.text((text_x, iy), "▼ Top fix", font=f_label, fill=RED, anchor="lm")
            draw.text((text_x + label_col, iy), _truncate(draw, fix, f_value, max_w - label_col),
                      font=f_value, fill=WHITE, anchor="lm")
    else:
        draw.text((text_x, iy), "Review intelligence for game studios",
                  font=f_sub, fill=SUBTLE, anchor="lm")

    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue()
