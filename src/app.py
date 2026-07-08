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

import html
import math
import os
import re
import threading
import time
import uuid
from hmac import compare_digest

from flask import Flask, request, jsonify, Response, session, redirect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from search import search_games
from pipeline import get_analysis, DEFAULT_MAX_AGE_DAYS
from fetch_reviews import fetch_review_total
from report import build_html
import store

# Hex validator for the admin-set seasonal gradient (guards against a crafted
# POST injecting arbitrary CSS into the home page).
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


app = Flask(__name__)

# Secret key signs the admin session cookie. Set SECRET_KEY in production so the
# admin login survives restarts; fall back to a random per-process key in dev.
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24).hex()

# Without a stable SECRET_KEY the admin session cookie is signed with a random
# per-process key, so admin logins reset on every restart and break across
# multiple gunicorn workers. Warn loudly so it is caught before going public.
if not os.environ.get("SECRET_KEY"):
    print("WARNING: SECRET_KEY is not set. Admin sessions will reset on restart "
          "and will not work across workers. Set SECRET_KEY in production.")

# Harden the admin session cookie: HTTPS-only, no JavaScript access, and
# SameSite to blunt CSRF. SESSION_COOKIE_SECURE is relaxed for local http dev
# in the __main__ block below. The password itself is never sent to the client;
# only this signed cookie is, so it cannot be read or forged.
app.config.update(
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") != "0",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# Owner-only password. When unset, the admin bypass is simply disabled.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# A normal visitor can only force a re-analysis once the game has gained this
# fraction of new reviews since the cached run (0.20 = 20% more). The owner
# (admin) bypasses it entirely.
REFRESH_GROWTH = 0.20

# Behind a host's proxy (e.g. Render), trust X-Forwarded-* so the rate limiter
# sees real client IPs rather than the proxy's.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Per-visitor rate limits to protect the shared API key. Use Redis storage when
# available so limits are shared across gunicorn workers; in_memory_fallback keeps
# the app working if Redis is briefly unreachable.
_multi_worker = int(os.environ.get("WEB_CONCURRENCY", "1")) > 1
_limiter_uri = (os.environ.get("REDIS_URL") if _multi_worker else None) or "memory://"
try:
    limiter = Limiter(get_remote_address, app=app, storage_uri=_limiter_uri,
                      in_memory_fallback_enabled=True)
except Exception as _err:
    # If the Redis storage backend can't initialize, never crash at startup.
    print(f"Rate-limit storage '{_limiter_uri}' unavailable ({_err}); using in-memory.")
    limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")


def valid_appid(appid: str) -> bool:
    """
    True only for a plain Steam app id (a positive integer, sane length).

    app_id is interpolated into cache file names (reviews_<id>_all.json,
    analysis_<id>.json) and into upstream Steam URLs, so validating it at the
    edge closes off path traversal and pointless upstream calls in one place.
    """
    return bool(appid) and appid.isdigit() and len(appid) <= 12


# Defense-in-depth response headers on every response: block MIME sniffing, deny
# framing (clickjacking), trim referrer leakage, and apply a pragmatic CSP. The
# inline scripts/styles still require 'unsafe-inline', but locking object-src,
# base-uri, and frame-ancestors and constraining the other sources still helps.
@app.after_request
def set_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    )
    # Lock down browser features the app never uses.
    resp.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), camera=(), microphone=(), payment=(), usb=()")
    # HSTS: force HTTPS for a year (incl. subdomains). Only over real HTTPS, so
    # local http dev is unaffected. request.is_secure honors X-Forwarded-Proto.
    if request.is_secure:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


def is_admin() -> bool:
    """True when the current session has authenticated as the owner."""
    return bool(session.get("admin"))


def current_review_total(appid):
    """The game's current total review count, soft-cached for 10 min to spare Steam."""
    key = f"revtotal:{appid}"
    cached = store.cache_get_int(key)
    if cached is not None:
        return cached
    total = fetch_review_total(appid)
    if total:
        store.cache_set_int(key, total, 600)
    return total


def refresh_status(appid):
    """
    Decide whether a force-refresh is allowed for this game right now.

    Returns (allowed: bool, reviews_needed: int). Admins are always allowed.
    Other visitors must wait until the game has gained REFRESH_GROWTH more reviews
    (e.g. 20%) since the cached run. Degrades open if the count is unknown, so a
    transient Steam hiccup never hard-blocks (the rate limit still applies).
    """
    if is_admin():
        return True, 0
    cached = store.load_analysis(appid, DEFAULT_MAX_AGE_DAYS)
    baseline = (cached or {}).get("steam_total_reviews")
    if not baseline:
        return True, 0   # old cache or unknown baseline: nothing to gate on
    current = current_review_total(appid)
    if not current:
        return True, 0   # can't check right now: do not hard-block
    threshold = math.ceil(baseline * (1 + REFRESH_GROWTH))
    if current >= threshold:
        return True, 0
    return False, threshold - current


