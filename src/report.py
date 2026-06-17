"""
report.py

Turns a combined analysis (negative + positive themes) into one standalone HTML
report. The page defaults to the "Fix These" (negative) view and offers a toggle
to the "Double Down" (positive) view, with no re-running: both are precomputed.

It uses no API calls, just the saved analysis JSON, so it is free to run anytime.

Run:
    python src/report.py data/analysis_730.json --title "Counter-Strike 2"
"""

import argparse
import html
import json
import os
from datetime import date
from urllib.parse import quote


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
SENTIMENT_COLORS = {"positive": "#98c379", "negative": "#e06c75", "neutral": "#abb2bf"}

UNCLEAR_LABEL = "unclear"   # constructive reviews that matched no theme
NOISE_LABEL = "noise"       # reviews filtered out as low-signal

# Steam-style thumb icon (points up). The "down" variant reuses it rotated.
THUMB_SVG = (
    '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" '
    'aria-hidden="true"><path d="M2 21h4V9H2v12zM23 10c0-1.1-.9-2-2-2h-6.31'
    'l.95-4.57.03-.32a1.5 1.5 0 0 0-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 '
    '8.45 7 9v10a2 2 0 0 0 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23'
    '.14-.47.14-.73v-2z"/></svg>'
)


# Header live-search script (plain string so its JS braces need no escaping).
NAV_SEARCH_JS = """
<script>
(function () {
  const input = document.getElementById('navq');
  const box = document.getElementById('navresults');
  if (!input) return;
  let timer = null;
  input.addEventListener('input', function () {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { box.style.display = 'none'; return; }
    timer = setTimeout(function () { suggest(q); }, 250);
  });
  async function suggest(q) {
    try {
      const r = await fetch('/api/search?q=' + encodeURIComponent(q));
      const games = await r.json();
      if (!games.length) { box.style.display = 'none'; return; }
      box.innerHTML = '';
      games.forEach(function (g) {
        const row = document.createElement('div');
        row.className = 'navresult';
        row.innerHTML = (g.image ? '<img src="' + g.image + '">' : '<img>') +
                        '<span>' + g.name + '</span>';
        row.onclick = function () {
          window.location = '/analyzing?appid=' + g.appid +
            '&title=' + encodeURIComponent(g.name);
        };
        box.appendChild(row);
      });
      box.style.display = 'block';
    } catch (e) { box.style.display = 'none'; }
  }
})();
</script>
"""

# Toggle between the Fix These / Double Down views (plain string).
TOGGLE_JS = """
<script>
// Grow every [data-w] bar under root from 0 to its target width.
function animateBars(root) {
  root.querySelectorAll('[data-w]').forEach(function (el) {
    el.style.width = '0%';
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { el.style.width = el.getAttribute('data-w') + '%'; });
    });
  });
}

function showSide(which) {
  var fix = document.getElementById('side-fix');
  var love = document.getElementById('side-love');
  fix.style.display = (which === 'fix') ? 'block' : 'none';
  love.style.display = (which === 'love') ? 'block' : 'none';
  document.getElementById('btn-fix').classList.toggle('active', which === 'fix');
  document.getElementById('btn-love').classList.toggle('active', which === 'love');
  var slider = document.getElementById('toggle-slider');
  if (slider) slider.style.transform = (which === 'love') ? 'translateX(100%)' : 'translateX(0)';
  animateBars(which === 'love' ? love : fix);
}

document.addEventListener('DOMContentLoaded', function () {
  var overview = document.querySelector('.overview');
  if (overview) animateBars(overview);
  var fix = document.getElementById('side-fix');
  if (fix) animateBars(fix);
});
</script>
"""


def esc(text) -> str:
    """Escape text so review content can't break the HTML."""
    return html.escape(str(text))

