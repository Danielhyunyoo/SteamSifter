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
import time

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
    url = HEADER_URL.format(appid=appid) + f"?t={int(time.time() // 86400)}"
    r = requests.get(url, headers=HEADERS, timeout=10)
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
    base = Image.composite(Image.new("RGB", (W, H), DARK), base, _vgrad(110, 200))

    draw = ImageDraw.Draw(base)
    f_brand = _font("DejaVuSans-Bold.ttf", 26)
    f_title = _font("DejaVuSans-Bold.ttf", 52)
    f_label = _font("DejaVuSans-Bold.ttf", 25)
    f_value = _font("DejaVuSans.ttf", 29)
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

    # Positive / negative split, upper-right (mirrors the report scoreboard).
    if analysis is not None:
        st = analysis.get("sentiment_totals", {}) or {}
        tot = st.get("positive", 0) + st.get("negative", 0) + st.get("neutral", 0)
        if tot:
            f_snum = _font("DejaVuSans-Bold.ttf", 50)
            f_slab = _font("DejaVuSans.ttf", 22)
            rx = W - PAD
            draw.text((rx, 160), f"{round(st.get('positive', 0) / tot * 100)}%", font=f_snum, fill=GREEN, anchor="rm")
            draw.text((rx, 197), "Positive", font=f_slab, fill=SUBTLE, anchor="rm")
            draw.text((rx, 250), f"{round(st.get('negative', 0) / tot * 100)}%", font=f_snum, fill=RED, anchor="rm")
            draw.text((rx, 287), "Negative", font=f_slab, fill=SUBTLE, anchor="rm")

    # Everything below is a single left-aligned column at the left margin.
    x = PAD
    full_w = W - 2 * PAD

    # Sharp banner above the title.
    y = 130
    if banner is not None:
        bw = 330
        bh = int(bw * banner.size[1] / banner.size[0])
        thumb = banner.resize((bw, bh), Image.LANCZOS)
        base.paste(thumb, (x, y))
        draw.rectangle((x - 1, y - 1, x + bw, y + bh), outline=(210, 216, 224), width=1)
        y += bh + 24
    else:
        y = 170

    # Title.
    draw.text((x, y), _truncate(draw, title, f_title, full_w), font=f_title, fill=WHITE, anchor="lt")
    y += 58

    if analysis is not None:
        total = analysis.get("total_reviews", 0)
        draw.text((x, y), f"{total:,} reviews analyzed", font=f_sub, fill=SUBTLE, anchor="lt")
        y += 46

        praise = _top_theme(analysis.get("positive"))
        fix = _top_theme(analysis.get("negative"))
        if praise or fix:
            draw.line((x, y, x + full_w, y), fill=(110, 120, 134), width=1)
            y += 30
            label_col = 210
            if praise:
                draw.text((x, y), "▲ Top praise", font=f_label, fill=GREEN, anchor="lm")
                draw.text((x + label_col, y), _truncate(draw, praise, f_value, full_w - label_col),
                          font=f_value, fill=WHITE, anchor="lm")
                y += 50
            if fix:
                draw.text((x, y), "▼ Top fix", font=f_label, fill=RED, anchor="lm")
                draw.text((x + label_col, y), _truncate(draw, fix, f_value, full_w - label_col),
                          font=f_value, fill=WHITE, anchor="lm")
    else:
        draw.text((x, y), "Review intelligence for game studios", font=f_sub, fill=SUBTLE, anchor="lt")

    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue()
