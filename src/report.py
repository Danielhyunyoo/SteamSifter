"""
report.py

Turns a themes file into a clean, standalone HTML report.

In "negative" mode it produces a "Fix These" dashboard of problems to address.
In "positive" mode it produces a "Double Down" dashboard of strengths to lean
into. This is the first visual view of the pipeline and a preview of the
eventual web app.

It uses no API calls, just the saved themes JSON, so it is free to run anytime.

Run:
    python src/report.py
    python src/report.py data/themes_reviews_1085660_positive.json --title "Destiny 2" --mode positive
"""

import argparse
import html
import json
import os
from datetime import date


# A color per category, used for the little category "pill" on each card.
CATEGORY_COLORS = {
    "bug": "#e06c75",
    "performance": "#d19a66",
    "gameplay": "#61afef",
    "cheating": "#c678dd",
    "community": "#56b6c2",
    "monetization": "#e5c07b",
    "content": "#98c379",
    "ui_ux": "#abb2bf",
    "praise": "#98c379",
    "other": "#7f848e",
}

# Colors for the sentiment overview bar.
SENTIMENT_COLORS = {
    "positive": "#98c379",
    "negative": "#e06c75",
    "neutral": "#abb2bf",
}

# The label used for constructive reviews that matched no theme.
UNCLEAR_LABEL = "unclear"

# The label used for reviews filtered out as noise before theming.
NOISE_LABEL = "noise"


def esc(text) -> str:
    """Escape text so review content can't break the HTML."""
    return html.escape(str(text))


def render_example(example: dict) -> str:
    """Render one example quote with its credibility badges."""
    text = esc(example.get("text", ""))
    hours = example.get("playtime_at_review_hours", 0)
    helpful = example.get("helpful_votes", 0)
    return (
        '<div class="example">'
        f'<span class="quote">&ldquo;{text}&rdquo;</span>'
        '<span class="badges">'
        f'<span class="badge">{hours:g}h played</span>'
        f'<span class="badge">{helpful} helpful</span>'
        '</span>'
        '</div>'
    )


def render_theme_card(rank: int, theme: dict, max_impact: float) -> str:
    """Render one theme as a ranked card with an impact-proportional bar."""
    name = esc(theme["theme"])
    category = theme.get("category", "other")
    color = CATEGORY_COLORS.get(category, "#7f848e")
    count = theme["count"]
    impact = theme.get("impact_score", 0)
    description = esc(theme.get("description", ""))

    # Bar width reflects IMPACT (playtime- and helpful-weighted), not raw count.
    width = int((impact / max_impact) * 100) if max_impact else 0

    examples_html = "".join(render_example(e) for e in theme.get("examples", []))

    return f"""
    <div class="card">
      <div class="card-head">
        <span class="rank">#{rank}</span>
        <span class="theme-name">{name}</span>
        <span class="pill" style="background:{color}">{esc(category)}</span>
        <span class="count">{count} reviews &middot; impact {impact:g}</span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:{width}%;background:{color}"></div></div>
      <p class="description">{description}</p>
      <div class="examples">{examples_html}</div>
    </div>
    """


def render_overview(records: list) -> str:
    """
    Build the at-a-glance overview: an overall sentiment bar and a category
    distribution. Reads the per-theme sentiment_counts saved by the themes step.
    """
    # Sentiment totals across every record (themes + unclear + noise).
    sent = {"positive": 0, "negative": 0, "neutral": 0}
    for r in records:
        for key, val in (r.get("sentiment_counts") or {}).items():
            sent[key] = sent.get(key, 0) + val
    sent_total = sum(sent.values())

    # Category totals over meaningful feedback (everything except the noise bucket).
    cats = {}
    for r in records:
        if r["theme"] == NOISE_LABEL:
            continue
        cats[r["category"]] = cats.get(r["category"], 0) + r["count"]
    cat_total = sum(cats.values()) or 1

    # Stacked sentiment bar + legend.
    segments, legend = "", ""
    for key in ("positive", "negative", "neutral"):
        val = sent.get(key, 0)
        pct = (val / sent_total * 100) if sent_total else 0
        if val:
            segments += f'<div class="seg" style="width:{pct:.1f}%;background:{SENTIMENT_COLORS[key]}"></div>'
        legend += (
            f'<span class="legend-item"><span class="dot" style="background:{SENTIMENT_COLORS[key]}"></span>'
            f'{key} {val}</span>'
        )

    # Category bars, most common first.
    cat_rows = ""
    for category, c in sorted(cats.items(), key=lambda kv: kv[1], reverse=True):
        pct = c / cat_total * 100
        color = CATEGORY_COLORS.get(category, "#7f848e")
        cat_rows += (
            '<div class="cat-row">'
            f'<span class="cat-label">{esc(category)}</span>'
            f'<span class="cat-track"><span class="cat-fill" style="width:{pct:.1f}%;background:{color}"></span></span>'
            f'<span class="cat-num">{c}</span>'
            '</div>'
        )

    return (
        '<div class="overview">'
        '<div class="ov-title">Sentiment</div>'
        f'<div class="sentiment-bar">{segments}</div>'
        f'<div class="legend">{legend}</div>'
        '<div class="ov-title">By category</div>'
        f'<div class="cat-list">{cat_rows}</div>'
        '</div>'
    )


