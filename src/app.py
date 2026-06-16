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

import threading
import uuid

from flask import Flask, request, jsonify, Response

from search import search_games
from pipeline import get_analysis
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
         background: linear-gradient(to bottom, #1b2838, #16202d) fixed; color: #c7d5e0; display: flex; min-height: 100vh;
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
    window.location = '/analyzing?appid=' + appid + '&title=' + encodeURIComponent(name);
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

    title = request.args.get("title") or f"App {appid}"

    # get_analysis uses the cache, so repeat lookups are instant and free.
    analysis = get_analysis(appid)
    return Response(build_html(analysis, title), mimetype="text/html")


# ----------------------------------------------------------------------------
# Background analysis jobs (so the page can show a real progress bar)
# ----------------------------------------------------------------------------

# In-memory job registry. Fine for single-process dev; a real deployment would
# use a shared store (parked with the concurrency work).
JOBS = {}


def _run_job(job_id, appid):
    """Run the analysis in a background thread, recording progress in JOBS."""
    def progress(pct, msg):
        JOBS[job_id]["percent"] = pct
        JOBS[job_id]["message"] = msg
    try:
        get_analysis(appid, progress=progress)   # writes the cache as it goes
        JOBS[job_id].update(percent=100, message="Done", done=True)
    except Exception as e:
        JOBS[job_id].update(error=str(e), done=True)


ANALYZING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analyzing... | SteamSifter</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
         background: linear-gradient(to bottom, #1b2838, #16202d) fixed; color: #c7d5e0; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; }
  .box { width: 100%; max-width: 520px; padding: 24px; text-align: center; }
  .brand { color: #66c0f4; letter-spacing: 3px; text-transform: uppercase; font-size: 13px; }
  h1 { margin: 10px 0 24px; font-size: 24px; color: #fff; }
  .track { background: #16202d; border: 1px solid #2a475e; border-radius: 8px; height: 22px; overflow: hidden; }
  .fill { height: 100%; width: 0%; background: linear-gradient(90deg,#1a9fff,#66c0f4); transition: width .4s ease; }
  .pct { font-size: 28px; font-weight: 700; color: #fff; margin: 16px 0 4px; }
  .msg { color: #8f98a0; font-size: 14px; min-height: 20px; }
  .err { color: #e06c75; font-size: 14px; margin-top: 14px; }
</style>
</head>
<body>
  <div class="box">
    <div class="brand">SteamSifter</div>
    <h1 id="title">Analyzing...</h1>
    <div class="track"><div id="fill" class="fill"></div></div>
    <div id="pct" class="pct">0%</div>
    <div id="msg" class="msg">Starting...</div>
    <div id="err" class="err"></div>
  </div>
<script>
  const params = new URLSearchParams(window.location.search);
  const appid = params.get('appid');
  const title = params.get('title') || '';
  if (title) document.getElementById('title').textContent = 'Analyzing ' + title;

  const fill = document.getElementById('fill');
  const pct = document.getElementById('pct');
  const msg = document.getElementById('msg');

  function setBar(p, m) {
    fill.style.width = p + '%';
    pct.textContent = p + '%';
    if (m) msg.textContent = m;
  }
  function showError(e) {
    document.getElementById('err').textContent =
      'Something went wrong: ' + e + '. Go back and try again.';
    msg.textContent = '';
  }

  let jobId = null;
  fetch('/start?appid=' + encodeURIComponent(appid))
    .then(r => r.json())
    .then(d => { if (d.error) { showError(d.error); return; } jobId = d.job; poll(); })
    .catch(() => showError('Could not start analysis.'));

  function poll() {
    fetch('/progress?job=' + jobId)
      .then(r => r.json())
      .then(d => {
        if (d.error) { showError(d.error); return; }
        setBar(d.percent || 0, d.message);
        if (d.done) {
          window.location = '/analyze?appid=' + encodeURIComponent(appid) +
            '&title=' + encodeURIComponent(title);
        } else {
          setTimeout(poll, 800);
        }
      })
      .catch(() => setTimeout(poll, 1500));
  }
</script>
</body>
</html>"""


@app.route("/analyzing")
def analyzing():
    """A progress page that starts the analysis and polls until it is ready."""
    return ANALYZING_PAGE


@app.route("/start")
def start():
    """Kick off a background analysis job; returns a job id to poll."""
    appid = request.args.get("appid")
    if not appid:
        return jsonify({"error": "missing appid"}), 400
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"percent": 0, "message": "Starting...", "done": False, "error": None}
    threading.Thread(target=_run_job, args=(job_id, appid), daemon=True).start()
    return jsonify({"job": job_id})


@app.route("/progress")
def progress_route():
    """Return the current progress for a job id."""
    job = request.args.get("job", "")
    return jsonify(JOBS.get(job, {"error": "unknown job", "done": True}))


if __name__ == "__main__":
    # threaded=True lets the progress endpoint respond while a job runs.
    app.run(debug=True, threaded=True, port=5000)
