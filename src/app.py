"""
app.py

The SteamSifter web app (Flask).

Routes:
  GET /              -> home page with a game search bar
  GET /api/search    -> JSON game suggestions for the search box (by name)
  GET /analyze       -> runs the cached pipeline for an app id and serves the report

Run it:
    python src/app.py
Then open http://127.0.0.1:5000 in your browser.
"""

from flask import Flask, request, jsonify, Response

from search import search_games
from pipeline import get_records
from report import build_html


app = Flask(__name__)


# The home page. Plain string (not an f-string) so the JS braces are safe.
HOME_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SteamSifter</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
         background: #1b2838; color: #c7d5e0; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; }
  .wrap { width: 100%; max-width: 560px; padding: 24px; text-align: center; }
  .brand { color: #66c0f4; letter-spacing: 3px; text-transform: uppercase; font-size: 14px; }
  h1 { margin: 8px 0 4px; font-size: 34px; color: #fff; }
  p.sub { color: #8f98a0; margin: 0 0 26px; }
  .search { position: relative; }
  input[type=text] { width: 100%; padding: 14px 16px; font-size: 16px; border-radius: 8px;
                     border: 1px solid #2a475e; background: #16202d; color: #fff; }
  .toggle { margin: 14px 0 0; font-size: 14px; color: #8f98a0; }
  .toggle label { margin: 0 10px; cursor: pointer; }
  .results { position: absolute; left: 0; right: 0; top: 56px; background: #16202d;
             border: 1px solid #2a475e; border-radius: 8px; overflow: hidden; z-index: 5; text-align: left; }
  .result { display: flex; align-items: center; gap: 12px; padding: 10px 12px; cursor: pointer; }
  .result:hover { background: #1f3346; }
  .result img { width: 60px; height: 23px; object-fit: cover; border-radius: 3px; background: #0e1620; }
  .result span { font-size: 14px; color: #c7d5e0; }
  .overlay { display: none; position: fixed; inset: 0; background: rgba(11,18,26,.92);
             align-items: center; justify-content: center; flex-direction: column; z-index: 10; }
  .overlay.show { display: flex; }
  .spinner { width: 38px; height: 38px; border: 4px solid #2a475e; border-top-color: #66c0f4;
             border-radius: 50%; animation: spin 1s linear infinite; margin-bottom: 16px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .hint { color: #66758a; font-size: 12px; margin-top: 18px; }
</style>
</head>
<body>
  <div class="wrap">
    <div class="brand">SteamSifter</div>
    <h1>Analyze any game's reviews</h1>
    <p class="sub">Search a Steam game to see what players want fixed or what they love.</p>
    <div class="search">
      <input id="q" type="text" placeholder="Search a game, e.g. Counter-Strike" autocomplete="off" autofocus>
      <div id="results" class="results" style="display:none"></div>
    </div>
    <div class="toggle">
      <label><input type="radio" name="mode" value="negative" checked> What to fix (negative)</label>
      <label><input type="radio" name="mode" value="positive"> What players love (positive)</label>
    </div>
    <div class="hint">First-time analysis of a game can take up to a minute. Repeat lookups are instant.</div>
  </div>

  <div id="overlay" class="overlay">
    <div class="spinner"></div>
    <div id="overlay-text">Analyzing...</div>
  </div>

<script>
  const input = document.getElementById('q');
  const results = document.getElementById('results');
  let timer = null;

  // Debounced search as the user types.
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { results.style.display = 'none'; return; }
    timer = setTimeout(() => fetchSuggestions(q), 250);
  });

  async function fetchSuggestions(q) {
    try {
      const resp = await fetch('/api/search?q=' + encodeURIComponent(q));
      const games = await resp.json();
      renderSuggestions(games);
    } catch (e) {
      results.style.display = 'none';
    }
  }

  function renderSuggestions(games) {
    if (!games.length) { results.style.display = 'none'; return; }
    results.innerHTML = '';
    games.forEach(g => {
      const row = document.createElement('div');
      row.className = 'result';
      row.innerHTML =
        (g.image ? '<img src="' + g.image + '">' : '<img>') +
        '<span>' + g.name + '</span>';
      row.onclick = () => analyze(g.appid, g.name);
      results.appendChild(row);
    });
    results.style.display = 'block';
  }

  function analyze(appid, name) {
    const mode = document.querySelector('input[name=mode]:checked').value;
    document.getElementById('overlay-text').textContent =
      'Analyzing ' + name + '... first run can take up to a minute.';
    document.getElementById('overlay').classList.add('show');
    window.location = '/analyze?appid=' + appid +
      '&type=' + mode + '&title=' + encodeURIComponent(name);
  }
</script>
</body>
</html>"""


@app.route("/")
def home():
    """Serve the search home page."""
    return HOME_PAGE


@app.route("/api/search")
def api_search():
    """Return JSON game suggestions for the search box."""
    return jsonify(search_games(request.args.get("q", "")))


@app.route("/analyze")
def analyze():
    """Run the cached pipeline for a game and serve its report."""
    appid = request.args.get("appid")
    if not appid:
        return "Missing appid", 400

    review_type = request.args.get("type", "negative")
    if review_type not in ("negative", "positive", "all"):
        review_type = "negative"

    title = request.args.get("title") or f"App {appid}"
    mode = "positive" if review_type == "positive" else "negative"

    # get_records uses the cache, so repeat lookups are instant and free.
    records = get_records(appid, review_type)
    html_doc = build_html(records, title, mode)
    return Response(html_doc, mimetype="text/html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
