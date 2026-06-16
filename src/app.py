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
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from search import search_games
from pipeline import get_analysis
from report import build_html


app = Flask(__name__)

# Behind a host's proxy (e.g. Render), trust X-Forwarded-* so the rate limiter
# sees real client IPs rather than the proxy's.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Per-visitor rate limits to protect the shared API key. In-memory storage is
# fine for the single-worker deploy; a multi-worker setup would need a shared
# store (parked with the concurrency work).
limiter = Limiter(get_remote_address, app=app)


# The home page. Plain string (not an f-string) so the JS braces are safe.
HOME_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SteamSifter</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; min-height: 100vh;
         display: flex; flex-direction: column;
         background: linear-gradient(to bottom, #1b2838, #16202d) fixed; color: #c7d5e0; }
  .hero { flex: 1; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; padding: 24px; }
  .hero-inner { width: 100%; max-width: 560px; }
  .brand { color: #66c0f4; letter-spacing: 3px; text-transform: uppercase; font-size: 14px; }
  h1 { margin: 8px 0 4px; font-size: 34px; color: #fff; }
  p.sub { color: #8f98a0; margin: 0 0 26px; }
  .search { position: relative; }
  input[type=text] { width: 100%; padding: 14px 16px; font-size: 16px; border-radius: 8px; border: 1px solid #2a475e; background: #16202d; color: #fff; }
  .results { position: absolute; left: 0; right: 0; top: 56px; background: #16202d; border: 1px solid #2a475e; border-radius: 8px; overflow: hidden; z-index: 5; text-align: left; }
  .result { display: flex; align-items: center; gap: 12px; padding: 10px 12px; cursor: pointer; }
  .result:hover { background: #1f3346; }
  .result img { width: 60px; height: 23px; object-fit: cover; border-radius: 3px; background: #0e1620; }
  .result span { font-size: 14px; color: #c7d5e0; }
  .hint { color: #66758a; font-size: 12px; margin-top: 18px; }
  .site-footer { background: #171a21; border-top: 1px solid #0e1620; padding: 18px 24px; text-align: center; }
  .footer-links { display: flex; gap: 20px; justify-content: center; align-items: center; flex-wrap: wrap; }
  .footer-links a { color: #8f98a0; text-decoration: none; font-size: 14px; display: inline-flex; align-items: center; }
  .footer-links a:hover { color: #66c0f4; }
  .footer-links svg { width: 20px; height: 20px; fill: currentColor; display: block; }
  .disclaimer { margin: 12px auto 0; color: #5a6675; font-size: 11px; line-height: 1.5; max-width: 640px; }
</style>
</head>
<body>
  <div class="hero">
    <div class="hero-inner">
      <div class="brand">SteamSifter</div>
      <h1>Analyze any game's reviews</h1>
      <p class="sub">Search a Steam game to see what players want fixed or what they love.</p>
      <div class="search">
        <input id="q" type="text" placeholder="Search a game, e.g. Counter-Strike" autocomplete="off" autofocus>
        <div id="results" class="results" style="display:none"></div>
      </div>
      <div class="hint">First-time analysis of a game can take up to a minute. Repeat lookups are instant.</div>
    </div>
  </div>

  <footer class="site-footer">
    <div class="footer-links">
      <a href="/about">About SteamSifter</a>
      <a href="https://github.com/Danielhyunyoo/SteamSifter" target="_blank" rel="noopener" title="GitHub" aria-label="GitHub"><svg viewBox="0 0 24 24"><path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"/></svg></a>
      <a href="https://steamcommunity.com/profiles/76561198990353371/" target="_blank" rel="noopener" title="Steam" aria-label="Steam"><svg viewBox="0 0 24 24"><path d="M11.979 0C5.678 0 .511 4.86.022 11.037l6.432 2.658c.545-.371 1.203-.589 1.912-.589.063 0 .125.004.188.006l2.861-4.142V8.91c0-2.495 2.028-4.524 4.524-4.524s4.524 2.029 4.524 4.524c0 2.494-2.028 4.524-4.524 4.524h-.105l-4.076 2.911c0 .052.004.105.004.158 0 1.875-1.515 3.396-3.39 3.396-1.635 0-3.016-1.173-3.331-2.727L.436 15.27C1.862 20.307 6.486 24 11.979 24c6.627 0 11.999-5.373 11.999-12S18.605 0 11.979 0zM7.54 18.21l-1.473-.61c.262.543.714.999 1.314 1.25 1.297.539 2.793-.076 3.332-1.375.263-.63.264-1.319.005-1.949s-.75-1.121-1.377-1.383c-.624-.26-1.29-.249-1.878-.03l1.523.63c.956.4 1.409 1.5 1.009 2.455-.397.957-1.497 1.41-2.454 1.012H7.54zm11.415-9.303c0-1.662-1.353-3.015-3.015-3.015-1.665 0-3.015 1.353-3.015 3.015 0 1.665 1.35 3.015 3.015 3.015 1.663 0 3.015-1.35 3.015-3.015zm-5.273-.005c0-1.252 1.013-2.266 2.265-2.266 1.249 0 2.266 1.014 2.266 2.266 0 1.251-1.017 2.265-2.266 2.265-1.253 0-2.265-1.014-2.265-2.265z"/></svg></a>
      <a href="https://www.linkedin.com/in/danielhyunwooyoo/" target="_blank" rel="noopener" title="LinkedIn" aria-label="LinkedIn"><svg viewBox="0 0 24 24"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg></a>
    </div>
    <div class="disclaimer">SteamSifter is an independent project and is not affiliated with, endorsed by, or sponsored by Valve or Steam. "Steam" is a trademark of Valve Corporation.</div>
  </footer>

<script>
  const input = document.getElementById('q');
  const results = document.getElementById('results');
  let timer = null;
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
    } catch (e) { results.style.display = 'none'; }
  }
  function renderSuggestions(games) {
    if (!games.length) { results.style.display = 'none'; return; }
    results.innerHTML = '';
    games.forEach(g => {
      const row = document.createElement('div');
      row.className = 'result';
      row.innerHTML = (g.image ? '<img src="' + g.image + '">' : '<img>') + '<span>' + g.name + '</span>';
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
@limiter.limit("40 per hour")
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
@limiter.limit("10 per hour; 3 per minute")
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


ABOUT_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About | SteamSifter</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #1b2838; color: #c7d5e0; }
  header { background: #171a21; padding: 22px 32px; border-bottom: 1px solid #0e1620; }
  header a.brand { color: #66c0f4; letter-spacing: 2px; text-transform: uppercase; font-size: 14px; text-decoration: none; }
  header a.brand:hover { color: #8fd0fb; }
  main { max-width: 760px; margin: 0 auto; padding: 30px 22px 60px; }
  h1 { color: #fff; font-size: 28px; margin: 0 0 6px; }
  .bio { color: #8f98a0; font-size: 15px; margin: 0 0 8px; }
  .profiles { margin: 14px 0 26px; display: flex; gap: 16px; flex-wrap: wrap; }
  .profiles a { color: #66c0f4; text-decoration: none; font-size: 14px; }
  .profiles a:hover { text-decoration: underline; }
  h2 { color: #fff; font-size: 18px; font-weight: 500; margin: 26px 0 10px; }
  p, li { color: #c7d5e0; font-size: 14px; line-height: 1.6; }
  ul, ol { padding-left: 20px; }
  .note { background: #16202d; border: 1px solid #2a3a4d; border-left: 3px solid #66c0f4; border-radius: 4px; padding: 12px 14px; color: #8f98a0; font-size: 13px; }
</style>
</head>
<body>
  <header><a class="brand" href="/">SteamSifter</a></header>
  <main>
    <h1>About SteamSifter</h1>
    <p class="bio">Daniel Yoo, 4th Year Computer Science Student currently enrolled at the University of Texas at Dallas.</p>
    <div class="profiles">
      <a href="https://github.com/Danielhyunyoo/SteamSifter" target="_blank" rel="noopener">GitHub repo</a>
      <a href="https://steamcommunity.com/profiles/76561198990353371/" target="_blank" rel="noopener">Steam profile</a>
      <a href="https://www.linkedin.com/in/danielhyunwooyoo/" target="_blank" rel="noopener">LinkedIn</a>
    </div>

    <h2>The Problem</h2>
    <p>Steam games can collect up to thousands of reviews, mixing genuine bug reports, feature praise, jokes, off-topic rants, and review-bombing. Reading all of that manually to answer "What should we fix?" and "What do our players want more of?" is a slow and inconsistent task.</p>
    <p>SteamSifter takes a game, pulls its reviews automatically, filters the noise, and returns two ranked dashboards: issues to fix (by impact) and praised features to double down on (by frequency and sentiment).</p>

    <h2>How It Works</h2>
    <ol>
      <li><strong>Ingest:</strong> fetches reviews directly from Steam's free public review API. Each review carries useful metadata: positive/negative flag, helpful-vote count, and the reviewer's playtime.</li>
      <li><strong>Filter for signal:</strong> a relevance classifier separates constructive feedback from off-topic noise, jokes, and review-bomb spam.</li>
      <li><strong>Classify:</strong> each review is tagged with sentiment and a category (bug, performance, gameplay, and so on) using structured model output.</li>
      <li><strong>Cluster:</strong> reviews describing the same issue are grouped into specific themes.</li>
      <li><strong>Rank by impact:</strong> themes are sorted by frequency plus behavioral weight, so issues raised by long-playtime, highly-upvoted reviewers rank above low-effort rage reviews.</li>
      <li><strong>Present:</strong> a dashboard with a Fix These view and a Double Down view, including counts, sentiment charts, and representative quotes.</li>
    </ol>
    <p class="note">"Impact" is an inferred heuristic (frequency, sentiment, playtime, helpful-votes), not ground truth. It is presented as an informed estimate.</p>

    <h2>Tech Stack</h2>
    <ul>
      <li><strong>Reviews:</strong> Steam public appreviews API (free, no key)</li>
      <li><strong>AI:</strong> LLM inference with structured/JSON output (OpenAI, or free-tier Gemini)</li>
      <li><strong>Frontend:</strong> web UI with charts for sentiment and themes</li>
      <li><strong>Backend:</strong> Flask, batched review processing, and per-game caching</li>
    </ul>

    <h2>Current Limitations</h2>
    <p>SteamSifter runs on a free or low-cost AI key, which caps how many requests it can make per day and per minute. That is fine for what this is right now: a solo project built for small, low-traffic usage. Scaling to many concurrent users would need a paid API key (where classifying a few hundred reviews costs only pennies) plus per-game caching so popular titles are analyzed only once.</p>
    <p>As of June 15, 2026, SteamSifter runs using an OpenAI API, and switches to other free-tier keys when necessary.</p>
  </main>
</body>
</html>"""


@app.route("/about")
def about():
    """Serve the About SteamSifter page."""
    return ABOUT_PAGE


if __name__ == "__main__":
    # threaded=True lets the progress endpoint respond while a job runs.
    app.run(debug=True, threaded=True, port=5000)
