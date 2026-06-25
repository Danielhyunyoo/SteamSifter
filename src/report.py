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
    "gameplay": "#818cf8",   # indigo: distinct from the blue impact chips
    "cheating": "#c678dd",
    "community": "#56b6c2",
    "monetization": "#e5c07b",
    "content": "#d977b8",
    "ui_ux": "#abb2bf",
    "praise": "#98c379",
    "other": "#7f848e",
}

# Colors for the sentiment overview bar.
SENTIMENT_COLORS = {"positive": "#98c379", "negative": "#e06c75", "neutral": "#abb2bf"}

UNCLEAR_LABEL = "unclear"   # constructive reviews that matched no theme
NOISE_LABEL = "noise"       # reviews filtered out as low-signal

# Absolute base URL for OpenGraph image/url tags (override per environment).
SITE_URL = os.environ.get("SITE_URL", "https://steamsifter.com").rstrip("/")
CARD_VERSION = "2"   # bump to refresh all social cards after a card redesign

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
        const img = document.createElement('img');
        if (g.image) img.src = g.image;
        const span = document.createElement('span');
        span.textContent = g.name;
        row.appendChild(img);
        row.appendChild(span);
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


FILTER_BAR_HTML = """
<div class="filterbar" id="filterbar">
  <span class="filterbar-label">Filter reviews</span>
  <label>Recommendation
    <select id="f-rec">
      <option value="all">All</option>
      <option value="yes">Recommended</option>
      <option value="no">Not recommended</option>
    </select>
  </label>
  <label>Min playtime
    <select id="f-pt">
      <option value="0">Any</option>
      <option value="10">10h+</option>
      <option value="100">100h+</option>
      <option value="500">500h+</option>
    </select>
  </label>
  <label>Language
    <select id="f-lang">
      <option value="all">All</option>
      <option value="en">English only</option>
    </select>
  </label>
  <span class="filtered-count" id="filteredCount"></span>
</div>
"""