def build_html(themes: list, title: str, mode: str = "negative") -> str:
    """
    Assemble the full HTML document from the theme records.

    mode controls the framing:
      - "negative": a "Fix These" dashboard of problems to address.
      - "positive": a "Double Down" dashboard of strengths to lean into.
    """
    # Choose the wording based on whether we're showing praise or complaints.
    if mode == "positive":
        subtitle = "Positive review analysis"
        section_heading = "Double Down - ranked by impact (playtime + helpful votes)"
    else:
        subtitle = "Negative review analysis"
        section_heading = "Fix These - ranked by impact (playtime + helpful votes)"

    # Separate real themes from the noise/unclear bucket.
    real_themes = [t for t in themes if t["theme"] not in (UNCLEAR_LABEL, NOISE_LABEL)]
    unclear = next((t for t in themes if t["theme"] == UNCLEAR_LABEL), None)
    noise = next((t for t in themes if t["theme"] == NOISE_LABEL), None)

    total_reviews = sum(t["count"] for t in themes)
    max_impact = max((t.get("impact_score", 0) for t in real_themes), default=1) or 1

    # Build the theme section(s). In positive mode we split actionable feature
    # praise (Double Down) from non-actionable emotional sentiment.
    def _section(heading, items):
        body = "".join(
            render_theme_card(i + 1, t, max_impact) for i, t in enumerate(items)
        )
        return f"<h2>{esc(heading)}</h2>{body}"

    if mode == "positive":
        features = [t for t in real_themes if t.get("kind", "feature") != "emotional"]
        emotional = [t for t in real_themes if t.get("kind") == "emotional"]
        sections_html = _section(section_heading, features)
        if emotional:
            sections_html += _section(
                "Player sentiment - emotional, not directly actionable", emotional
            )
    else:
        sections_html = _section(section_heading, real_themes)

    # A muted note for the low-signal pile, shown honestly rather than hidden.
    low_signal_parts = []
    if noise:
        npct = round((noise["count"] / total_reviews) * 100) if total_reviews else 0
        low_signal_parts.append(
            f"<strong>{noise['count']} reviews ({npct}%)</strong> were filtered out "
            "as noise (jokes, one-liners, off-topic rants, and spam) before theming."
        )
    if unclear:
        upct = round((unclear["count"] / total_reviews) * 100) if total_reviews else 0
        low_signal_parts.append(
            f"<strong>{unclear['count']} reviews ({upct}%)</strong> were constructive "
            "but did not match a specific theme."
        )
    unclear_html = ""
    if low_signal_parts:
        unclear_html = '<div class="unclear">' + " ".join(low_signal_parts) + "</div>"

    generated = date.today().strftime("%B %d, %Y")

    overview_html = render_overview(themes)

    # The CSS lives inline so the report is a single portable file. Note that
    # every literal CSS brace is doubled ({{ }}) because this is an f-string.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SteamSifter Report: {esc(title)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #f4f5f7; color: #1c1e21; }}
  header {{ background: #171a21; color: #fff; padding: 28px 32px; }}
  header .brand {{ font-size: 14px; letter-spacing: 2px; color: #66c0f4; text-transform: uppercase; }}
  header h1 {{ margin: 6px 0 4px; font-size: 26px; }}
  header .meta {{ color: #9aa4b2; font-size: 14px; }}
  main {{ max-width: 880px; margin: 0 auto; padding: 28px 20px 60px; }}
  h2 {{ font-size: 18px; margin: 24px 0 12px; }}
  .card {{ background: #fff; border: 1px solid #e3e6ea; border-radius: 10px; padding: 18px 20px; margin-bottom: 14px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }}
  .card-head {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .rank {{ font-weight: 700; color: #8a929e; font-size: 14px; }}
  .theme-name {{ font-weight: 700; font-size: 16px; }}
  .pill {{ color: #1c1e21; font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 20px; text-transform: lowercase; }}
  .count {{ margin-left: auto; font-size: 13px; color: #6b7280; font-weight: 600; }}
  .bar-track {{ background: #eef0f3; border-radius: 6px; height: 8px; margin: 12px 0 10px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 6px; }}
  .description {{ font-size: 14px; color: #3c4149; margin: 0 0 12px; }}
  .example {{ border-left: 3px solid #e3e6ea; padding: 4px 0 4px 12px; margin: 8px 0; }}
  .quote {{ font-style: italic; color: #2c3038; font-size: 13px; }}
  .badges {{ display: block; margin-top: 4px; }}
  .badge {{ display: inline-block; font-size: 11px; background: #eef0f3; color: #5b6470; border-radius: 4px; padding: 1px 7px; margin-right: 6px; }}
  .unclear {{ background: #fff8e6; border: 1px solid #f0e2b8; border-radius: 10px; padding: 16px 18px; font-size: 14px; color: #5c531f; }}
  .overview {{ background: #fff; border: 1px solid #e3e6ea; border-radius: 10px; padding: 18px 20px; margin-bottom: 18px; }}
  .ov-title {{ font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: #8a929e; margin: 10px 0 8px; font-weight: 700; }}
  .ov-title:first-child {{ margin-top: 0; }}
  .sentiment-bar {{ display: flex; height: 14px; border-radius: 7px; overflow: hidden; background: #eef0f3; }}
  .seg {{ height: 100%; }}
  .legend {{ margin: 8px 0 4px; font-size: 12px; color: #5b6470; }}
  .legend-item {{ margin-right: 14px; text-transform: lowercase; }}
  .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }}
  .cat-row {{ display: flex; align-items: center; gap: 8px; margin: 5px 0; font-size: 12px; }}
  .cat-label {{ width: 95px; color: #3c4149; text-transform: lowercase; }}
  .cat-track {{ flex: 1; background: #eef0f3; border-radius: 5px; height: 8px; overflow: hidden; }}
  .cat-fill {{ display: block; height: 100%; border-radius: 5px; }}
  .cat-num {{ width: 34px; text-align: right; color: #6b7280; font-weight: 600; }}
</style>
</head>
<body>
  <header>
    <div class="brand">SteamSifter</div>
    <h1>{esc(title)}</h1>
    <div class="meta">{esc(subtitle)} &middot; {total_reviews} reviews &middot; Generated {generated}</div>
  </header>
  <main>
    <h2>Overview</h2>
    {overview_html}
    {sections_html}
    <h2>Low-signal reviews</h2>
    {unclear_html}
  </main>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(
        description="Generate an HTML report from a themes JSON file."
    )
    parser.add_argument(
        "themes_file",
        nargs="?",
        default="data/themes_reviews_730_negative.json",
        help="Path to a themes JSON file",
    )
    parser.add_argument(
        "--title", default="Counter-Strike 2 (App 730)",
        help="Game title shown in the report header",
    )
    parser.add_argument(
        "--out", default="steamsifter_report.html",
        help="Output HTML file path (default: steamsifter_report.html)",
    )
    parser.add_argument(
        "--mode", default="negative", choices=["negative", "positive"],
        help="negative = 'Fix These' framing, positive = 'Double Down' framing",
    )
    args = parser.parse_args()

    with open(args.themes_file, encoding="utf-8") as f:
        themes = json.load(f)

    html_doc = build_html(themes, args.title, args.mode)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print(f"Report written to {os.path.abspath(args.out)}")
    print("Open it in your browser (double-click the file, or right-click > Open with > browser).")


if __name__ == "__main__":
    main()