# The home page. Plain string (not an f-string) so the JS braces are safe.
HOME_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2366c0f4' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='7'/><line x1='21' y1='21' x2='16.65' y2='16.65'/></svg>">
<title>SteamSifter</title>
<meta name="description" content="SteamSifter turns Steam reviews into a ranked, themed breakdown of what to fix and what players love. AI-powered review analysis for game studios.">
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
  .results { position: absolute; left: 0; right: 0; top: 56px; background: #16202d; border: 1px solid #2a475e; border-radius: 8px; overflow-y: auto; max-height: 360px; z-index: 5; text-align: left; }
  .result { display: flex; align-items: center; gap: 12px; padding: 10px 12px; cursor: pointer; }
  .result:hover { background: #1f3346; }
  .result img { width: 60px; height: 23px; object-fit: cover; border-radius: 3px; background: #0e1620; }
  .result span { font-size: 14px; color: #c7d5e0; }
  .hint { color: #66758a; font-size: 12px; margin-top: 18px; }
  .site-footer { background: #171a21; border-top: 1px solid #0e1620; padding: 18px 24px; text-align: center; }
  .bgjob { position: fixed; right: 18px; bottom: 18px; width: 280px; background: #16202d; border: 1px solid #2a475e; border-radius: 10px; padding: 14px 16px; box-shadow: 0 6px 24px rgba(0,0,0,0.4); z-index: 50; text-align: left; }
  .bgjob-label { color: #66c0f4; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; }
  .bgjob-title { color: #fff; font-size: 15px; font-weight: 600; margin: 2px 0 8px; word-break: break-word; }
  .bgjob-thumblink { display: block; margin-bottom: 8px; }
  .bgjob-thumb { width: 100%; border-radius: 6px; display: none; }
  .bgjob-meta { color: #66758a; font-size: 12px; margin-top: 6px; font-variant-numeric: tabular-nums; }
  .bgjob-track { background: #0e1620; border-radius: 6px; height: 8px; overflow: hidden; }
  .bgjob-fill { height: 100%; width: 0%; background: linear-gradient(90deg,#1a9fff,#66c0f4); transition: width .3s; }
  .bgjob-status { color: #8f98a0; font-size: 12px; margin-top: 8px; min-height: 16px; }
  .bgjob-btn { display: inline-block; margin-top: 10px; background: #66c0f4; color: #0e1620; font-weight: 600; font-size: 13px; padding: 7px 12px; border-radius: 6px; text-decoration: none; }
  .bgjob-x { position: absolute; top: 8px; right: 10px; background: none; border: none; color: #8f98a0; font-size: 18px; cursor: pointer; line-height: 1; }
  .footer-links { display: flex; gap: 20px; justify-content: center; align-items: center; flex-wrap: wrap; }
  .footer-links a { color: #8f98a0; text-decoration: none; font-size: 14px; display: inline-flex; align-items: center; }
  .footer-links a:hover { color: #66c0f4; }
  .footer-links svg { width: 20px; height: 20px; fill: currentColor; display: block; }
  .disclaimer { margin: 12px auto 0; color: #5a6675; font-size: 11px; line-height: 1.5; max-width: 640px; }
  .anns { position: fixed; top: 16px; right: 16px; width: 300px; max-width: calc(100vw - 32px); z-index: 40; display: flex; flex-direction: column; gap: 10px; }
  .ann { position: relative; background: #16202d; border: 1px solid #2a475e; border-left: 3px solid #66c0f4; border-radius: 10px; padding: 13px 38px 13px 15px; box-shadow: 0 6px 24px rgba(0,0,0,0.4); text-align: left; transition: opacity .3s ease, transform .3s ease; }
  .ann.dismissing { opacity: 0; transform: translateX(16px); pointer-events: none; }
  .ann-t { color: #fff; font-size: 14px; font-weight: 600; }
  .ann-m { color: #8f98a0; font-size: 13px; margin-top: 3px; line-height: 1.5; }
  .ann-x { position: absolute; top: 8px; right: 10px; background: none; border: none; color: #8f98a0; font-size: 18px; line-height: 1; cursor: pointer; }
  .ann-x:hover { color: #c7d5e0; }
  @media (max-width: 640px) { .anns { top: 10px; right: 10px; left: 10px; width: auto; } }
  @media (max-width: 640px) {
    h1 { font-size: 27px; }
    .hero { padding: 20px 16px; }
    input[type=text] { font-size: 16px; padding: 12px 14px; }
    .site-footer { padding: 16px; }
    .footer-links { gap: 14px; }
  }
</style>
{{THEME_STYLE}}
</head>
<body>
  {{ANNOUNCEMENTS}}
  <div class="hero">
    <div class="hero-inner">
      <div class="brand">SteamSifter</div>
      <h1>Analyze any game's reviews</h1>
      <p class="sub">Search a Steam game to see what players want fixed or what they love.</p>
      <div class="search">
        <input id="q" type="text" placeholder="Search a game, e.g. Counter-Strike" autocomplete="off" autofocus>
        <div id="results" class="results" style="display:none"></div>
      </div>
      <div class="hint">First-time analysis usually takes 1 to 3 minutes. After that, repeat lookups are instant.</div>
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

  <div id="bgJob" class="bgjob" style="display:none">
    <button id="bgJobDismiss" class="bgjob-x" title="Dismiss" aria-label="Dismiss">&times;</button>
    <a id="bgJobLink" class="bgjob-thumblink" title="Back to the loading screen"><img id="bgJobThumb" class="bgjob-thumb" alt=""></a>
    <div class="bgjob-label">Analyzing</div>
    <div class="bgjob-title" id="bgJobTitle"></div>
    <div class="bgjob-track"><div class="bgjob-fill" id="bgJobFill"></div></div>
    <div class="bgjob-meta"><span id="bgJobPct">0 / 1000</span> &middot; <span id="bgJobElapsed">0:00</span></div>
    <div class="bgjob-status" id="bgJobStatus">Starting...</div>
    <a class="bgjob-btn" id="bgJobView" style="display:none">View report &rarr;</a>
  </div>

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
      const img = document.createElement('img');
      if (g.image) img.src = g.image;
      const span = document.createElement('span');
      span.textContent = g.name;
      row.appendChild(img);
      row.appendChild(span);
      row.onclick = () => analyze(g.appid, g.name);
      results.appendChild(row);
    });
    results.style.display = 'block';
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !/^(INPUT|TEXTAREA)$/.test(e.target.tagName || '')) {
      e.preventDefault(); input.focus();
    }
  });
  function analyze(appid, name) {
    window.location = '/analyzing?appid=' + appid + '&title=' + encodeURIComponent(name);
  }

  // Home-page announcements: hide any the visitor already dismissed (kept in
  // their browser), and let them dismiss the rest.
  (function () {
    var seen = [];
    try { seen = JSON.parse(localStorage.getItem('ss_dismissed_anns') || '[]'); } catch (e) {}
    document.querySelectorAll('.ann').forEach(function (el) {
      if (seen.indexOf(el.getAttribute('data-ann')) !== -1) el.style.display = 'none';
    });
  })();
  function dismissAnn(id) {
    var seen = [];
    try { seen = JSON.parse(localStorage.getItem('ss_dismissed_anns') || '[]'); } catch (e) {}
    if (seen.indexOf(id) === -1) seen.push(id);
    try { localStorage.setItem('ss_dismissed_anns', JSON.stringify(seen)); } catch (e) {}
    var el = document.querySelector('.ann[data-ann="' + id + '"]');
    if (el) { el.classList.add('dismissing'); setTimeout(function () { el.style.display = 'none'; }, 300); }
  }

  // Background-analysis widget: if a game is still analyzing (started before the
  // user returned to the home page), show its progress, disable search until it
  // finishes, then offer a button to open the finished report.
  (function () {
    var raw = null;
    try { raw = JSON.parse(localStorage.getItem('ss_job') || 'null'); } catch (e) {}
    if (!raw || !raw.job) return;
    var box = document.getElementById('bgJob');
    var fill = document.getElementById('bgJobFill');
    var status = document.getElementById('bgJobStatus');
    var view = document.getElementById('bgJobView');
    var pctEl = document.getElementById('bgJobPct');
    var elapsedEl = document.getElementById('bgJobElapsed');
    var aid = encodeURIComponent(raw.appid);
    var qs = 'appid=' + aid + '&title=' + encodeURIComponent(raw.title || '');
    document.getElementById('bgJobTitle').textContent = raw.title || 'your game';
    view.href = '/analyze?' + qs;
    // Banner, clickable back to the full loading screen (which re-attaches to the
    // same running job); falls back across Steam asset hosts.
    var link = document.getElementById('bgJobLink');
    var thumb = document.getElementById('bgJobThumb');
    link.href = '/analyzing?' + qs;
    var srcs = [
      'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/' + aid + '/header.jpg',
      'https://cdn.cloudflare.steamstatic.com/steam/apps/' + aid + '/header.jpg',
      'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/' + aid + '/capsule_616x353.jpg'
    ];
    var si = 0;
    thumb.onload = function () { thumb.style.display = 'block'; };
    thumb.onerror = function () { si++; if (si < srcs.length) thumb.src = srcs[si]; else thumb.style.display = 'none'; };
    thumb.src = srcs[0];
    box.style.display = 'block';
    function setDisabled(d) {
      input.disabled = d;
      input.placeholder = d ? 'Analysis in progress, please wait...' : 'Search a game, e.g. Counter-Strike';
    }
    setDisabled(true);
    var started = raw.started || Date.now();
    var timer = setInterval(function () {
      var s = Math.max(0, Math.floor((Date.now() - started) / 1000));
      elapsedEl.textContent = Math.floor(s / 60) + ':' + (s % 60 < 10 ? '0' : '') + (s % 60);
    }, 1000);
    document.getElementById('bgJobDismiss').onclick = function () {
      try { localStorage.removeItem('ss_job'); } catch (e) {}
      clearInterval(timer); box.style.display = 'none'; setDisabled(false);
    };
    function finish() {
      clearInterval(timer); setDisabled(false);
      fill.style.width = '100%'; pctEl.textContent = '1000 / 1000';
      status.textContent = 'Report ready'; view.style.display = 'inline-block';
    }
    function poll() {
      fetch('/progress?job=' + encodeURIComponent(raw.job))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (typeof d.elapsed === 'number') started = Date.now() - d.elapsed * 1000;  // anchor to server elapsed (skew-free)
          var p = Math.min(100, d.percent || 0);
          fill.style.width = p + '%';
          pctEl.textContent = Math.floor(p * 10) + ' / 1000';
          if (d.message) status.textContent = d.message;
          if (d.done) { finish(); return; }
          setTimeout(poll, 1200);
        })
        .catch(function () { setTimeout(poll, 2000); });
    }
    poll();
  })();
</script>
</body>
</html>"""


def _render_home():
    """Home HTML with active announcements + seasonal gradient injected."""
    ann_html = ""
    anns = store.announcements_active()
    if anns:
        rows = ""
        for a in anns:
            aid = html.escape(a.get("id", ""))
            rows += (
                f'<div class="ann" data-ann="{aid}">'
                f'<button class="ann-x" onclick="dismissAnn(&#39;{aid}&#39;)" '
                'title="Dismiss" aria-label="Dismiss">&times;</button>'
                f'<div class="ann-t">{html.escape(a.get("title", ""))}</div>'
                f'<div class="ann-m">{html.escape(a.get("message", ""))}</div>'
                '</div>'
            )
        ann_html = f'<div class="anns">{rows}</div>'
    theme_style = ""
    theme = store.theme_active()
    if theme and _HEX_RE.match(theme.get("grad_top", "")) and _HEX_RE.match(theme.get("grad_bottom", "")):
        theme_style = (
            "<style>"
            "body{background:linear-gradient(160deg," + theme["grad_top"] + "," + theme["grad_bottom"] + ") fixed !important;}"
            # Footer fades in from transparent so the gradient bleeds through, no hard edge.
            ".site-footer{background:linear-gradient(to bottom,rgba(14,22,32,0),rgba(14,22,32,0.92)) !important;border-top:none !important;}"
            # Search box goes translucent so the gradient tints it instead of a stark block.
            "input[type=text]{background:rgba(16,22,32,0.55) !important;}"
            "</style>"
        )
    return (HOME_PAGE
            .replace("{{ANNOUNCEMENTS}}", ann_html)
            .replace("{{THEME_STYLE}}", theme_style))


@app.route("/")
def home():
    """Serve the search home page (with active announcements + theme)."""
    return Response(_render_home(), mimetype="text/html")


@app.route("/api/search")
@limiter.limit("120 per minute")   # generous for live typing, caps scripted abuse
def api_search():
    """Return JSON game suggestions for the search box."""
    return jsonify(search_games(request.args.get("q", "")))


EMPTY_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2366c0f4' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='7'/><line x1='21' y1='21' x2='16.65' y2='16.65'/></svg>">
<title>No reviews | SteamSifter</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; min-height: 100vh;
         display: flex; align-items: center; justify-content: center;
         background: linear-gradient(to bottom, #1b2838, #16202d) fixed; color: #c7d5e0; }
  .hero-inner { width: 100%; max-width: 520px; padding: 24px; text-align: center; }
  .brand { color: #66c0f4; letter-spacing: 3px; text-transform: uppercase; font-size: 13px; }
  h1 { margin: 10px 0 6px; font-size: 28px; color: #fff; }
  p.sub { color: #8f98a0; margin: 0 0 22px; }
  .search { position: relative; text-align: left; }
  input[type=text] { width: 100%; padding: 14px 16px; font-size: 16px; border-radius: 8px; border: 1px solid #2a475e; background: #16202d; color: #fff; }
  .results { position: absolute; left: 0; right: 0; top: 56px; background: #16202d; border: 1px solid #2a475e; border-radius: 8px; overflow-y: auto; max-height: 360px; z-index: 5; }
  .result { display: flex; align-items: center; gap: 12px; padding: 10px 12px; cursor: pointer; }
  .result:hover { background: #1f3346; }
  .result img { width: 60px; height: 23px; object-fit: cover; border-radius: 3px; background: #0e1620; }
  .result span { font-size: 14px; color: #c7d5e0; }
  .hint { margin-top: 18px; font-size: 13px; }
  .hint a { color: #66c0f4; text-decoration: none; }
</style>
</head>
<body>
  <div class="hero-inner">
    <div class="brand">SteamSifter</div>
    <h1>Nothing to sift here :C</h1>
    <p class="sub"><strong>__TITLE__</strong> has no Steam reviews to analyze yet. Pick another game and let's dig in.</p>
    <div class="search">
      <input id="q" type="text" placeholder="Search a game, e.g. Counter-Strike" autocomplete="off" autofocus>
      <div id="results" class="results" style="display:none"></div>
    </div>
    <div class="hint"><a href="/">Back to home</a></div>
  </div>
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
      renderSuggestions(await resp.json());
    } catch (e) { results.style.display = 'none'; }
  }
  function renderSuggestions(games) {
    if (!games.length) { results.style.display = 'none'; return; }
    results.innerHTML = '';
    games.forEach(g => {
      const row = document.createElement('div');
      row.className = 'result';
      const img = document.createElement('img');
      if (g.image) img.src = g.image;
      const span = document.createElement('span');
      span.textContent = g.name;
      row.appendChild(img);
      row.appendChild(span);
      row.onclick = () => { window.location = '/analyzing?appid=' + g.appid + '&title=' + encodeURIComponent(g.name); };
      results.appendChild(row);
    });
    results.style.display = 'block';
  }
</script>
</body>
</html>"""


@app.route("/analyze")
@limiter.limit("40 per hour")
def analyze():
    """Run the cached pipeline for a game and serve its report."""
    appid = request.args.get("appid", "")
    if not valid_appid(appid):
        return "Invalid appid", 400

    title = request.args.get("title") or f"App {appid}"

    # get_analysis uses the cache, so repeat lookups are instant and free.
    analysis = get_analysis(appid)

    # No reviews to analyze: show a friendly empty-state page, not an all-zeros report.
    if not analysis.get("total_reviews"):
        return Response(EMPTY_PAGE.replace("__TITLE__", html.escape(title)),
                        mimetype="text/html")

    # Tell the report whether a force-refresh is available right now, so the
    # button renders enabled, on cooldown, or in admin mode.
    allowed, needed = refresh_status(appid)
    refresh_state = {
        "appid": appid,
        "title": title,
        "allowed": allowed,
        "reviews_needed": needed,
        "admin": is_admin(),
    }
    return Response(build_html(analysis, title, refresh_state), mimetype="text/html")


# ----------------------------------------------------------------------------
# Background analysis jobs (so the page can show a real progress bar)
# ----------------------------------------------------------------------------

# Job state lives in the shared store (Redis across workers, else in-memory), so
# progress polling and same-game de-duplication work no matter which gunicorn
# worker handles each request.


def _run_job(job_id, appid, refresh=False):
    """Run the analysis in a background thread, recording progress in the store."""
    try:
        get_analysis(appid, refresh=refresh,
                     progress=lambda pct, msg: store.job_update(job_id, pct, msg))
        store.job_finish(appid, job_id)
    except Exception as e:
        # Log the real error server-side, but hand users a generic message so
        # internal details (paths, tracebacks) never leak out via /progress.
        print(f"Analysis job {job_id} for app {appid} failed: {e}")
        store.job_finish(appid, job_id, error="Analysis failed. Please try again.")


def _begin_job(appid, refresh=False):
    """
    Start a background analysis and return its job id. If a job for this same game
    is already running (anywhere across workers), attach to it instead of starting
    a duplicate run.
    """
    job_id, is_new = store.job_begin(appid)
    if is_new:
        threading.Thread(target=_run_job, args=(job_id, appid, refresh), daemon=True).start()
    return job_id


ANALYZING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2366c0f4' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='7'/><line x1='21' y1='21' x2='16.65' y2='16.65'/></svg>">
<title>Analyzing... | SteamSifter</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
         background: linear-gradient(to bottom, #1b2838, #16202d) fixed; color: #c7d5e0; display: flex;
         flex-direction: column; min-height: 100vh; }
  .bg-blur { position: fixed; top: -6%; left: -6%; width: 112%; height: 112%; z-index: -2; object-fit: cover; filter: blur(32px) saturate(1.08) brightness(0.6); opacity: 0; transition: opacity .8s ease; pointer-events: none; }
  .bg-scrim { position: fixed; top: 0; left: 0; right: 0; bottom: 0; z-index: -1; background: linear-gradient(to bottom, rgba(11,16,24,0.5), rgba(14,20,30,0.82)); pointer-events: none; }
  .box { width: 100%; max-width: 520px; padding: 24px; text-align: center; margin: auto; }
  .brand { color: #66c0f4; letter-spacing: 3px; text-transform: uppercase; font-size: 13px; }
  h1 { margin: 10px 0 24px; font-size: 24px; color: #fff; }
  .track { background: #16202d; border: 1px solid #2a475e; border-radius: 8px; height: 22px; overflow: hidden; }
  .fill { height: 100%; width: 0%; background: linear-gradient(90deg,#1a9fff,#66c0f4); transition: width .15s linear; }
  .pct { font-size: 36px; font-weight: 700; color: #fff; margin: 16px 0 4px; font-variant-numeric: tabular-nums; }
  .msg { color: #8f98a0; font-size: 14px; min-height: 20px; }
  .flavor { color: #66c0f4; font-size: 13px; min-height: 18px; margin-top: 12px;
           opacity: 0; transition: opacity .45s ease; }
  .elapsed { color: #66758a; font-size: 13px; margin-top: 10px; font-variant-numeric: tabular-nums; }
  .leavehint { color: #5a6675; font-size: 12px; margin-top: 16px; max-width: 420px; margin-left: auto; margin-right: auto; line-height: 1.5; }
  .err { color: #e06c75; font-size: 14px; margin-top: 14px; }
  .gamethumb { width: 240px; max-width: 100%; height: auto; border-radius: 6px;
               border: 1px solid #2a475e; margin: 20px auto 18px; display: none; }
  .site-footer { background: #171a21; border-top: 1px solid #0e1620; padding: 22px 24px; text-align: center; }
  .footer-links { display: flex; gap: 18px; justify-content: center; flex-wrap: wrap; }
  .footer-links a { color: #8f98a0; text-decoration: none; font-size: 13px; }
  .footer-links a:hover { color: #66c0f4; }
  .site-footer .disclaimer { margin: 12px auto 0; color: #5a6675; font-size: 11px; max-width: 640px; line-height: 1.5; }
</style>
</head>
<body>
  <img id="bgBlur" class="bg-blur" alt="">
  <div class="bg-scrim"></div>
  <div class="box">
    <a href="/" class="brand" style="text-decoration: none" onclick="window.__leaving = 1">SteamSifter</a>
    <img id="thumb" class="gamethumb" alt="">
    <h1 id="title">Analyzing...</h1>
    <div class="track"><div id="fill" class="fill"></div></div>
    <div id="pct" class="pct">0 / 1000</div>
    <div id="msg" class="msg">Starting...</div>
    <div id="flavor" class="flavor"></div>
    <div id="elapsed" class="elapsed">Elapsed 0:00</div>
    <div class="leavehint">Safe to leave or close this tab - the analysis keeps running, and the report will be waiting when you come back.</div>
    <div id="err" class="err"></div>
  </div>
  <footer class="site-footer">
    <div class="footer-links">
      <a href="/about">About SteamSifter</a>
      <a href="https://github.com/Danielhyunyoo/SteamSifter" target="_blank" rel="noopener">GitHub</a>
      <a href="https://steamcommunity.com/profiles/76561198990353371/" target="_blank" rel="noopener">Steam</a>
      <a href="https://www.linkedin.com/in/danielhyunwooyoo/" target="_blank" rel="noopener">LinkedIn</a>
    </div>
    <div class="disclaimer">SteamSifter is an independent project and is not affiliated with, endorsed by, or sponsored by Valve or Steam. "Steam" is a trademark of Valve Corporation.</div>
  </footer>
<script>
  const params = new URLSearchParams(window.location.search);
  const appid = params.get('appid');
  const title = params.get('title') || '';
  const force = params.get('force');   // set by the report's Re-analyze button
  if (title) document.getElementById('title').textContent = 'Analyzing ' + title;

  // Show the game's Steam banner once it loads; stay hidden if the title has none.
  var thumb = document.getElementById('thumb');
  if (appid) {
    window.__bannerError = function (img) {
      try {
        var list = JSON.parse(img.getAttribute('data-srcs') || '[]');
        var i = (parseInt(img.getAttribute('data-i'), 10) || 0) + 1;
        if (i < list.length) { img.setAttribute('data-i', i); img.src = list[i]; }
        else { img.style.display = 'none'; }
      } catch (e) { img.style.display = 'none'; }
    };
    var aid = encodeURIComponent(appid);
    var srcs = [
      'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/' + aid + '/header.jpg',
      'https://cdn.cloudflare.steamstatic.com/steam/apps/' + aid + '/header.jpg',
      'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/' + aid + '/capsule_616x353.jpg'
    ];
    thumb.setAttribute('data-srcs', JSON.stringify(srcs));
    thumb.setAttribute('data-i', '0');
    thumb.onload = function () { thumb.style.display = 'block'; };
    thumb.onerror = function () { window.__bannerError(thumb); };
    thumb.src = srcs[0] + '?t=' + Math.floor(Date.now() / 86400000);
    // Same banner, blurred, as an ambient full-page background (like the share card).
    var bg = document.getElementById('bgBlur');
    if (bg) {
      bg.setAttribute('data-srcs', JSON.stringify(srcs));
      bg.setAttribute('data-i', '0');
      bg.onload = function () { bg.style.opacity = '1'; };
      bg.onerror = function () { window.__bannerError(bg); };
      bg.src = srcs[0] + '?t=' + Math.floor(Date.now() / 86400000);
    }
  }

  const fill = document.getElementById('fill');
  const pct = document.getElementById('pct');
  const msg = document.getElementById('msg');

  // Rotating, fading flavor lines so the wait feels intentional (and a bit fun).
  var flavorEl = document.getElementById('flavor');
  var flavors = [
    "Reading the rage reviews so you don't have to...",
    "Weighing playtime against pure salt...",
    "Sifting jokes from real bug reports...",
    "Counting how many people typed 'unplayable'...",
    "Asking the sweaty try-hards what they really think...",
    "Doing the math on a thousand opinions...",
    "Hang in there, it's almost done, TRUST...",
    "Almost there, the AI is forming opinions..."
  ];
  var flavorIdx = 0;
  function rotateFlavor() {
    if (!flavorEl) return;
    flavorEl.style.opacity = 0;
    setTimeout(function () {
      flavorEl.textContent = flavors[flavorIdx % flavors.length];
      flavorIdx++;
      flavorEl.style.opacity = 1;
    }, 450);
  }
  rotateFlavor();
  setInterval(rotateFlavor, 4200);

  var target = 0;     // latest server-reported percent (0-100)
  var shown = 0;      // smoothly animated value, so the counter never looks frozen
  var done = false;
  var hasError = false;

  // Confirm before leaving/closing while still analyzing. The work continues in
  // the background regardless; this just catches an accidental close.
  window.addEventListener('beforeunload', function (e) {
    if (!done && !hasError && !window.__leaving) { e.preventDefault(); e.returnValue = ''; }
  });

  // Elapsed-time counter, so a long analysis always shows it is still working.
  // Seed from this game's stored start so switching pages does not reset it;
  // the server clock (started_at from /progress) becomes authoritative below.
  var startTime = Date.now();
  try {
    var _prevJob = JSON.parse(localStorage.getItem('ss_job') || 'null');
    if (_prevJob && _prevJob.started && _prevJob.appid === appid) startTime = _prevJob.started;
  } catch (e) {}
  var elapsedEl = document.getElementById('elapsed');
  var elapsedTimer = setInterval(function () {
    var s = Math.max(0, Math.floor((Date.now() - startTime) / 1000));
    var m = Math.floor(s / 60), sec = s % 60;
    if (elapsedEl) elapsedEl.textContent = 'Elapsed ' + m + ':' + (sec < 10 ? '0' : '') + sec;
    if (hasError) clearInterval(elapsedTimer);
  }, 1000);

  function redirect() {
    window.location = '/analyze?appid=' + encodeURIComponent(appid) +
      '&title=' + encodeURIComponent(title);
  }

  // Always creep toward the target (and a little past while waiting) so the
  // number out of 1000 keeps ticking, like a shader compiler.
  function tick() {
    if (hasError) return;
    var ceil = done ? 100 : Math.min(target + 3, 98);
    if (shown < ceil) {
      shown += Math.max(0.05, (ceil - shown) * 0.045);
      if (shown > ceil) shown = ceil;
    }
    pct.textContent = Math.floor(shown * 10) + ' / 1000';
    fill.style.width = shown.toFixed(1) + '%';
    if (done && shown >= 99.9) { redirect(); return; }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  function showError(e) {
    hasError = true;
    document.getElementById('err').textContent =
      'Something went wrong: ' + e + '. Go back and try again.';
    msg.textContent = '';
  }


  // --- Completion chime: a soft two-note ping (Web Audio, no asset needed). ---
  // Lets a user who tabbed away hear when their report is ready.
  var audioCtx = null;
  var dinged = false;
  function getCtx() {
    if (!audioCtx) {
      try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
      catch (e) { audioCtx = null; }
    }
    return audioCtx;
  }
  // Resume audio on any interaction, for browsers that gate sound behind a gesture.
  ['pointerdown', 'keydown'].forEach(function (ev) {
    window.addEventListener(ev, function () {
      var c = getCtx();
      if (c && c.state === 'suspended') c.resume();
    });
  });
  function playDing() {
    var c = getCtx();
    if (!c) return;
    if (c.state === 'suspended') { c.resume().catch(function () {}); }
    var now = c.currentTime;
    // Two gentle sine notes (A5 then E6) with soft envelopes and low volume.
    [[880, 0.0], [1318.5, 0.16]].forEach(function (pair) {
      var osc = c.createOscillator();
      var gain = c.createGain();
      osc.type = 'sine';
      osc.frequency.value = pair[0];
      var t = now + pair[1];
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.14, t + 0.02);    // soft attack
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.33);  // gentle decay
      osc.connect(gain);
      gain.connect(c.destination);
      osc.start(t);
      osc.stop(t + 0.38);
    });
  }
  function notifyDone() {
    if (dinged) return;
    dinged = true;
    playDing();
    // Visual cue for a tabbed-away user: change the tab title.
    document.title = 'Report ready | SteamSifter';
  }

  var jobId = null;
  var startUrl = (force ? '/refresh' : '/start') + '?appid=' + encodeURIComponent(appid);
  fetch(startUrl)
    .then(r => r.json())
    .then(d => { if (d.error) { showError(d.error); return; } jobId = d.job;
      try {
        var _existing = JSON.parse(localStorage.getItem('ss_job') || 'null');
        var _startedAt = (_existing && _existing.job === jobId && _existing.started) ? _existing.started : Date.now();
        localStorage.setItem('ss_job', JSON.stringify({job: jobId, appid: appid, title: title, started: _startedAt}));
      } catch (e) {}
      poll(); })
    .catch(() => showError('Could not start analysis.'));

  function poll() {
    fetch('/progress?job=' + jobId)
      .then(r => r.json())
      .then(d => {
        if (d.error) { showError(d.error); return; }
        if (typeof d.elapsed === 'number') startTime = Date.now() - d.elapsed * 1000;  // anchor to server elapsed (skew-free)
        target = d.percent || 0;
        if (d.message) msg.textContent = d.message;
        if (d.done) {
          target = 100;
          if (!done) { done = true; notifyDone(); }   // fire the chime once
        }
        else { setTimeout(poll, 800); }
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
    """Kick off a background analysis job (cache-friendly); returns a job id."""
    appid = request.args.get("appid", "")
    if not valid_appid(appid):
        return jsonify({"error": "invalid appid"}), 400
    return jsonify({"job": _begin_job(appid, refresh=False)})


@app.route("/refresh")
@limiter.limit("3 per hour", exempt_when=is_admin)
def refresh():
    """
    Force a fresh re-analysis, bypassing the cache. Guarded so normal visitors
    cannot spam it: the report must be older than the cooldown (admins exempt),
    and the route is rate limited on top of that.
    """
    appid = request.args.get("appid", "")
    if not valid_appid(appid):
        return jsonify({"error": "invalid appid"}), 400
    allowed, needed = refresh_status(appid)
    if not allowed:
        return jsonify({"error": f"Not enough new reviews yet. About {needed:,} more "
                                 "reviews are needed before this can be re-analyzed."}), 429
    return jsonify({"job": _begin_job(appid, refresh=True)})


@app.route("/progress")
def progress_route():
    """Return the current progress for a job id."""
    job = request.args.get("job", "")
    return jsonify(store.job_get(job) or {"error": "unknown job", "done": True})


ABOUT_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2366c0f4' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='7'/><line x1='21' y1='21' x2='16.65' y2='16.65'/></svg>">
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
  @media (max-width: 640px) {
    header { padding: 16px 18px; }
    main { padding: 22px 16px 48px; }
    h1 { font-size: 24px; }
    .profiles { gap: 12px; }
  }
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
      <li><strong>Search and ingest:</strong> resolves a game name to its Steam app ID and pulls the most recent reviews (up to a few thousand) from Steam's free public API, each with its recommend flag, helpful votes, playtime, language, and date.</li>
      <li><strong>Filter for signal:</strong> a relevance classifier separates constructive feedback from off-topic noise, jokes, and review-bomb spam.</li>
      <li><strong>Classify:</strong> each review is tagged with sentiment and a category (bug, performance, gameplay, cheating, monetization, UI/UX, content, community, praise). This runs on fast local models distilled from the LLM (logistic regression over embeddings), with an LLM fallback for uncertain cases, and keeps growing its own training set in the background for future retraining.</li>
      <li><strong>Theme:</strong> reviews are grouped into specific, named themes for each side, by an LLM pass for small sets or by embeddings + k-means clustering at scale, with all clusters named in one batched call and the two sides themed concurrently.</li>
      <li><strong>Route and rank:</strong> each review lands on the Issues or Praise side by the player's recommend vote first, refined by sentiment; themes are then ranked by frequency plus behavioral weight, so issues raised by long-playtime, highly-upvoted reviewers rank above low-effort rage reviews.</li>
      <li><strong>Present:</strong> a two-column dashboard, a full-width scoreboard, the sentiment donut and sentiment-over-time trend side by side, and Issues / Praise theme cards in two columns with clickable example quotes (each showing the reviewer's avatar and name plus an English translation for non-English reviews, excerpted at the theme-relevant part of long reviews). Every report also offers live filters (recommendation, playtime, language), a print / save-as-PDF export, and a rich link-preview card when shared.</li>
    </ol>
    <p class="note">"Impact" is an inferred heuristic (frequency, sentiment, playtime, helpful-votes), not ground truth. It is presented as an informed estimate.</p>

    <h2>Tech Stack</h2>
    <ul>
      <li><strong>Reviews and search:</strong> Steam's public appreviews API (free, no key), plus the games-filtered storefront search that surfaces niche titles over DLC</li>
      <li><strong>AI and ML:</strong> OpenAI (gpt-4.1-mini) with structured output via Pydantic, swappable to free-tier Gemini; per-review classification distilled into local scikit-learn models (over text-embedding-3-small embeddings) with confidence gating and an optional fully-local mode; theming that scales via embeddings and k-means with single-call cluster labeling and a centroid merge to consolidate duplicates</li>
      <li><strong>Frontend:</strong> Steam-styled, mobile-responsive two-column UI with Chart.js charts (pinned with Subresource Integrity), a summary scoreboard, a Steam-style rating badge, live review filters, a print / save-as-PDF view, a blurred-banner loading screen, and admin-managed announcements and a seasonal theme</li>
      <li><strong>Backend:</strong> Flask and gunicorn, background jobs with a live progress bar, a Redis-shared (multi-worker-ready) job store, per-game caching persisted to Redis, and environment-tunable concurrency, batch size, review count, and confidence thresholds</li>
      <li><strong>Sharing and security:</strong> dynamic OpenGraph share cards generated with Pillow, plus a hardening pass and full audit (app-ID validation, rate-limited admin auth, security headers, CDN Subresource Integrity, gated debug server, DOM-safe rendering)</li>
      <li><strong>Deployment:</strong> Render on the custom domain steamsifter.com (Cloudflare DNS), configured entirely through environment variables</li>
    </ul>

    <h2>Current Limitations</h2>
    <p>SteamSifter is deployed and open to anyone, but it is a solo project tuned for light traffic: a single worker on a free Render instance sharing one AI key. Classification runs on distilled local models, so it no longer dominates the wait; analyses often finish in well under a minute at typical volumes and stay bounded into the low minutes at several thousand reviews, capped mainly by the OpenAI tier's throughput and the free instance's memory. Results are cached so popular titles are only re-analyzed when their reviews grow, and the code is multi-worker ready, so scaling out is mostly a matter of paid hosting rather than a rewrite.</p>
    <p>As of July 2026, SteamSifter runs on the OpenAI API (gpt-4.1-mini) with distilled scikit-learn classifiers, and can switch to free-tier Gemini when needed.</p>
  </main>
</body>
</html>"""


@app.route("/about")
def about():
    """Serve the About SteamSifter page."""
    return ABOUT_PAGE


# Small per-worker cache of generated share cards, so repeat crawler hits and
# refreshes do not re-render (and re-download the banner) every time.
_og_cache = {}
_OG_TTL = 3600


@app.route("/og/<appid>.png")
@limiter.limit("60 per minute")
def og_image(appid):
    """Render the 1200x630 social-share (OpenGraph) card for a game."""
    if not valid_appid(appid):
        return "Invalid appid", 404
    title = (request.args.get("t") or f"App {appid}")[:80]
    ver = request.args.get("v", "")
    now = time.time()
    key = (appid, title, ver)
    cached = _og_cache.get(key)
    if cached and now - cached[0] < _OG_TTL:
        png = cached[1]
    else:
        import og_card
        analysis = store.load_analysis(appid, DEFAULT_MAX_AGE_DAYS)
        try:
            png = og_card.render(appid, title, analysis)
        except Exception as err:
            print(f"OG card render failed for {appid}: {err}")
            return "", 500
        _og_cache[key] = (now, png)
        if len(_og_cache) > 200:                      # crude prune of oldest
            for k in sorted(_og_cache, key=lambda k: _og_cache[k][0])[:80]:
                _og_cache.pop(k, None)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ----------------------------------------------------------------------------
# Rate-limit response + owner (admin) login
# ----------------------------------------------------------------------------

@app.errorhandler(429)
def too_many(_e):
    """JSON for the polled endpoints so the page can show a clean message."""
    if request.path in ("/start", "/refresh"):
        return jsonify({"error": "You are doing that too often. Please wait a "
                        "bit and try again."}), 429
    return Response("Rate limit exceeded. Please slow down and try again.",
                    status=429, mimetype="text/plain")


ADMIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2366c0f4' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='7'/><line x1='21' y1='21' x2='16.65' y2='16.65'/></svg>">
<title>Owner sign-in | SteamSifter</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; min-height: 100vh;
         display: flex; align-items: center; justify-content: center;
         background: linear-gradient(to bottom, #1b2838, #16202d) fixed; color: #c7d5e0; }
  .box { width: 100%; max-width: 360px; padding: 24px; text-align: center; }
  .brand { color: #66c0f4; letter-spacing: 3px; text-transform: uppercase; font-size: 13px; }
  h1 { margin: 10px 0 18px; font-size: 22px; color: #fff; }
  input { width: 100%; padding: 12px 14px; font-size: 15px; border-radius: 6px;
          border: 1px solid #2a475e; background: #16202d; color: #fff; margin-bottom: 12px; }
  .btn { display: inline-block; border: 1px solid #2a475e; background: #16202d; color: #66c0f4;
         border-radius: 6px; padding: 10px 16px; font-size: 14px; font-weight: 600;
         text-decoration: none; cursor: pointer; }
  .btn:hover { background: #1f3346; color: #8fd0fb; }
  .btn.ghost { color: #8f98a0; }
  .msg { color: #8f98a0; font-size: 13px; margin-bottom: 14px; }
  body:has(#adminManage) { align-items: flex-start; padding: 40px 16px; }
  body:has(#adminManage) .box { max-width: 620px; text-align: left; }
  #adminManage h2 { font-size: 15px; color: #fff; margin: 24px 0 10px; border-top: 1px solid #223142; padding-top: 18px; }
  #adminManage label { display: block; font-size: 12px; color: #8f98a0; margin: 10px 0 4px; }
  #adminManage input[type=text], #adminManage textarea, #adminManage select { width: 100%; padding: 9px 11px; font-size: 14px; border-radius: 6px; border: 1px solid #2a475e; background: #16202d; color: #fff; margin: 0; }
  #adminManage textarea { resize: vertical; min-height: 60px; }
  #adminManage input[type=color] { width: 46px; height: 34px; padding: 2px; border: 1px solid #2a475e; border-radius: 6px; background: #16202d; vertical-align: middle; }
  #adminManage .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  #adminManage .ann-item { background: #16202d; border: 1px solid #2a475e; border-radius: 6px; padding: 10px 12px; margin-top: 8px; }
  #adminManage .ann-item .t { color: #fff; font-size: 13px; font-weight: 600; }
  #adminManage .ann-item .m { color: #8f98a0; font-size: 12px; margin: 2px 0 6px; }
  #adminManage .exp { color: #66758a; font-size: 11px; }
  #adminManage .mini { padding: 5px 10px; font-size: 12px; }
  #adminManage .swatch { display: inline-block; width: 16px; height: 16px; border-radius: 3px; vertical-align: middle; border: 1px solid #2a475e; }
</style>
</head>
<body>
  <div class="box">
    <div class="brand">SteamSifter</div>
    <h1>Owner sign-in</h1>
    {{BODY}}
  </div>
</body>
</html>"""


def _relative_expiry(ts):
    """A short human label for when something expires (e.g. "expires in 5 d")."""
    if not ts:
        return "no expiry"
    secs = int(ts - time.time())
    if secs <= 0:
        return "expired"
    if secs < 3600:
        return f"expires in {secs // 60} min"
    if secs < 86400:
        return f"expires in {secs // 3600} h"
    return f"expires in {secs // 86400} d"


# Duration menus (seconds). 0 on the theme menu means "until I remove it".
_ANN_DURATIONS = [(3600, "1 hour"), (86400, "1 day"), (259200, "3 days"),
                  (604800, "1 week"), (1209600, "2 weeks"), (2592000, "1 month")]
_THEME_DURATIONS = [(604800, "1 week"), (1209600, "2 weeks"),
                    (2592000, "1 month"), (0, "Until I remove it")]


def _duration_options(pairs, default):
    """Build <option> tags for a duration <select>."""
    out = ""
    for secs, label in pairs:
        sel = " selected" if secs == default else ""
        out += f'<option value="{secs}"{sel}>{label}</option>'
    return out


def _admin_page(message, signed_in=False):
    """Render the admin page with a message and either a form or the owner tools."""
    note = f'<div class="msg">{message}</div>' if message else ''
    if not signed_in:
        body = (note +
                '<form method="post">'
                '<input type="password" name="password" placeholder="Owner password" autofocus>'
                '<button class="btn" type="submit">Sign in</button>'
                '</form>')
        return Response(ADMIN_PAGE.replace("{{BODY}}", body), mimetype="text/html")

    import training_data

    # Active announcements, each with a delete button.
    ann_rows = ""
    for a in store.announcements_active():
        ann_rows += (
            '<div class="ann-item">'
            f'<div class="t">{html.escape(a.get("title", ""))}</div>'
            f'<div class="m">{html.escape(a.get("message", ""))}</div>'
            '<div class="row">'
            f'<span class="exp">{_relative_expiry(a.get("expires_at"))}</span>'
            '<form method="post" action="/admin/announce/delete" style="margin:0">'
            f'<input type="hidden" name="id" value="{html.escape(a.get("id", ""))}">'
            '<button class="btn ghost mini" type="submit">Delete</button></form>'
            '</div></div>'
        )
    if not ann_rows:
        ann_rows = '<div class="msg">No active announcements.</div>'

    # Current seasonal theme status.
    theme = store.theme_active()
    if theme:
        theme_status = (
            '<div class="ann-item"><div class="row">'
            f'<span class="swatch" style="background:{html.escape(theme.get("grad_top", ""))}"></span>'
            f'<span class="swatch" style="background:{html.escape(theme.get("grad_bottom", ""))}"></span>'
            f'<span class="exp">Active &middot; {_relative_expiry(theme.get("expires_at"))}</span>'
            '<form method="post" action="/admin/theme/clear" style="margin:0">'
            '<button class="btn ghost mini" type="submit">Remove theme</button></form>'
            '</div></div>'
        )
    else:
        theme_status = '<div class="msg">No seasonal theme active.</div>'

    body = (
        '<div id="adminManage">'
        + note
        + f'<div class="msg">Training samples collected: {training_data.count():,}</div>'
        '<a class="btn" href="/admin/training.jsonl">Download training data</a> '
        '<a class="btn" href="/admin/logout">Sign out</a> '
        '<a class="btn ghost" href="/">Back to site</a>'

        '<h2>Home-page announcements</h2>'
        + ann_rows +
        '<form method="post" action="/admin/announce">'
        '<label>Title</label>'
        '<input type="text" name="title" maxlength="120" required>'
        '<label>Message</label>'
        '<textarea name="message" maxlength="600" required></textarea>'
        '<label>Show for</label>'
        '<select name="duration">' + _duration_options(_ANN_DURATIONS, 86400) + '</select>'
        '<div style="margin-top:12px"><button class="btn" type="submit">Post announcement</button></div>'
        '</form>'

        '<h2>Seasonal background gradient</h2>'
        + theme_status +
        '<form method="post" action="/admin/theme">'
        '<div class="row">'
        '<label style="margin:0">Top</label><input type="color" name="grad_top" value="#0f2c4a">'
        '<label style="margin:0">Bottom</label><input type="color" name="grad_bottom" value="#a85c3e">'
        '</div>'
        '<label>Duration</label>'
        '<select name="duration">' + _duration_options(_THEME_DURATIONS, 1209600) + '</select>'
        '<div style="margin-top:12px"><button class="btn" type="submit">Apply theme</button></div>'
        '</form>'
        '</div>'
    )
    return Response(ADMIN_PAGE.replace("{{BODY}}", body), mimetype="text/html")


@app.route("/admin", methods=["GET", "POST"])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"], exempt_when=is_admin)
def admin():
    """Owner login. A correct password sets a signed admin session cookie."""
    if request.method == "POST":
        if not ADMIN_PASSWORD:
            return _admin_page("Admin login is not configured on this server.")
        if compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["admin"] = True
            session.permanent = True
            return redirect("/")
        return _admin_page("Incorrect password.")
    if is_admin():
        return _admin_page("You are signed in as the owner.", signed_in=True)
    return _admin_page("")


@app.route("/admin/logout")
def admin_logout():
    """Clear the admin session."""
    session.pop("admin", None)
    return redirect("/")


@app.route("/admin/announce", methods=["POST"])
def admin_announce():
    """Owner: post a home-page announcement."""
    if not is_admin():
        return redirect("/admin")
    title = (request.form.get("title") or "").strip()[:120]
    message = (request.form.get("message") or "").strip()[:600]
    try:
        ttl = int(request.form.get("duration") or "86400")
    except ValueError:
        ttl = 86400
    if title and message:
        store.announcement_add(title, message, ttl)
    return redirect("/admin")


@app.route("/admin/announce/delete", methods=["POST"])
def admin_announce_delete():
    """Owner: remove one announcement by id."""
    if not is_admin():
        return redirect("/admin")
    store.announcement_delete((request.form.get("id") or "").strip())
    return redirect("/admin")


@app.route("/admin/theme", methods=["POST"])
def admin_theme():
    """Owner: set the seasonal background gradient (validated hex colors)."""
    if not is_admin():
        return redirect("/admin")
    top = (request.form.get("grad_top") or "").strip()
    bottom = (request.form.get("grad_bottom") or "").strip()
    try:
        ttl = int(request.form.get("duration") or "0")
    except ValueError:
        ttl = 0
    if _HEX_RE.match(top) and _HEX_RE.match(bottom):
        store.theme_set(top, bottom, ttl or None)
    return redirect("/admin")


@app.route("/admin/theme/clear", methods=["POST"])
def admin_theme_clear():
    """Owner: turn the seasonal theme off."""
    if not is_admin():
        return redirect("/admin")
    store.theme_clear()
    return redirect("/admin")


@app.route("/admin/training.jsonl")
def admin_training():
    """Download the collected (text, label) training dataset (owner only)."""
    if not is_admin():
        return redirect("/admin")
    import training_data
    return Response(
        training_data.export_jsonl(),
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=training_samples.jsonl"})


if __name__ == "__main__":
    # Local dev runs over plain http, so a Secure cookie would never be set and
    # admin login would silently fail; relax it only here.
    app.config["SESSION_COOKIE_SECURE"] = False
    # threaded=True lets the progress endpoint respond while a job runs locally.
    # Debug/Werkzeug console is off unless explicitly opted in (never in prod,
    # which runs via gunicorn and does not execute this block anyway).
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True, port=5000)
