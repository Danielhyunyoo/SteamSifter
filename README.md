# SteamSifter

**AI-powered review intelligence for game studios.** Turn Steam reviews into a ranked, themed list of what to fix and what your players love.

> Status: live and being built incrementally. Try it: https://steamsifter.onrender.com

---

## The Problem

Steam games can collect up to thousands of reviews, mixing genuine bug reports, feature praise, jokes, off-topic rants, and review-bombing. Reading all of that manually to answer "What should we fix?" and "What do our players want more of?" is a slow and inconsistent task.

SteamSifter takes a game, pulls its reviews automatically, filters the noise, and returns two ranked dashboards: issues to fix (by impact) and praised features to double down on (by frequency and sentiment).

## How It Works

1. **Search and resolve:** type a game name and SteamSifter resolves it to a Steam app ID.
2. **Ingest:** fetches the most recent reviews (a few hundred) from Steam's free public API. Each review carries useful metadata: a recommend / not-recommend flag, helpful-vote count, reviewer playtime, language, and date.
3. **Filter for signal:** a relevance classifier separates constructive product feedback from off-topic noise, jokes, and review-bomb spam.
4. **Classify:** each constructive review is tagged with sentiment and a category (bug, performance, gameplay, cheating, monetization, UI/UX, content, community, or praise) using structured model output for consistency.
5. **Theme:** a two-pass step discovers specific, named themes and assigns every review to one, done separately for the negative and positive sides so the report can show both.
6. **Rank by impact:** themes are scored by summed behavioral weight, `1 + log10(1 + playtime_hours) + log10(1 + helpful_votes)` per review, so issues raised by experienced, upvoted players outrank low-effort rage reviews.
7. **Present:** a dashboard with a summary scoreboard, a sentiment donut, a sentiment-over-time trend, an Issues / Praise toggle, ranked theme cards (each with a High/Med/Low impact chip and clickable example quotes that link back to the real Steam review), and a low-signal noise summary.

Classification and theming run as concurrent batches, so a first-time analysis of a few hundred reviews completes in well under a minute. Results are cached per game and persisted, so repeat lookups are instant and free. A game can be re-analyzed by anyone once it has gained roughly 20% more reviews, and by the owner at any time.

> NOTE TO DEVELOPERS: "impact" is an inferred heuristic (frequency, sentiment, playtime, helpful-votes), not ground truth. It is presented as an informed estimate. Take that as you will.

## Tech Stack

- **Reviews and metadata:** Steam public `appreviews` API (free, no key), plus `storesearch` for game-name to app-ID lookups.
- **AI:** OpenAI (`gpt-4.1-mini`) by default, swappable to Google Gemini (free tier) via the `LLM_PROVIDER` env var. Structured JSON output is enforced with Pydantic schemas, over batched classification and a two-pass theming step, with batches run concurrently across a thread pool to keep analysis fast.
- **Web app:** Flask, served in production by gunicorn. Background worker threads with an in-memory job registry power the live progress bar; flask-limiter applies per-visitor rate limits; werkzeug ProxyFix gives correct client IPs behind Render's proxy.
- **Frontend:** server-rendered HTML and CSS in a Steam-inspired theme. Chart.js for the category donut and sentiment-over-time trend, inline SVG for the scoreboard and icons, a Web Audio completion chime, and a fully responsive (mobile-friendly) layout.
- **Persistence:** Upstash Redis (TLS) caches analyses so they survive redeploys, with a local JSON-file fallback for development.
- **Deployment and config:** Render (`render.yaml` blueprint and `Procfile`), configured entirely through environment variables (`OPENAI_API_KEY`, `REDIS_URL`, `ADMIN_PASSWORD`, `SECRET_KEY`, `LLM_PROVIDER`).

## Roadmap

- [x] **Minimum Viable Product:** one game, a few hundred recent negative reviews; classify, theme, and rank by frequency; basic list view
- [x] **V2:** behavioral impact weighting (playtime + helpful-votes), noise filter, representative quotes, sentiment charts, positive "Double Down" view
- [x] **V3:** web app with game-name search, one-pass analysis with a Fix These / Double Down toggle, live progress bar, per-game caching, and a Steam-styled UI
- [x] **V4:** UI animations (overview + review bars, toggle slider); home-page links (GitHub, About, Steam, LinkedIn) with a clear Valve/Steam non-affiliation notice; clickable source links on every shown review
- [x] **V5:** live public deployment on Render with a production stack (gunicorn, rate limiting, proxy-aware); persistent Redis cache; summary scoreboard, category donut, and sentiment-over-time trend; per-review recommend/not-recommend badges; impact shown as High/Med/Low with an explainer; toggle renamed to Issues / Praise; owner-gated re-analyze that unlocks for the public after ~20% review growth; numeric progress counter with a completion chime; mobile-responsive across all pages; concurrent batch processing that cut a fresh analysis from ~3 minutes to under a minute
- [ ] **V6:** scale and hardening for real traffic, same-game request de-duplication (one shared job per game), background-job cleanup, and a scale-to-thousands pipeline (embed once, label clusters, propagate sample labels) to handle far more than a few hundred reviews
- [ ] **Later:** multi-worker support, exportable reports (PDF/CSV), and review filters

## Current Limitations

SteamSifter is deployed and open to anyone, but it is still a solo project tuned for light traffic: a single worker on a free Render instance sharing one AI key. Thanks to concurrent batch processing, a fresh analysis of a game's most recent ~300 reviews now finishes in well under a minute, and results are cached so popular titles are only re-analyzed once enough new reviews accumulate. Scaling to many simultaneous users, or to thousands of reviews per game, is the next milestone (V6).

As of June 18, 2026, SteamSifter runs on the OpenAI API (`gpt-4.1-mini`) and can switch to free-tier Gemini when needed.

## Disclaimer

SteamSifter is an independent project and is not affiliated with, endorsed by, or sponsored by Valve Corporation or Steam. "Steam" and related marks are trademarks of Valve Corporation. All review content belongs to its original authors and is shown for analysis and attribution purposes.