def render_refresh(state: dict) -> str:
    """Render the Re-analyze control in its enabled, cooldown, or admin state.

    state is None for offline/CLI reports (no button). The live app passes a
    dict: {appid, title, allowed, wait_hours, admin}.
    """
    if not state:
        return ""
    appid = esc(state.get("appid", ""))
    href = f'/analyzing?appid={appid}&title={quote(state.get("title", ""))}&force=1'
    if state.get("admin"):
        return (f'<a class="refresh admin" href="{href}" '
                'title="Admin: refresh anytime">Re-analyze &#8635;'
                '<span class="admtag">admin</span></a>')
    if state.get("allowed"):
        return (f'<a class="refresh" href="{href}" '
                'title="Run a fresh analysis with the latest reviews">'
                'Re-analyze &#8635;</a>')
    wait = state.get("wait_hours", 0)
    return ('<span class="refresh disabled" title="This report was updated '
            'recently; a fresh run is available later">'
            f'Re-analyze available in ~{wait}h</span>')


def render_example(example: dict) -> str:
    """Render one example quote with credibility badges and a link to the real
    Steam review (when we have the reviewer's permalink)."""
    text = esc(example.get("text", ""))
    hours = example.get("playtime_at_review_hours", 0)
    helpful = example.get("helpful_votes", 0)
    url = example.get("url")
    voted = example.get("voted_up")

    # Steam recommend / not-recommend badge, only when we know the flag
    # (older cached analyses may not carry it).
    if voted is True:
        thumb = f'<span class="badge thumb up">{THUMB_SVG} Recommended</span>'
    elif voted is False:
        thumb = (f'<span class="badge thumb down">'
                 f'<span class="thumb-dn">{THUMB_SVG}</span> Not recommended</span>')
    else:
        thumb = ''

    if url:
        quote_html = (f'<a class="quote quote-link" href="{esc(url)}" target="_blank" '
                      f'rel="noopener">&ldquo;{text}&rdquo;</a>')
        source = (f'<a class="source" href="{esc(url)}" target="_blank" '
                  'rel="noopener">View on Steam &#8599;</a>')
    else:
        quote_html = f'<span class="quote">&ldquo;{text}&rdquo;</span>'
        source = ''

    return (
        '<div class="example">'
        f'{quote_html}'
        '<span class="badges">'
        f'{thumb}'
        f'<span class="badge">{hours:g}h played</span>'
        f'<span class="badge">{helpful} helpful</span>'
        f'{source}'
        '</span>'
        '</div>'
    )


def impact_level(width: int) -> str:
    """Map a bar width (0-100, impact relative to the top theme) to a tier."""
    if width >= 66:
        return "high"
    if width >= 33:
        return "med"
    return "low"


def render_theme_card(rank: int, theme: dict, max_impact: float) -> str:
    """Render one theme as a ranked card with an impact-proportional bar."""
    name = esc(theme["theme"])
    category = theme.get("category", "other")
    color = CATEGORY_COLORS.get(category, "#7f848e")
    count = theme["count"]
    impact = theme.get("impact_score", 0)
    description = esc(theme.get("description", ""))
    width = int((impact / max_impact) * 100) if max_impact else 0
    level = impact_level(width)                 # high / med / low vs the top theme
    level_label = {"high": "High", "med": "Med", "low": "Low"}[level]
    impact_title = (f"Impact score {impact:g}: review count weighted by each "
                    "reviewer's playtime and helpful votes, shown relative to the "
                    "top theme on this side.")
    examples_html = "".join(render_example(e) for e in theme.get("examples", []))
    return (
        '<div class="card">'
        '<div class="card-head">'
        f'<span class="rank">#{rank}</span>'
        f'<span class="theme-name">{name}</span>'
        f'<span class="pill" style="background:{color}">{esc(category)}</span>'
        f'<span class="count">{count} reviews</span>'
        f'<span class="impact-chip impact-{level}" title="{esc(impact_title)}">'
        f'{level_label} impact</span>'
        '</div>'
        f'<div class="bar-track"><div class="bar-fill" data-w="{width}" style="width:0%;background:{color}"></div></div>'
        f'<p class="description">{description}</p>'
        f'<div class="examples">{examples_html}</div>'
        '</div>'
    )


