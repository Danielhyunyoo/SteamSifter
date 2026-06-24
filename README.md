# SteamSifter

**AI-powered review intelligence for game studios.** Turn Steam reviews into a ranked, themed list of what to fix and what your players love.

> Status: live and being built incrementally. Try it: https://steamsifter.com

---

## The Problem

Steam games can collect up to thousands of reviews, mixing genuine bug reports, feature praise, jokes, off-topic rants, and review-bombing. Reading all of that manually to answer "What should we fix?" and "What do our players want more of?" is a slow and inconsistent task.

SteamSifter takes a game, pulls its reviews automatically, filters the noise, and returns two ranked dashboards: issues to fix (by impact) and praised features to double down on (by frequency and sentiment).

## How It Works

1. **Search and resolve:** type a game name and SteamSifter resolves it to a Steam app ID.
2. **Ingest:** fetches the most recent reviews (up to ~1,000) from Steam's free public API. Each review carries useful metadata: a recommend / not-recommend flag, helpful-vote count, reviewer playtime, language, and date.
3. **Filter for signal:** a relevance classifier separates constructive product feedback from off-topic noise, jokes, and review-bomb spam.
4. **Classify:** each constructive review is tagged with sentiment and a category (bug, performance, gameplay, cheating, monetization, UI/UX, content, community, or praise) using structured model output for consistency.
5. **Theme:** reviews are grouped into specific, named themes for each side. For typical volumes an LLM pass discovers and assigns themes; for large review sets SteamSifter instead embeds every review and clusters them with k-means, labeling each cluster in one call, and it picks the method automatically by volume.
6. **Rank by impact:** themes are scored by summed behavioral weight, `1 + log10(1 + playtime_hours) + log10(1 + helpful_votes)` per review, so issues raised by experienced, upvoted players outrank low-effort rage reviews.
7. **Present:** a dashboard with a summary scoreboard, a sentiment donut, a sentiment-over-time trend, an Issues / Praise toggle, ranked theme cards (each with a High/Med/Low impact chip and clickable example quotes that link back to the real Steam review, each showing the reviewer's avatar and name with an English translation for non-English reviews), a low-signal noise summary, live filters (by recommendation, playtime, and language), and a print / save-as-PDF export.

Classification and theming run as concurrent batches, so a first-time analysis takes roughly one to a few minutes depending on review volume. Results are cached per game (for 30 days, and refreshed sooner when reviews grow) and persisted, so repeat lookups are instant and free. A game can be re-analyzed by anyone once it has gained roughly 20% more reviews, and by the owner at any time.

> NOTE TO DEVELOPERS: "impact" is an inferred heuristic (frequency, sentiment, playtime, helpful-votes), not ground truth. It is presented as an informed estimate. Take that as you will.

## Tech Stack

- **Reviews and search:** Steam's public `appreviews` API (free, no key) for reviews, plus the storefront search-suggest endpoint (games-filtered) for game-name to app-ID lookups that surface niche titles instead of burying them under another game's DLC.
- **AI and ML:** OpenAI (`gpt-4.1-mini`) by default, swappable to Google Gemini (free tier) via `LLM_PROVIDER`. Structured JSON output is enforced with Pydantic schemas over batched classification, run concurrently across a thread pool. Theming uses either an LLM discover-and-assign pass or, at scale, OpenAI `text-embedding-3-small` embeddings clustered with scikit-learn k-means (plus PCA), auto-selected by review volume; near-duplicate themes are then consolidated by a cosine-centroid merge and a final LLM dedupe pass.
- **Web app:** Flask served in production by gunicorn. Background worker threads power the live progress bar; a Redis-backed job store de-duplicates concurrent requests for the same game and makes the app multi-worker ready; flask-limiter applies per-visitor rate limits; werkzeug ProxyFix gives correct client IPs behind Render's proxy.
- **Frontend:** server-rendered HTML and CSS in a Steam-inspired, fully responsive theme. Chart.js for the category donut and sentiment-over-time trend, inline SVG for the scoreboard and icons, a Web Audio completion chime, a Steam-style overall rating badge, animated stat count-ups, live client-side review filters (recommendation, playtime, language) that recompute the whole dashboard without re-analyzing, and a print / save-as-PDF view with its own light theme.
- **Enrichment:** English translations for non-English example quotes, plus reviewer avatars and usernames via Steam's `GetPlayerSummaries` (optional `STEAM_API_KEY`).
- **Sharing and SEO:** dynamic OpenGraph / Twitter share cards rendered as 1200x630 images with Pillow (a blurred game banner, the rating, and the top praise / top fix), cache-busted per analysis, plus meta descriptions, so shared links unfurl into rich previews.
- **Security:** strict app-ID validation (blocks path traversal into cache files and upstream calls), a timing-safe, rate-limited admin login with Secure / HttpOnly / SameSite cookies, defense-in-depth response headers (Content-Security-Policy, X-Frame-Options, nosniff, Referrer-Policy), and DOM-safe rendering of all external text.
- **Persistence:** Upstash Redis (TLS) caches analyses (a 30-day backstop) so they survive redeploys, with a local JSON-file fallback for development.
- **Deployment and config:** Render (`render.yaml` blueprint and `Procfile`) on the custom domain `steamsifter.com` (Cloudflare DNS), configured entirely through environment variables (`OPENAI_API_KEY`, `REDIS_URL`, `ADMIN_PASSWORD`, `SECRET_KEY`, `LLM_PROVIDER`, plus optional `STEAM_API_KEY`, `THEME_METHOD`, `MAX_REVIEWS`, `MAX_AGE_DAYS`, `WEB_CONCURRENCY`).

## Roadmap

- [x] **Minimum Viable Product:** one game, a few hundred recent negative reviews; classify, theme, and rank by frequency; basic list view
- [x] **V2:** behavioral impact weighting (playtime + helpful-votes), noise filter, representative quotes, sentiment charts, positive "Double Down" view
- [x] **V3:** web app with game-name search, one-pass analysis with a Fix These / Double Down toggle, live progress bar, per-game caching, and a Steam-styled UI
- [x] **V4:** UI animations (overview + review bars, toggle slider); home-page links (GitHub, About, Steam, LinkedIn) with a clear Valve/Steam non-affiliation notice; clickable source links on every shown review
- [x] **V5:** live public deployment on Render with a production stack (gunicorn, rate limiting, proxy-aware); persistent Redis cache; summary scoreboard, category donut, and sentiment-over-time trend; per-review recommend/not-recommend badges; impact shown as High/Med/Low with an explainer; toggle renamed to Issues / Praise; owner-gated re-analyze that unlocks for the public after ~20% review growth; numeric progress counter with a completion chime; mobile-responsive across all pages; concurrent batch processing that cut a fresh analysis from ~3 minutes to under a minute
- [x] **V6:** scale and hardening, same-game request de-duplication (one shared job per game) and background-job cleanup; a scalable theming path that embeds reviews and clusters them with k-means (auto-selected by volume, configurable up to ~1,500 reviews); English translations for non-English quotes; reviewer avatars and usernames on quotes; and a 30-day cache backstop
- [x] **V7:** multi-worker readiness (Redis-shared job store and rate limiting); print / save-as-PDF export with a light theme; live client-side review filters (recommendation, playtime, language); near-duplicate theme consolidation (centroid merge plus an LLM dedupe pass); a games-filtered search that surfaces niche titles over DLC; dynamic OpenGraph / Twitter share cards with SEO meta tags; a custom domain (`steamsifter.com` via Cloudflare); and a security pass (app-ID validation, hardened admin auth, response headers, DOM-XSS fixes)
- [ ] **Later:** higher-concurrency hosting, a faster sub-minute analysis path at full review volume, and studio-facing extras (comparing games, tracking a title over time)

## Current Limitations

SteamSifter is deployed and open to anyone, but it is still a solo project tuned for light traffic: it runs a single worker on a free Render instance sharing one AI key, though the code is already multi-worker ready and just needs paid hosting to scale out. A first-time analysis takes roughly one to two minutes depending on volume; concurrent batching and a scalable embedding-based theming path keep it bounded up to ~1,000 reviews, and results are cached so popular titles are only re-analyzed once their reviews grow. The main remaining limit is raw throughput on the free tier, which is a hosting choice rather than a code limit.

As of June 24, 2026, SteamSifter runs on the OpenAI API (`gpt-4.1-mini`) and can switch to free-tier Gemini when needed.

## Disclaimer

SteamSifter is an independent project and is not affiliated with, endorsed by, or sponsored by Valve Corporation or Steam. "Steam" and related marks are trademarks of Valve Corporation. All review content belongs to its original authors and is shown for analysis and attribution purposes.
