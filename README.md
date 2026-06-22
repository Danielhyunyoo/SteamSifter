# SteamSifter

**AI-powered review intelligence for game studios.** Turn Steam reviews into a ranked, themed list of what to fix and what your players love.

> Status: live and being built incrementally. Try it: https://steamsifter.com

---

## The Problem

Steam games can collect up to thousands of reviews, mixing genuine bug reports, feature praise, jokes, off-topic rants, and review-bombing. Reading all of that manually to answer "What should we fix?" and "What do our players want more of?" is a slow and inconsistent task.

SteamSifter takes a game, pulls its reviews automatically, filters the noise, and returns two ranked dashboards: issues to fix (by impact) and praised features to double down on (by frequency and sentiment).

## How It Works

1. **Search and resolve:** type a game name and SteamSifter resolves it to a Steam app ID.
2. **Ingest:** fetches the most recent reviews (a few hundred) from Steam's free public API. Each review carries useful metadata: a recommend / not-recommend flag, helpful-vote count, reviewer playtime, language, and date.
3. **Filter for signal:** a relevance classifier separates constructive product feedback from off-topic noise, jokes, and review-bomb spam.
4. **Classify:** each constructive review is tagged with sentiment and a category (bug, performance, gameplay, cheating, monetization, UI/UX, content, community, or praise) using structured model output for consistency.
5. **Theme:** reviews are grouped into specific, named themes for each side. For typical volumes an LLM pass discovers and assigns themes; for large review sets SteamSifter instead embeds every review and clusters them with k-means, labeling each cluster in one call, and it picks the method automatically by volume.
6. **Rank by impact:** themes are scored by summed behavioral weight, `1 + log10(1 + playtime_hours) + log10(1 + helpful_votes)` per review, so issues raised by experienced, upvoted players outrank low-effort rage reviews.
7. **Present:** a dashboard with a summary scoreboard, a sentiment donut, a sentiment-over-time trend, an Issues / Praise toggle, ranked theme cards (each with a High/Med/Low impact chip and clickable example quotes that link back to the real Steam review, each showing the reviewer's avatar and name with an English translation for non-English reviews), and a low-signal noise summary.

Classification and theming run as concurrent batches, so a first-time analysis takes roughly one to a few minutes depending on review volume. Results are cached per game (for 30 days, and refreshed sooner when reviews grow) and persisted, so repeat lookups are instant and free. A game can be re-analyzed by anyone once it has gained roughly 20% more reviews, and by the owner at any time.

> NOTE TO DEVELOPERS: "impact" is an inferred heuristic (frequency, sentiment, playtime, helpful-votes), not ground truth. It is presented as an informed estimate. Take that as you will.

## Tech Stack

- **Reviews and metadata:** Steam public `appreviews` API (free, no key), plus `storesearch` for game-name to app-ID lookups.
- **AI:** OpenAI (`gpt-4.1-mini`) by default, swappable to Google Gemini (free tier) via the `LLM_PROVIDER` env var. Structured JSON output is enforced with Pydantic schemas, over batched classification, run concurrently across a thread pool to keep analysis fast. Theming uses either an LLM pass or, at scale, OpenAI embeddings with scikit-learn k-means clustering, auto-selected by review volume.
- **Web app:** Flask, served in production by gunicorn. Background worker threads with an in-memory job registry power the live progress bar; flask-limiter applies per-visitor rate limits; werkzeug ProxyFix gives correct client IPs behind Render's proxy.
- **Frontend:** server-rendered HTML and CSS in a Steam-inspired theme. Chart.js for the category donut and sentiment-over-time trend, inline SVG for the scoreboard and icons, a Web Audio completion chime, and a fully responsive (mobile-friendly) layout.
- **Enrichment:** English translations for non-English example quotes, plus reviewer avatars and usernames via Steam's `GetPlayerSummaries` (optional `STEAM_API_KEY`).
- **Persistence:** Upstash Redis (TLS) caches analyses so they survive redeploys, with a local JSON-file fallback for development.
- **Deployment and config:** Render (`render.yaml` blueprint and `Procfile`), configured entirely through environment variables (`OPENAI_API_KEY`, `REDIS_URL`, `ADMIN_PASSWORD`, `SECRET_KEY`, `LLM_PROVIDER`, plus optional `STEAM_API_KEY`, `THEME_METHOD`, `MAX_REVIEWS`, `MAX_AGE_DAYS`).

## Roadmap

- [x] **Minimum Viable Product:** one game, a few hundred recent negative reviews; classify, theme, and rank by frequency; basic list view
- [x] **V2:** behavioral impact weighting (playtime + helpful-votes), noise filter, representative quotes, sentiment charts, positive "Double Down" view
- [x] **V3:** web app with game-name search, one-pass analysis with a Fix These / Double Down toggle, live progress bar, per-game caching, and a Steam-styled UI
- [x] **V4:** UI animations (overview + review bars, toggle slider); home-page links (GitHub, About, Steam, LinkedIn) with a clear Valve/Steam non-affiliation notice; clickable source links on every shown review
- [x] **V5:** live public deployment on Render with a production stack (gunicorn, rate limiting, proxy-aware); persistent Redis cache; summary scoreboard, category donut, and sentiment-over-time trend; per-review recommend/not-recommend badges; impact shown as High/Med/Low with an explainer; toggle renamed to Issues / Praise; owner-gated re-analyze that unlocks for the public after ~20% review growth; numeric progress counter with a completion chime; mobile-responsive across all pages; concurrent batch processing that cut a fresh analysis from ~3 minutes to under a minute
- [x] **V6:** scale and hardening, same-game request de-duplication (one shared job per game) and background-job cleanup; a scalable theming path that embeds reviews and clusters them with k-means (auto-selected by volume, configurable up to ~1,500 reviews); English translations for non-English quotes; reviewer avatars and usernames on quotes; and a 30-day cache backstop
- [ ] **Later:** multi-worker support, exportable reports (PDF/CSV), and review filters

## Current Limitations

SteamSifter is deployed and open to anyone, but it is still a solo project tuned for light traffic: a single worker on a free Render instance sharing one AI key. A first-time analysis takes about one to a few minutes depending on volume (concurrent batching plus a scalable embedding-based theming path keep it bounded up to ~1,500 reviews), and results are cached so popular titles are only re-analyzed when their reviews grow. The main remaining limit is concurrency: many simultaneous users would contend for the single worker and shared key, which is the next milestone (multi-worker support).

As of June 18, 2026, SteamSifter runs on the OpenAI API (`gpt-4.1-mini`) and can switch to free-tier Gemini when needed.

## Disclaimer

SteamSifter is an independent project and is not affiliated with, endorsed by, or sponsored by Valve Corporation or Steam. "Steam" and related marks are trademarks of Valve Corporation. All review content belongs to its original authors and is shown for analysis and attribution purposes.