def _section(heading: str, items: list, max_impact: float) -> str:
    """A heading plus its ranked theme cards."""
    body = "".join(render_theme_card(i + 1, t, max_impact) for i, t in enumerate(items))
    return f"<h2>{esc(heading)}</h2>{body}"


def render_side(records: list, mode: str) -> str:
    """
    Render one polarity's content: ranked theme sections plus a per-side note for
    constructive reviews that matched no theme.
    """
    real = [t for t in records if t["theme"] not in (UNCLEAR_LABEL, NOISE_LABEL)]
    unclear = next((t for t in records if t["theme"] == UNCLEAR_LABEL), None)
    max_impact = max((t.get("impact_score", 0) for t in real), default=1) or 1

    if not real:
        html_out = '<p class="empty">Not enough reviews on this side to surface themes.</p>'
    elif mode == "positive":
        features = [t for t in real if t.get("kind", "feature") != "emotional"]
        emotional = [t for t in real if t.get("kind") == "emotional"]
        html_out = _section("Double Down - ranked by impact (playtime + helpful votes)",
                            features, max_impact)
        if emotional:
            html_out += _section("Player sentiment - emotional, not directly actionable",
                                 emotional, max_impact)
    else:
        html_out = _section("Fix These - ranked by impact (playtime + helpful votes)",
                            real, max_impact)

    if unclear and unclear.get("count"):
        total_side = sum(t["count"] for t in records) or 1
        pct = round(unclear["count"] / total_side * 100)
        html_out += (f'<div class="unclear">{unclear["count"]} reviews ({pct}%) were '
                     "constructive but did not match a specific theme.</div>")
    return html_out


def render_overview(neg: list, pos: list, sentiment_totals: dict, total_reviews: int, noise_count: int = 0) -> str:
    """Overall sentiment bar + category distribution (across both sides)."""
    sent = {"positive": 0, "negative": 0, "neutral": 0}
    for k, v in (sentiment_totals or {}).items():
        sent[k] = sent.get(k, 0) + v
    sent_total = sum(sent.values())

    cats = {}
    for rec in list(neg) + list(pos):
        if rec["theme"] in (NOISE_LABEL, UNCLEAR_LABEL):
            continue
        cats[rec["category"]] = cats.get(rec["category"], 0) + rec["count"]
    cat_total = sum(cats.values()) or 1

    segments, legend = "", ""
    for key in ("positive", "negative", "neutral"):
        val = sent.get(key, 0)
        pct = (val / sent_total * 100) if sent_total else 0
        if val:
            segments += f'<div class="seg" data-w="{pct:.1f}" style="width:0%;background:{SENTIMENT_COLORS[key]}"></div>'
        legend += (f'<span class="legend-item"><span class="dot" '
                   f'style="background:{SENTIMENT_COLORS[key]}"></span>{key} {pct:.0f}% '
                   f'<span class="cat-count">({val})</span></span>')

    cat_rows = ""
    for category, c in sorted(cats.items(), key=lambda kv: kv[1], reverse=True):
        pct = c / cat_total * 100
        color = CATEGORY_COLORS.get(category, "#7f848e")
        cat_rows += (
            '<div class="cat-row">'
            f'<span class="cat-label">{esc(category)}</span>'
            f'<span class="cat-track"><span class="cat-fill" data-w="{pct:.1f}" style="width:0%;background:{color}"></span></span>'
            f'<span class="cat-num">{pct:.0f}% <span class="cat-count">({c})</span></span>'
            '</div>'
        )

    filtered_line = ""
    if noise_count:
        npct = round(noise_count / total_reviews * 100) if total_reviews else 0
        filtered_line = (f'<div class="ov-note">Filtered as low-signal noise: '
                         f'<strong>{noise_count}</strong> reviews ({npct}% of all).</div>')

    return (
        '<div class="overview">'
        '<div class="ov-title">Sentiment</div>'
        f'<div class="sentiment-bar">{segments}</div>'
        f'<div class="legend">{legend}</div>'
        '<div class="ov-title">By category <span class="ov-sub">share of categorized reviews</span></div>'
        f'<div class="cat-list">{cat_rows}</div>'
        f'{filtered_line}'
        '</div>'
    )


