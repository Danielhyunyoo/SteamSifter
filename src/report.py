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

    # Build the ranked cards (already ordered by impact from the themes step).
    cards = "".join(
        render_theme_card(i + 1, t, max_impact) for i, t in enumerate(real_themes)
    )

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
</style>
</head>
<body>
  <header>
    <div class="brand">SteamSifter</div>
    <h1>{esc(title)}</h1>
    <div class="meta">{esc(subtitle)} &middot; {total_reviews} reviews &middot; Generated {generated}</div>
  </header>
  <main>
    <h2>{esc(section_heading)}</h2>
    {cards}
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
