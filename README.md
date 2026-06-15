# SteamSifter

**AI-powered review intelligence for Game Studios.** Turn Steam reviews into a ranked, themed list of what to fix and what your players love.

> Status: early development. Being built incrementally.

---

## The Problem

Steam games can collect up to thousands of reviews, mixing genuine bug reports, feature praise, jokes, off-topic rants, and review-bombing. Reading all of that manually to answer "What should we fix?" and "What do our players want more of?" is going to be a slow and inconsistent task.

SteamSifter will enter a game's app ID, pulls its reviews automatically, filters the noise, and returns two ranked dashboards: bugs and issues to fix (by order impact) and praised features to double down on (by frequency and sentiment).

## How It Works

1. **Ingest:** fetches reviews directly from Steam's free public review API, looking through the full set. Each review includes useful metadata: positive/negative flag, helpful-vote count, and the reviewer's playtime in the game.
2. **Filter for signal:** a relevance classifier separates constructive product feedback from off-topic noise, jokes, and review-bomb spam. This is signal-vs-noise filtering.
3. **Classify:** each review is tagged with sentiment, a category (bug, performance, feature request, praise, UX, pricing, etc.), and severity, using structured model output for consistency.
4. **Cluster:** reviews describing the same issue are grouped into themes.
5. **Rank by impact:** themes are sorted by frequency plus behavioral weight, so an issue reported by long-playtime, highly-upvoted reviewers ranks above low-effort rage reviews.
6. **Present:** a dashboard with a *Fix These* view and a *Double Down* view, including counts, sentiment charts, and representative quotes.

> NOTE TO DEVELOPERS: "impact" is an inferred heuristic (frequency, sentiment, playtime, helpful-votes), not ground truth. It is presented as an informed estimate. Take that as you will.

## Tech stack

- **Reviews:** Steam public appreviews API (free, no key)
- **AI:** free-tier LLM inference (i.e. Gemini free tier or Groq), with structured/JSON output; embeddings for clustering at scale
- **Frontend:** web UI with charting for sentiment and theme visuals
- **Backend:** batched review processing and per-app caching

## Roadmap

- [x] **Minimum Viable Product:** one game, a few hundred recent negative reviews; classify, theme, and rank by frequency; basic list view
- [ ] **V2:** behavioral impact weighting (playtime + helpful-votes), noise filter, representative quotes, sentiment charts, positive "Double Down" view
- [ ] **V3:** scale to thousands of reviews via batching and embeddings clustering, app ID search, exportable reports, filters, public deployment

## Disclaimer

SteamSifter is an independent project and is not affiliated with, endorsed by, or sponsored by Valve or Steam. "Steam" is a trademark of Valve Corporation.