FILTER_JS = """
<script>
(function () {
  var dataEl = document.getElementById('reviewdata');
  var bar = document.getElementById('filterbar');
  if (!dataEl || !bar) return;
  var D; try { D = JSON.parse(dataEl.textContent); } catch (e) { return; }
  var reviews = D.reviews, catColors = D.cats, tl = D.tl;
  var SC = { positive: '#98c379', negative: '#e06c75', neutral: '#abb2bf' };

  function weight(r) { return 1 + Math.log10(1 + r.pt) + Math.log10(1 + r.hv); }
  function byImp(a, b) { return b.impact - a.impact; }
  function filters() {
    return {
      rec: document.getElementById('f-rec').value,
      pt: parseFloat(document.getElementById('f-pt').value) || 0,
      lang: document.getElementById('f-lang').value,
    };
  }
  function match(r, f) {
    if (f.rec === 'yes' && r.vu !== 1) return false;
    if (f.rec === 'no' && r.vu !== 0) return false;
    if (r.pt < f.pt) return false;
    if (f.lang === 'en' && r.en !== 1) return false;
    return true;
  }
  function setStat(k, v) { var e = document.querySelector('[data-stat=\"' + k + '\"]'); if (e) e.textContent = v; }

  function recompute() {
    var f = filters();
    var fr = reviews.filter(function (r) { return match(r, f); });

    var st = { positive: 0, negative: 0, neutral: 0 };
    fr.forEach(function (r) { if (st[r.se] !== undefined) st[r.se]++; });
    var tot = st.positive + st.negative + st.neutral;

    var cats = {}, themes = {};
    fr.forEach(function (r) {
      if (r.co === 1 && r.th) {
        cats[r.ca] = (cats[r.ca] || 0) + 1;
        var k = r.sd + '|' + r.th;
        var t = themes[k] || (themes[k] = { count: 0, impact: 0, side: r.sd, name: r.th });
        t.count++; t.impact += weight(r);
      }
    });
    var noise = fr.filter(function (r) { return r.co === 0; }).length;

    // Scoreboard
    setStat('reviews', fr.length);
    setStat('positive', (tot ? Math.round(st.positive / tot * 100) : 0) + '%');
    setStat('negative', (tot ? Math.round(st.negative / tot * 100) : 0) + '%');
    var neg = [], pos = [];
    Object.keys(themes).forEach(function (k) { (themes[k].side === 'pos' ? pos : neg).push(themes[k]); });
    setStat('themes', neg.length + pos.length);
    neg.sort(byImp); pos.sort(byImp);
    setStat('topfix', neg.length ? neg[0].name : '\u2014');
    setStat('toppraise', pos.length ? pos[0].name : '\u2014');

    // Sentiment bar + legend
    var sb = document.getElementById('sentimentBar'), sl = document.getElementById('sentimentLegend');
    if (sb) { var h = ''; ['positive','negative','neutral'].forEach(function (k) {
      var v = st[k], p = tot ? v / tot * 100 : 0;
      if (v) h += '<div class=\"seg\" style=\"width:' + p.toFixed(1) + '%;background:' + SC[k] + '\"></div>'; }); sb.innerHTML = h; }
    if (sl) { var lh = ''; ['positive','negative','neutral'].forEach(function (k) {
      var v = st[k], p = tot ? Math.round(v / tot * 100) : 0;
      lh += '<span class=\"legend-item\"><span class=\"dot\" style=\"background:' + SC[k] + '\"></span>' + k + ' ' + p + '% <span class=\"cat-count\">(' + v + ')</span></span>'; }); sl.innerHTML = lh; }

    // Donut
    var dc = window.__charts && window.__charts.donut;
    if (dc) {
      var ce = Object.keys(cats).map(function (c) { return [c, cats[c]]; }).sort(function (a, b) { return b[1] - a[1]; });
      dc.data.labels = ce.map(function (e) { return e[0]; });
      dc.data.datasets[0].data = ce.map(function (e) { return e[1]; });
      dc.data.datasets[0].backgroundColor = ce.map(function (e) { return catColors[e[0]] || '#7f848e'; });
      dc.update('none');
    }

    // Trend (re-bucket over the original date buckets)
    var tc = window.__charts && window.__charts.trend;
    if (tc && tl && tl.keys.length) {
      var idx = {}; tl.keys.forEach(function (k, i) { idx[k] = i; });
      var P = tl.keys.map(function () { return 0; }), N = P.slice(), U = P.slice(), V = P.slice();
      fr.forEach(function (r) {
        if (!r.dt) return;
        var key = tl.gran === 'month' ? r.dt.slice(0, 7) : r.dt;
        var i = idx[key]; if (i === undefined) return;
        if (r.se === 'positive') P[i]++; else if (r.se === 'negative') N[i]++; else U[i]++;
        V[i]++;
      });
      var ds = tc.data.datasets;
      if (ds[0]) ds[0].data = P; if (ds[1]) ds[1].data = N; if (ds[2]) ds[2].data = U; if (ds[3]) ds[3].data = V;
      tc.update('none');
    }

    // Theme cards: update + reorder by impact, hide empties.
    ['side-fix', 'side-love'].forEach(function (sideId) {
      var side = sideId === 'side-fix' ? 'neg' : 'pos';
      var cont = document.getElementById(sideId);
      if (!cont) return;
      var cards = Array.prototype.slice.call(cont.querySelectorAll('.card'));
      var maxI = 0;
      cards.forEach(function (c) { var t = themes[side + '|' + c.getAttribute('data-theme')]; if (t && t.impact > maxI) maxI = t.impact; });
      maxI = maxI || 1;
      var ranked = [];
      cards.forEach(function (c) {
        var t = themes[side + '|' + c.getAttribute('data-theme')];
        if (!t || t.count === 0) { c.style.display = 'none'; return; }
        c.style.display = '';
        var w = Math.round(t.impact / maxI * 100);
        var cn = c.querySelector('.count'); if (cn) cn.textContent = t.count + ' reviews';
        var bf = c.querySelector('.bar-fill'); if (bf) { bf.style.width = w + '%'; bf.setAttribute('data-w', w); }
        var ch = c.querySelector('.impact-chip');
        if (ch) { var lv = w >= 66 ? 'high' : (w >= 33 ? 'med' : 'low');
          ch.className = 'impact-chip impact-' + lv;
          ch.textContent = (lv === 'high' ? 'High' : lv === 'med' ? 'Med' : 'Low') + ' impact'; }
        ranked.push({ c: c, imp: t.impact });
      });
      ranked.sort(function (a, b) { return b.imp - a.imp; });
      ranked.forEach(function (o, i) { cont.appendChild(o.c); var rk = o.c.querySelector('.rank'); if (rk) rk.textContent = '#' + (i + 1); });
      var uncl = cont.querySelector('.unclear'); if (uncl) cont.appendChild(uncl);
    });

    // Noise note + filtered count
    var nn = document.getElementById('noiseNote');
    if (nn) nn.innerHTML = 'Filtered as low-signal noise: <strong>' + noise + '</strong> reviews.';
    var fc = document.getElementById('filteredCount');
    if (fc) fc.textContent = fr.length + ' of ' + reviews.length + ' reviews';

    // Example quotes: show those matching the filter, cap 2 per theme.
    document.querySelectorAll('.example').forEach(function (ex) {
      var vu = ex.getAttribute('data-vu'), pt = parseFloat(ex.getAttribute('data-pt')) || 0, en = ex.getAttribute('data-en');
      var ok = true;
      if (f.rec === 'yes' && vu !== '1') ok = false;
      if (f.rec === 'no' && vu !== '0') ok = false;
      if (pt < f.pt) ok = false;
      if (f.lang === 'en' && en !== '1') ok = false;
      ex.style.display = ok ? '' : 'none';
    });
    document.querySelectorAll('.examples').forEach(function (box) {
      var shown = 0;
      Array.prototype.slice.call(box.querySelectorAll('.example')).forEach(function (ex) {
        if (ex.style.display !== 'none') { shown++; if (shown > 2) ex.style.display = 'none'; }
      });
    });
  }

  ['f-rec', 'f-pt', 'f-lang'].forEach(function (id) {
    var e = document.getElementById(id); if (e) e.addEventListener('change', recompute);
  });
})();
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
    needed = state.get("reviews_needed", 0)
    return ('<span class="refresh disabled" title="Unlocks once the game gains '
            f'more reviews">Re-analyze after {needed:,} more reviews</span>')


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

    # English translation line for foreign-language reviews (when present).
    translation = example.get("translation")
    trans_html = (f'<div class="translation">EN: &ldquo;{esc(translation)}&rdquo;</div>'
                  if translation else '')

    # Reviewer avatar + name, so two identical quotes read as distinct people.
    avatar = example.get("author_avatar")
    aname = example.get("author_name")
    if avatar or aname:
        av = (f'<img class="avatar" src="{esc(avatar)}" alt="" loading="lazy">'
              if avatar else '')
        nm = f'<span class="aname">{esc(aname)}</span>' if aname else ''
        author_html = f'<div class="author">{av}{nm}</div>'
    else:
        author_html = ''

    return (
        f'<div class="example" data-vu="{1 if voted is True else 0}" '
        f'data-pt="{hours:g}" data-hv="{helpful}" data-en="{example.get("en", 1)}">'
        f'{author_html}'
        f'{quote_html}'
        f'{trans_html}'
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
        f'<div class="card" data-theme="{name}">'
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
        html_out = _section("Praise - ranked by impact (playtime + helpful votes)",
                            features, max_impact)
        if emotional:
            html_out += _section("Player sentiment - emotional, not directly actionable",
                                 emotional, max_impact)
    else:
        html_out = _section("Issues - ranked by impact (playtime + helpful votes)",
                            real, max_impact)

    if unclear and unclear.get("count"):
        total_side = sum(t["count"] for t in records) or 1
        pct = round(unclear["count"] / total_side * 100)
        html_out += (f'<div class="unclear">{unclear["count"]} reviews ({pct}%) were '
                     "constructive but did not match a specific theme.</div>")
    return html_out


def render_overview(sentiment_totals: dict, total_reviews: int, noise_count: int = 0) -> str:
    """Overall sentiment bar plus a category donut (drawn by Chart.js)."""
    sent = {"positive": 0, "negative": 0, "neutral": 0}
    for k, v in (sentiment_totals or {}).items():
        sent[k] = sent.get(k, 0) + v
    sent_total = sum(sent.values())

    segments, legend = "", ""
    for key in ("positive", "negative", "neutral"):
        val = sent.get(key, 0)
        pct = (val / sent_total * 100) if sent_total else 0
        if val:
            segments += f'<div class="seg" data-w="{pct:.1f}" style="width:0%;background:{SENTIMENT_COLORS[key]}"></div>'
        legend += (f'<span class="legend-item"><span class="dot" '
                   f'style="background:{SENTIMENT_COLORS[key]}"></span>{key} {pct:.0f}% '
                   f'<span class="cat-count">({val})</span></span>')

    filtered_line = ""
    if noise_count:
        npct = round(noise_count / total_reviews * 100) if total_reviews else 0
        filtered_line = (f'<div class="ov-note" id="noiseNote">Filtered as low-signal noise: '
                         f'<strong>{noise_count}</strong> reviews ({npct}% of all).</div>')

    return (
        '<div class="overview">'
        '<div class="ov-title">Sentiment</div>'
        f'<div class="sentiment-bar" id="sentimentBar">{segments}</div>'
        f'<div class="legend" id="sentimentLegend">{legend}</div>'
        '<div class="ov-title">By category <span class="ov-sub">share of categorized reviews</span></div>'
        '<div class="donut-wrap"><canvas id="catDonut"></canvas></div>'
        '<div class="donut-hint">Tip: click a category in the legend to show or hide it.</div>'
        f'{filtered_line}'
        '</div>'
    )



# ----------------------------------------------------------------------------
# Summary scoreboard (inline SVG, no dependencies) + Chart.js data helpers
# ----------------------------------------------------------------------------

def _icon(paths: str, color: str) -> str:
    """Wrap Feather-style stroke paths in a sized, colored SVG."""
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">{paths}</svg>')


ICON_REVIEWS = _icon('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>', "#66c0f4")
ICON_THEMES = _icon('<polygon points="12 2 2 7 12 12 22 7 12 2"/>'
                    '<polyline points="2 17 12 22 22 17"/>'
                    '<polyline points="2 12 12 17 22 12"/>', "#66c0f4")
ICON_UP = _icon('<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/>'
                '<polyline points="17 6 23 6 23 12"/>', "#98c379")
ICON_DOWN = _icon('<polyline points="23 18 13.5 8.5 8.5 13.5 1 6"/>'
                  '<polyline points="17 18 23 18 23 12"/>', "#e06c75")
ICON_FIX = _icon('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77'
                 'a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 '
                 '7.94-7.94l-3.76 3.76z"/>', "#e06c75")
ICON_PRAISE = _icon('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 '
                    '17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>', "#98c379")


def category_counts(neg: list, pos: list) -> dict:
    """Total review count per category across both sides (excluding noise/unclear)."""
    cats = {}
    for rec in list(neg) + list(pos):
        if rec["theme"] in (NOISE_LABEL, UNCLEAR_LABEL):
            continue
        cats[rec["category"]] = cats.get(rec["category"], 0) + rec["count"]
    return cats


def render_scoreboard(analysis: dict) -> str:
    """A band of at-a-glance stat cards shown above the overview."""
    sent = analysis.get("sentiment_totals", {}) or {}
    pos = sent.get("positive", 0)
    neg = sent.get("negative", 0)
    neu = sent.get("neutral", 0)
    sent_total = pos + neg + neu
    total_reviews = analysis.get("total_reviews", 0)
    pct_pos = round(pos / sent_total * 100) if sent_total else 0
    pct_neg = round(neg / sent_total * 100) if sent_total else 0

    def real(recs):
        return [t for t in recs if t["theme"] not in (NOISE_LABEL, UNCLEAR_LABEL)]

    negs = real(analysis.get("negative", []))
    poss = real(analysis.get("positive", []))
    themes_count = len(negs) + len(poss)
    top_fix = esc(negs[0]["theme"]) if negs else "&mdash;"
    top_praise = esc(poss[0]["theme"]) if poss else "&mdash;"

    def card(icon, label, value, key, cls=""):
        return (f'<div class="stat">{icon}<div class="stat-body">'
                f'<div class="stat-label">{label}</div>'
                f'<div class="stat-value {cls}" data-stat="{key}">{value}</div></div></div>')

    return (
        '<div class="scoreboard">'
        + card(ICON_REVIEWS, "Reviews analyzed", total_reviews, "reviews")
        + card(ICON_UP, "Positive", f"{pct_pos}%", "positive", "good")
        + card(ICON_DOWN, "Negative", f"{pct_neg}%", "negative", "bad")
        + card(ICON_THEMES, "Themes found", themes_count, "themes")
        + card(ICON_FIX, "Top fix", top_fix, "topfix", "small bad")
        + card(ICON_PRAISE, "Top praise", top_praise, "toppraise", "small good")
        + '</div>'
    )


def trend_labels(timeline: dict) -> list:
    """Human-readable x-axis labels for the sentiment trend chart."""
    pts = timeline.get("points", [])
    gran = timeline.get("granularity", "day")
    out = []
    for p in pts:
        lab = p.get("label", "")
        try:
            parts = [int(x) for x in lab.split("-")]
            if gran == "month":
                out.append(date(parts[0], parts[1], 1).strftime("%b %Y"))
            else:
                out.append(date(parts[0], parts[1], parts[2]).strftime("%b %d"))
        except (ValueError, IndexError):
            out.append(lab)
    return out


# Chart.js (pinned) and the chart setup script (plain string: JS braces are safe).
CHARTJS_CDN = ('<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/'
               '4.4.1/chart.umd.min.js"></script>')

CHARTS_JS = """
<script>
(function () {
  var el = document.getElementById('chartdata');
  if (!el || typeof Chart === 'undefined') return;
  var data;
  try { data = JSON.parse(el.textContent); } catch (e) { return; }

  var narrow = window.innerWidth < 640;   // stack legends/titles on small screens
  var donutChart = null, trendChart = null;
  Chart.defaults.color = '#8f98a0';
  Chart.defaults.font.family = '-apple-system, Segoe UI, Roboto, sans-serif';

  // Fixed-position trend tooltip: render an HTML box pinned to the top of the
  // chart wrapper so it stays at a constant height and never clips the lines.
  function externalTrendTip(context) {
    var tip = document.getElementById('trendTip');
    if (!tip) return;
    var model = context.tooltip;
    if (!model || model.opacity === 0) { tip.style.opacity = '0'; return; }
    var title = (model.title && model.title[0]) || '';
    var rows = (model.body || []).map(function (b, i) {
      var lc = model.labelColors[i] || {};
      var color = lc.borderColor || lc.backgroundColor || '#8f98a0';
      return '<div class="tt-row"><span class="tt-dot" style="background:' + color +
             '"></span>' + b.lines.join(' ') + '</div>';
    }).join('');
    tip.innerHTML = '<div class="tt-title">' + title + '</div>' + rows;
    // Follow the cursor horizontally; vertical position stays fixed at the top.
    var canvas = context.chart.canvas;
    var wrapW = tip.parentNode.clientWidth;
    var half = tip.offsetWidth / 2;
    var x = canvas.offsetLeft + model.caretX;
    x = Math.max(half + 4, Math.min(x, wrapW - half - 4));
    tip.style.left = x + 'px';
    tip.style.opacity = '1';
  }

  // Vertical crosshair line at the hovered x, through all three series.
  var trendCrosshair = {
    id: 'trendCrosshair',
    afterDatasetsDraw: function (chart) {
      var tt = chart.tooltip;
      if (!tt || tt.opacity === 0) return;
      var ctx = chart.ctx, x = tt.caretX;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x, chart.chartArea.top);
      ctx.lineTo(x, chart.chartArea.bottom);
      ctx.lineWidth = 1;
      ctx.strokeStyle = 'rgba(199,213,224,0.35)';
      ctx.stroke();
      ctx.restore();
    }
  };

  // Category donut.
  var d = data.donut, dc = document.getElementById('catDonut');
  if (dc && d && d.values.length) {
    donutChart = new Chart(dc, {
      type: 'doughnut',
      data: { labels: d.labels,
              datasets: [{ data: d.values, backgroundColor: d.colors,
                           borderColor: '#16202d', borderWidth: 2 }] },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: '62%',
        plugins: {
          legend: { position: narrow ? 'bottom' : 'right', labels: { boxWidth: 12, padding: 10 } },
          tooltip: { callbacks: { label: function (c) {
            var total = c.dataset.data.reduce(function (a, b) { return a + b; }, 0);
            var pct = total ? Math.round(c.parsed / total * 100) : 0;
            return ' ' + c.label + ': ' + c.parsed + ' (' + pct + '%)';
          } } }
        }
      }
    });
  }

  // Sentiment over time (with review volume).
  var t = data.trend, tc = document.getElementById('trendChart');
  if (tc && t && t.labels.length > 1) {
    var mk = function (label, color, arr) {
      return { label: label, data: arr, borderColor: color,
               backgroundColor: color + '33', fill: true, tension: 0.3,
               pointRadius: 2, borderWidth: 2, yAxisID: 'y', order: 0 };
    };
    var datasets = [
      mk('Positive', '#98c379', t.positive),
      mk('Negative', '#e06c75', t.negative),
      mk('Neutral', '#abb2bf', t.neutral)
    ];
    if (t.volume && t.volume.length) {
      datasets.push({ type: 'bar', label: 'Reviews', data: t.volume,
        backgroundColor: 'rgba(102,192,244,0.16)', borderColor: '#66c0f4', borderWidth: 0,
        yAxisID: 'yVol', order: 5 });
    }
    trendChart = new Chart(tc, {
      type: 'line',
      data: { labels: t.labels, datasets: datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        layout: { padding: { top: 16 } },
        scales: {
          x: { grid: { color: '#233040' } },
          y: { grid: { color: '#233040' }, beginAtZero: true, grace: '12%', ticks: { precision: 0 },
               title: { display: !narrow, text: 'by sentiment' } },
          yVol: { position: 'right', grid: { display: false }, beginAtZero: true, grace: '12%',
                  ticks: { precision: 0 }, title: { display: !narrow, text: 'total reviews' } }
        },
        plugins: {
          legend: { position: 'bottom', labels: { boxWidth: 12, padding: 12 } },
          tooltip: { enabled: false, external: externalTrendTip }
        }
      },
      plugins: [trendCrosshair]
    });
  }

  // Charts are drawn in the dark theme; swap them to light for print, restore after.
  function setChartTheme(forPrint) {
    var gridC = forPrint ? '#d0d7de' : '#233040';
    var textC = forPrint ? '#555f6a' : '#8f98a0';
    if (donutChart) {
      donutChart.data.datasets[0].borderColor = forPrint ? '#ffffff' : '#16202d';
      if (donutChart.options.plugins.legend) donutChart.options.plugins.legend.labels.color = textC;
      donutChart.update('none');
    }
    if (trendChart) {
      var sc = trendChart.options.scales;
      ['x', 'y', 'yVol'].forEach(function (k) {
        if (!sc[k]) return;
        if (sc[k].grid) sc[k].grid.color = gridC;
        if (sc[k].ticks) sc[k].ticks.color = textC;
        if (sc[k].title) sc[k].title.color = textC;
      });
      if (trendChart.data.datasets[2]) {
        trendChart.data.datasets[2].borderColor = forPrint ? '#8a929c' : '#abb2bf';
      }
      if (trendChart.options.plugins.legend) trendChart.options.plugins.legend.labels.color = textC;
      trendChart.update('none');
    }
  }
  window.addEventListener('beforeprint', function () { setChartTheme(true); });
  window.addEventListener('afterprint', function () { setChartTheme(false); });
  window.__charts = { donut: donutChart, trend: trendChart };
})();
</script>
"""


def steam_rating(pos, neg, total):
    """
    A Steam-style overall rating label from the positive/negative split, plus a
    background and text color. Returns ("", "", "") when there is nothing to rate.
    """
    denom = pos + neg
    if denom < 1:
        return "", "", ""
    ratio = pos / denom
    big = total >= 200                       # only claim "Overwhelmingly" with volume
    if ratio >= 0.95:
        label = "Overwhelmingly Positive" if big else "Very Positive"
    elif ratio >= 0.80:
        label = "Very Positive"
    elif ratio >= 0.70:
        label = "Mostly Positive"
    elif ratio >= 0.40:
        label = "Mixed"
    elif ratio >= 0.20:
        label = "Mostly Negative"
    else:
        label = "Overwhelmingly Negative" if big else "Very Negative"
    if ratio >= 0.70:
        return label, "#66c0f4", "#0e1620"   # Steam blue, dark text
    if ratio >= 0.40:
        return label, "#b9a06a", "#211b0e"   # tan/gold for Mixed
    return label, "#c15b5b", "#1f0e0e"       # muted red for Negative


def relative_time(epoch):
    """A human 'x ago' string from an epoch timestamp, or None when missing."""
    import time
    if not epoch:
        return None
    secs = max(0, time.time() - epoch)
    if secs < 90:
        return "just now"
    for size, name in ((86400 * 365, "year"), (86400 * 30, "month"),
                       (86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size:
            n = int(secs // size)
            return f"{n} {name}{'s' if n != 1 else ''} ago"
    return "just now"


# Quality-of-life client behaviors: count-up scoreboard numbers, a copy-link
# button, and a "/" shortcut to focus the in-report search. Plain string (not an
# f-string), injected as a value, so its braces and regex backslashes are safe.
QOL_JS = """<script>
(function () {
  function countUp(el) {
    var raw = el.textContent.trim();
    var m = raw.match(/^(\\d[\\d,]*)(\\D*)$/);   // leading number + optional suffix
    if (!m) return;
    var target = parseInt(m[1].replace(/,/g, ''), 10);
    var suffix = m[2] || '';
    if (!isFinite(target)) return;
    var dur = 700, start = null;
    function step(ts) {
      if (start === null) start = ts;
      var p = Math.min(1, (ts - start) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * eased).toLocaleString() + suffix;
      if (p < 1) requestAnimationFrame(step);
      else el.textContent = target.toLocaleString() + suffix;
    }
    requestAnimationFrame(step);
  }
  document.querySelectorAll('.stat-value[data-stat]').forEach(function (el) {
    if (/^\\d/.test(el.textContent.trim())) countUp(el);
  });

  window.copyReportLink = function (btn) {
    var url = (btn && btn.getAttribute('data-share')) || window.location.href;
    navigator.clipboard.writeText(url).then(function () {
      var prev = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(function () { btn.textContent = prev; }, 1500);
    }).catch(function () {});
  };

  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && !/^(INPUT|TEXTAREA)$/.test((e.target.tagName || ''))) {
      var q = document.getElementById('navq');
      if (q) { e.preventDefault(); q.focus(); }
    }
  });
})();
</script>"""


def build_html(analysis: dict, title: str, refresh_state: dict = None) -> str:
    """Assemble the full report from a combined analysis dict (see analyze_both)."""
    neg = analysis.get("negative", [])
    pos = analysis.get("positive", [])
    noise = analysis.get("noise", {}) or {}
    sentiment_totals = analysis.get("sentiment_totals", {})
    total_reviews = analysis.get("total_reviews", 0)
    generated = date.today().strftime("%B %d, %Y")
    analyzed_rel = relative_time(analysis.get("cached_at")) or "just now"
    rating_label, rating_bg, rating_fg = steam_rating(
        sentiment_totals.get("positive", 0), sentiment_totals.get("negative", 0), total_reviews)
    rating_badge = (f'<span class="rating-badge" style="background:{rating_bg};color:{rating_fg}">'
                    f'{esc(rating_label)}</span>') if rating_label else ''

    overview_html = render_overview(sentiment_totals, total_reviews, noise.get("count", 0))
    scoreboard_html = render_scoreboard(analysis)

    # Chart.js data: a category donut and an optional sentiment-over-time trend.
    cats_sorted = sorted(category_counts(neg, pos).items(), key=lambda kv: kv[1], reverse=True)
    donut = {
        "labels": [c for c, _ in cats_sorted],
        "values": [v for _, v in cats_sorted],
        "colors": [CATEGORY_COLORS.get(c, "#7f848e") for c, _ in cats_sorted],
    }
    timeline = analysis.get("sentiment_timeline") or {}
    pts = timeline.get("points", [])
    trend = {
        "labels": trend_labels(timeline),
        "positive": [p.get("positive", 0) for p in pts],
        "negative": [p.get("negative", 0) for p in pts],
        "neutral": [p.get("neutral", 0) for p in pts],
        "volume": [p.get("positive", 0) + p.get("negative", 0) + p.get("neutral", 0) for p in pts],
    }
    # Escape "</" so a theme name can never break out of the <script> tag.
    chartdata_json = json.dumps({"donut": donut, "trend": trend},
                                ensure_ascii=False).replace("</", "<\\/")
    chartdata_script = f'<script id="chartdata" type="application/json">{chartdata_json}</script>'

    trend_section = ""
    if len(pts) > 1:
        grouping = "by month" if timeline.get("granularity") == "month" else "by day"
        trend_section = (
            '<h2>Sentiment over time</h2>'
            f'<div class="trend-sub">Sentiment of the {total_reviews} most recent reviews, '
            f'grouped {grouping}.</div>'
            '<div class="trend-wrap"><canvas id="trendChart"></canvas>'
            '<div id="trendTip" class="trend-tip"></div></div>'
        )
    fix_html = render_side(neg, "negative")
    love_html = render_side(pos, "positive")

    noise_html = ""
    if noise.get("count"):
        pct = round(noise["count"] / total_reviews * 100) if total_reviews else 0
        noise_html = (f'<div class="unclear">{noise["count"]} reviews ({pct}%) were filtered '
                      "out as noise (jokes, one-liners, off-topic rants, and spam) before "
                      "theming.</div>")

    refresh_html = render_refresh(refresh_state)

    # Game banner straight from Steam's CDN, built from the app id (available via
    # refresh_state on the web app). onerror hides it for titles with no header
    # image, so a missing banner never leaves a broken-image icon.
    appid = (refresh_state or {}).get("appid", "")
    # Cache version = analysis save time + card-template version, so the share
    # link changes on either a re-analysis OR a card redesign (forces Discord et
    # al. to re-scrape instead of showing a stale cached embed).
    cache_ver = f"{int(analysis.get('cached_at') or 0)}.{CARD_VERSION}"
    share_url = (f"{SITE_URL}/analyze?appid={appid}&title={quote(title)}&v={cache_ver}"
                 if appid else "")
    thumb_html = (
        f'<img class="gamethumb" alt="" '
        f'src="https://cdn.cloudflare.steamstatic.com/steam/apps/{esc(appid)}/header.jpg" '
        'onerror="this.style.display=\'none\'">'
    ) if appid else ''

    # OpenGraph / Twitter card so shared links unfurl into a rich preview. Only
    # emitted on the web path (appid present); image is served by the /og route.
    og_tags = ""
    if appid:
        top_fix_name = next((x["theme"] for x in neg
                             if x["theme"] not in (NOISE_LABEL, UNCLEAR_LABEL)), "")
        top_praise_name = next((x["theme"] for x in pos
                               if x["theme"] not in (NOISE_LABEL, UNCLEAR_LABEL)), "")
        bits = []
        if top_praise_name:
            bits.append(f"Top praise: {top_praise_name}")
        if top_fix_name:
            bits.append(f"Top fix: {top_fix_name}")
        detail = " \u00b7 ".join(bits) or "AI review analysis"
        og_desc = f"{total_reviews} reviews analyzed. {detail}."
        og_title = f"{title} - SteamSifter review analysis"
        og_img = f"{SITE_URL}/og/{appid}.png?t={quote(title)}&v={cache_ver}"
        og_url = share_url
        og_tags = (
            f'<meta name="description" content="{esc(og_desc)}">'
            '<meta property="og:type" content="website">'
            '<meta property="og:site_name" content="SteamSifter">'
            f'<meta property="og:title" content="{esc(og_title)}">'
            f'<meta property="og:description" content="{esc(og_desc)}">'
            f'<meta property="og:image" content="{esc(og_img)}">'
            '<meta property="og:image:width" content="1200">'
            '<meta property="og:image:height" content="630">'
            f'<meta property="og:url" content="{esc(og_url)}">'
            '<meta name="twitter:card" content="summary_large_image">'
            f'<meta name="twitter:title" content="{esc(og_title)}">'
            f'<meta name="twitter:description" content="{esc(og_desc)}">'
            f'<meta name="twitter:image" content="{esc(og_img)}">'
        )

    # Header markup (plain strings, brace-safe).
    header_html = (
        '<header>'
        '<a class="brand" href="/">SteamSifter</a>'
        '<div class="titlerow">'
        '<div class="titlelead">'
        f'{thumb_html}'
        '<div class="titleblock">'
        f'<h1>{esc(title)}</h1>'
        f'{rating_badge}'
        f'<div class="meta" title="Generated {generated}">Review analysis &middot; '
        f'{total_reviews} reviews &middot; Analyzed {analyzed_rel}</div>'
        f'{refresh_html}'
        '<button class="printbtn" onclick="window.print()" title="Print or save this report as a PDF">Print / Save PDF</button>'
        f'<button class="printbtn copybtn" data-share="{esc(share_url)}" onclick="copyReportLink(this)" title="Copy a shareable link to this report">Copy link</button>'
        '</div>'
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
    charts_js = CHARTS_JS
    qol_js = QOL_JS
    chartjs_cdn = CHARTJS_CDN

    # Live review filters: ship the compact per-review array so the dashboard can
    # recompute under filters in the browser. Old cached reports lack it, so the
    # filter bar is simply omitted (no breakage, no re-analysis needed).
    filter_reviews = analysis.get("reviews")
    if filter_reviews:
        tl_payload = {"gran": timeline.get("granularity", "day"),
                      "keys": [p.get("label", "") for p in pts],
                      "labels": trend_labels(timeline)}
        fdata_json = json.dumps({"reviews": filter_reviews, "cats": dict(CATEGORY_COLORS),
                                 "tl": tl_payload}, ensure_ascii=False).replace("</", "<\\/")
        filter_data_script = f'<script id="reviewdata" type="application/json">{fdata_json}</script>'
        filter_bar = FILTER_BAR_HTML
        filter_js_block = FILTER_JS
    else:
        filter_data_script = filter_bar = filter_js_block = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2366c0f4' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='7'/><line x1='21' y1='21' x2='16.65' y2='16.65'/></svg>">
<title>SteamSifter Report: {esc(title)}</title>
{og_tags}
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Motiva Sans", -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #1b2838; color: #c7d5e0; }}
  header {{ background: #171a21; color: #fff; padding: 24px 32px; border-bottom: 1px solid #0e1620; }}
  header a.brand {{ font-size: 14px; letter-spacing: 2px; color: #66c0f4; text-transform: uppercase; text-decoration: none; display: inline-block; margin-bottom: 12px; }}
  header a.brand:hover {{ color: #8fd0fb; }}
  .titlerow {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
  .titlelead {{ display: flex; align-items: center; gap: 16px; min-width: 0; }}
  .gamethumb {{ width: 120px; height: auto; border-radius: 4px; border: 1px solid #2a3a4d; display: block; flex-shrink: 0; }}
  .rating-badge {{ display: inline-block; padding: 3px 10px; border-radius: 4px; font-size: 12px; font-weight: 600; margin: 0 0 8px; }}
  .copybtn {{ margin-left: 8px; }}
  header h1 {{ margin: 0 0 4px; font-size: 26px; color: #fff; font-weight: 500; }}
  header .meta {{ color: #8f98a0; font-size: 14px; }}
  .navsearch {{ position: relative; flex: 1; max-width: 320px; }}
  .navsearch input {{ width: 100%; padding: 8px 12px; border-radius: 3px; border: 1px solid #2a3a4d; background: #316282; color: #fff; font-size: 13px; }}
  .navsearch input::placeholder {{ color: #c6dbec; }}
  .navresults {{ position: absolute; left: 0; right: 0; top: 38px; background: #16202d; border: 1px solid #2a3a4d; border-radius: 3px; overflow-y: auto; max-height: 340px; z-index: 5; }}
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
  .card {{ background: rgba(22, 32, 45, 0.72); border: 1px solid #233040; border-radius: 4px; padding: 18px 20px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.2); }}
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
  .translation {{ font-size: 12px; color: #8f98a0; margin: 3px 0 0; }}
  .author {{ display: flex; align-items: center; gap: 6px; margin-bottom: 5px; }}
  .avatar {{ width: 20px; height: 20px; border-radius: 3px; object-fit: cover; background: #0e1620; }}
  .aname {{ font-size: 12px; color: #8f98a0; font-weight: 600; }}
  .badges {{ display: block; margin-top: 4px; }}
  .badge {{ display: inline-block; font-size: 11px; background: #2a3f5a; color: #c7d5e0; border-radius: 3px; padding: 1px 7px; margin-right: 6px; }}
  .unclear {{ background: #16202d; border: 1px solid #2a475e; border-left: 3px solid #66c0f4; border-radius: 3px; padding: 14px 16px; font-size: 14px; color: #8f98a0; margin-top: 10px; }}
  .empty {{ color: #8f98a0; font-style: italic; }}
  .overview {{ background: rgba(22, 32, 45, 0.72); border: 1px solid #2a3a4d; border-radius: 4px; padding: 18px 20px; margin-bottom: 8px; }}
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
  .printbtn {{ display: inline-block; margin: 10px 0 0 8px; font-size: 13px; font-weight: 600; color: #66c0f4; background: #16202d; border: 1px solid #2a475e; border-radius: 4px; padding: 6px 12px; cursor: pointer; }}
  .printbtn:hover {{ background: #1f3346; color: #8fd0fb; }}
  .scoreboard {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 20px; }}
  .stat {{ flex: 1 1 150px; background: rgba(22, 32, 45, 0.72); border: 1px solid #2a3a4d; border-radius: 6px; padding: 12px 14px; display: flex; gap: 11px; align-items: center; }}
  .stat svg {{ width: 22px; height: 22px; flex: none; }}
  .stat-body {{ min-width: 0; }}
  .stat-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .5px; color: #8f98a0; }}
  .stat-value {{ font-size: 20px; font-weight: 700; color: #fff; line-height: 1.2; }}
  .stat-value.small {{ font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .stat-value.good {{ color: #98c379; }}
  .stat-value.bad {{ color: #e06c75; }}
  .donut-wrap {{ position: relative; height: 240px; margin: 6px 0 2px; }}
  .donut-hint {{ font-size: 11px; color: #8f98a0; margin-top: 6px; }}
  .trend-wrap {{ position: relative; height: 300px; background: rgba(22, 32, 45, 0.72); border: 1px solid #2a3a4d; border-radius: 6px; padding: 12px; }}
  .trend-sub {{ font-size: 12px; color: #8f98a0; margin: 2px 0 10px; }}
  .trend-tip {{ position: absolute; top: auto; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%); background: rgba(14,22,32,0.78); border: 1px solid #2a3a4d; border-radius: 6px; padding: 7px 10px; font-size: 12px; color: #c7d5e0; pointer-events: none; opacity: 0; transition: opacity .08s; white-space: nowrap; z-index: 10; }}
  .trend-tip .tt-title {{ font-weight: 700; color: #fff; margin-bottom: 4px; }}
  .trend-tip .tt-row {{ display: flex; align-items: center; gap: 6px; line-height: 1.4; }}
  .trend-tip .tt-dot {{ width: 9px; height: 9px; border-radius: 2px; flex: none; }}
  .filterbar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 12px; background: rgba(22,32,45,0.72); border: 1px solid #2a3a4d; border-radius: 6px; padding: 10px 14px; margin: 0 0 14px; font-size: 12px; }}
  .filterbar-label {{ color: #66c0f4; font-weight: 700; text-transform: uppercase; letter-spacing: .5px; }}
  .filterbar label {{ color: #8f98a0; display: inline-flex; align-items: center; gap: 6px; }}
  .filterbar select {{ background: #0e1620; color: #c7d5e0; border: 1px solid #2a3a4d; border-radius: 4px; padding: 4px 6px; font-size: 12px; }}
  .filtered-count {{ margin-left: auto; color: #8f98a0; }}
  .examples .example:nth-child(n+3) {{ display: none; }}
  @media (max-width: 640px) {{
    header {{ padding: 16px 18px; }}
    .titlerow {{ flex-direction: column; align-items: stretch; gap: 12px; }}
    .gamethumb {{ width: 96px; }}
    header h1 {{ font-size: 22px; }}
    .navsearch {{ max-width: none; }}
    main {{ padding: 20px 14px 48px; }}
    h2 {{ font-size: 16px; }}
    .toggle-bar {{ display: flex; width: 100%; }}
    .toggle-btn {{ min-width: 0; flex: 1 1 0; padding: 8px 10px; }}
    .scoreboard {{ gap: 8px; }}
    .stat {{ flex: 1 1 calc(50% - 8px); padding: 10px 12px; gap: 9px; }}
    .stat svg {{ width: 20px; height: 20px; }}
    .stat-value {{ font-size: 18px; }}
    .donut-wrap {{ height: 300px; }}
    .trend-wrap {{ height: 300px; padding: 10px; }}
    .card {{ padding: 14px; }}
    .impact-chip {{ margin-left: 0; }}
    .refresh {{ display: block; text-align: center; }}
  }}
  @media print {{
    @page {{ margin: 1.2cm; }}
    /* Light theme for paper/PDF: dark text on white, keep the color accents. */
    body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; background: #ffffff !important; color: #1d2733 !important; }}
    header {{ background: #ffffff !important; padding: 0 0 10px !important; border-bottom: 1px solid #d0d7de !important; }}
    header a.brand {{ color: #1a6dc4 !important; }}
    header h1 {{ color: #111111 !important; }}
    main {{ max-width: 100%; padding: 10px 0; }}
    h2 {{ color: #111111 !important; break-after: avoid; }}
    .navsearch, .refresh, .printbtn, .donut-hint, .trend-tip, .toggle-bar, .filterbar {{ display: none !important; }}
    #side-fix, #side-love {{ display: block !important; }}
    .overview, .card, .trend-wrap, .stat {{ background: #ffffff !important; border: 1px solid #d0d7de !important; box-shadow: none !important; break-inside: avoid; }}
    .example {{ break-inside: avoid; border-left-color: #d0d7de !important; }}
    .meta, .count, .legend, .legend-item, .ov-note, .ov-sub, .trend-sub, .impact-help, .aname, .translation, .cat-label, .cat-num, .cat-count, .stat-label {{ color: #555f6a !important; }}
    .theme-name, .stat-value {{ color: #111111 !important; }}
    .description, .quote, .quote-link {{ color: #2b333c !important; }}
    .ov-title, .source {{ color: #1a6dc4 !important; }}
    .badge {{ background: #eef1f4 !important; color: #333333 !important; }}
    .thumb.up {{ background: #e6f4ea !important; color: #1a7f37 !important; }}
    .thumb.down {{ background: #fbe9ea !important; color: #b3261e !important; }}
    .unclear {{ background: #f6f8fa !important; border-color: #d0d7de !important; color: #444444 !important; }}
    .bar-track, .cat-track, .sentiment-bar {{ background: #e8edf2 !important; }}
  }}
</style>
</head>
<body>
  {header_html}
  <main>
    {scoreboard_html}
    {filter_bar}
    <h2>Overview</h2>
    {overview_html}
    {trend_section}
    <div class="toggle-bar">
      <div class="toggle-slider" id="toggle-slider"></div>
      <button id="btn-fix" class="toggle-btn active" onclick="showSide('fix')">Issues</button>
      <button id="btn-love" class="toggle-btn" onclick="showSide('love')">Praise</button>
    </div>
    <div class="impact-help">Themes are ranked by <strong>impact</strong>: how many reviews raised each one, weighted by the reviewer's playtime and helpful votes. A few experienced, upvoted players outweigh many drive-by reviews. The bar and the High/Med/Low chip are relative to the top theme on each side.</div>
    <div id="side-fix">{fix_html}</div>
    <div id="side-love" style="display:none">{love_html}</div>
    <h2>Low-signal reviews</h2>
    {noise_html}
  </main>
  {chartdata_script}
  {filter_data_script}
  {nav_search}
  {toggle_js}
  {chartjs_cdn}
  {charts_js}
  {qol_js}
  {filter_js_block}
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