def build_html(analysis: dict, title: str, refresh_state: dict = None) -> str:
    """Assemble the full report from a combined analysis dict (see analyze_both)."""
    neg = analysis.get("negative", [])
    pos = analysis.get("positive", [])
    noise = analysis.get("noise", {}) or {}
    sentiment_totals = analysis.get("sentiment_totals", {})
    total_reviews = analysis.get("total_reviews", 0)
    generated = date.today().strftime("%B %d, %Y")

    overview_html = render_overview(neg, pos, sentiment_totals, total_reviews, noise.get("count", 0))
    fix_html = render_side(neg, "negative")
    love_html = render_side(pos, "positive")

    noise_html = ""
    if noise.get("count"):
        pct = round(noise["count"] / total_reviews * 100) if total_reviews else 0
        noise_html = (f'<div class="unclear">{noise["count"]} reviews ({pct}%) were filtered '
                      "out as noise (jokes, one-liners, off-topic rants, and spam) before "
                      "theming.</div>")

    refresh_html = render_refresh(refresh_state)

    # Header markup (plain strings, brace-safe).
    header_html = (
        '<header>'
        '<a class="brand" href="/">SteamSifter</a>'
        '<div class="titlerow">'
        '<div class="titleblock">'
        f'<h1>{esc(title)}</h1>'
        f'<div class="meta">Review analysis &middot; {total_reviews} reviews &middot; '
        f'Generated {generated}</div>'
        f'{refresh_html}'
        '</div>'
        '<div class="navsearch">'
        '<input id="navq" type="text" placeholder="Analyze another game..." autocomplete="off">'
        '<div id="navresults" class="navresults" style="display:none"></div>'
        '</div>'
        '</div>'
        '</header>'
    )

    nav_search = NAV_SEARCH_JS
    toggle_js = TOGGLE_JS

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SteamSifter Report: {esc(title)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Motiva Sans", -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #1b2838; color: #c7d5e0; }}
  header {{ background: #171a21; color: #fff; padding: 24px 32px; border-bottom: 1px solid #0e1620; }}
  header a.brand {{ font-size: 14px; letter-spacing: 2px; color: #66c0f4; text-transform: uppercase; text-decoration: none; display: inline-block; margin-bottom: 12px; }}
  header a.brand:hover {{ color: #8fd0fb; }}
  .titlerow {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
  header h1 {{ margin: 0 0 4px; font-size: 26px; color: #fff; font-weight: 500; }}
  header .meta {{ color: #8f98a0; font-size: 14px; }}
  .navsearch {{ position: relative; flex: 1; max-width: 320px; }}
  .navsearch input {{ width: 100%; padding: 8px 12px; border-radius: 3px; border: 1px solid #2a3a4d; background: #316282; color: #fff; font-size: 13px; }}
  .navsearch input::placeholder {{ color: #c6dbec; }}
  .navresults {{ position: absolute; left: 0; right: 0; top: 38px; background: #16202d; border: 1px solid #2a3a4d; border-radius: 3px; overflow: hidden; z-index: 5; }}
  .navresult {{ display: flex; align-items: center; gap: 10px; padding: 8px 10px; cursor: pointer; }}
  .navresult:hover {{ background: #1f3346; }}
  .navresult img {{ width: 46px; height: 18px; object-fit: cover; border-radius: 2px; background: #0e1620; }}
  .navresult span {{ font-size: 13px; color: #c7d5e0; }}
  main {{ max-width: 880px; margin: 0 auto; padding: 28px 20px 60px; }}
  h2 {{ font-size: 18px; margin: 24px 0 12px; color: #fff; font-weight: 500; }}
  .toggle-bar {{ position: relative; display: inline-flex; background: #16202d; border: 1px solid #2a3a4d; border-radius: 4px; padding: 3px; margin: 8px 0 4px; }}
  .toggle-slider {{ position: absolute; top: 3px; bottom: 3px; left: 3px; width: calc(50% - 3px); border-radius: 3px; background: linear-gradient(to bottom, #1a9fff, #0a78c2); transition: transform .25s ease; z-index: 0; }}
  .toggle-btn {{ position: relative; z-index: 1; flex: 1 1 0; min-width: 130px; text-align: center; border: 0; background: transparent; padding: 8px 18px; border-radius: 3px; font-size: 14px; font-weight: 600; color: #8f98a0; cursor: pointer; transition: color .25s ease; }}
  .toggle-btn.active {{ color: #fff; }}
  .card {{ background: #16202d; border: 1px solid #233040; border-radius: 4px; padding: 18px 20px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.2); }}
  .card-head {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .rank {{ font-weight: 700; color: #66c0f4; font-size: 14px; }}
  .theme-name {{ font-weight: 700; font-size: 16px; color: #fff; }}
  .pill {{ color: #0e1620; font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 3px; text-transform: lowercase; }}
  .count {{ margin-left: auto; font-size: 13px; color: #8f98a0; font-weight: 600; }}
  .bar-track {{ background: #0e1620; border-radius: 3px; height: 8px; margin: 12px 0 10px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 3px; transition: width .8s cubic-bezier(.25,.8,.25,1); }}
  .description {{ font-size: 14px; color: #acb2b8; margin: 0 0 12px; }}
  .example {{ border-left: 3px solid #2a475e; padding: 4px 0 4px 12px; margin: 8px 0; }}
  .quote {{ font-style: italic; color: #c7d5e0; font-size: 13px; }}
  .badges {{ display: block; margin-top: 4px; }}
  .badge {{ display: inline-block; font-size: 11px; background: #2a3f5a; color: #c7d5e0; border-radius: 3px; padding: 1px 7px; margin-right: 6px; }}
  .unclear {{ background: #16202d; border: 1px solid #2a475e; border-left: 3px solid #66c0f4; border-radius: 3px; padding: 14px 16px; font-size: 14px; color: #8f98a0; margin-top: 10px; }}
  .empty {{ color: #8f98a0; font-style: italic; }}
  .overview {{ background: #16202d; border: 1px solid #2a3a4d; border-radius: 4px; padding: 18px 20px; margin-bottom: 8px; }}
  .ov-title {{ font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: #66c0f4; margin: 10px 0 8px; font-weight: 700; }}
  .ov-title:first-child {{ margin-top: 0; }}
  .sentiment-bar {{ display: flex; height: 14px; border-radius: 3px; overflow: hidden; background: #0e1620; }}
  .seg {{ height: 100%; transition: width .8s cubic-bezier(.25,.8,.25,1); }}
  .legend {{ margin: 8px 0 4px; font-size: 12px; color: #8f98a0; }}
  .legend-item {{ margin-right: 14px; text-transform: lowercase; }}
  .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }}
  .cat-row {{ display: flex; align-items: center; gap: 8px; margin: 5px 0; font-size: 12px; }}
  .cat-label {{ width: 95px; color: #acb2b8; text-transform: lowercase; }}
  .cat-track {{ flex: 1; background: #0e1620; border-radius: 3px; height: 8px; overflow: hidden; }}
  .cat-fill {{ display: block; height: 100%; border-radius: 3px; transition: width .8s cubic-bezier(.25,.8,.25,1); }}
  .cat-num {{ width: 34px; text-align: right; color: #8f98a0; font-weight: 600; }}
  .quote-link {{ color: #c7d5e0; text-decoration: none; }}
  .quote-link:hover {{ color: #66c0f4; text-decoration: underline; }}
  .source {{ display: inline-block; font-size: 11px; color: #66c0f4; text-decoration: none; margin-left: 2px; }}
  .source:hover {{ text-decoration: underline; }}
  .ov-sub {{ font-weight: 400; text-transform: none; letter-spacing: 0; color: #8f98a0; font-size: 11px; }}
  .ov-note {{ margin-top: 12px; font-size: 12px; color: #8f98a0; }}
  .cat-count {{ color: #6b7785; font-weight: 400; }}
  .cat-num {{ min-width: 70px; }}
  .impact-help {{ font-size: 12px; color: #8f98a0; max-width: 680px; margin: 2px 0 16px; line-height: 1.5; }}
  .impact-chip {{ font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 3px; margin-left: 8px; cursor: help; white-space: nowrap; }}
  .impact-high {{ background: #66c0f4; color: #0e1620; }}
  .impact-med {{ background: #39698a; color: #dfeaf2; }}
  .impact-low {{ background: #2a3f5a; color: #9fb0c0; }}
  .thumb {{ display: inline-flex; align-items: center; gap: 4px; }}
  .thumb.up {{ background: #1a3a2a; color: #a4d4a2; }}
  .thumb.down {{ background: #3a1f24; color: #e08f96; }}
  .thumb svg {{ display: inline-block; }}
  .thumb-dn svg {{ transform: rotate(180deg); }}
  .refresh {{ display: inline-block; margin-top: 10px; font-size: 13px; font-weight: 600; color: #66c0f4; text-decoration: none; background: #16202d; border: 1px solid #2a475e; border-radius: 4px; padding: 6px 12px; }}
  .refresh:hover {{ background: #1f3346; color: #8fd0fb; }}
  .refresh.disabled {{ color: #6b7785; border-color: #233040; background: transparent; cursor: not-allowed; }}
  .refresh.admin {{ border-color: #66c0f4; }}
  .admtag {{ font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: #0e1620; background: #66c0f4; border-radius: 3px; padding: 1px 5px; margin-left: 6px; }}
</style>
</head>
<body>
  {header_html}
  <main>
    <h2>Overview</h2>
    {overview_html}
    <div class="toggle-bar">
      <div class="toggle-slider" id="toggle-slider"></div>
      <button id="btn-fix" class="toggle-btn active" onclick="showSide('fix')">Fix These</button>
      <button id="btn-love" class="toggle-btn" onclick="showSide('love')">Double Down</button>
    </div>
    <div class="impact-help">Themes are ranked by <strong>impact</strong>: how many reviews raised each one, weighted by the reviewer's playtime and helpful votes. A few experienced, upvoted players outweigh many drive-by reviews. The bar and the High/Med/Low chip are relative to the top theme on each side.</div>
    <div id="side-fix">{fix_html}</div>
    <div id="side-love" style="display:none">{love_html}</div>
    <h2>Low-signal reviews</h2>
    {noise_html}
  </main>
  {nav_search}
  {toggle_js}
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Render a report from an analysis JSON file.")
    parser.add_argument("analysis_file", nargs="?", default="data/analysis_730.json",
                        help="Path to an analysis JSON file (from the pipeline)")
    parser.add_argument("--title", default="Counter-Strike 2 (App 730)",
                        help="Game title shown in the report header")
    parser.add_argument("--out", default="steamsifter_report.html", help="Output HTML path")
    args = parser.parse_args()

    with open(args.analysis_file, encoding="utf-8") as f:
        analysis = json.load(f)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(build_html(analysis, args.title))

    print(f"Report written to {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
