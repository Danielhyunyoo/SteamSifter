# Deploying SteamSifter (Render)

SteamSifter runs as a persistent Flask process, so use a "web service" host
(Render or Railway), not a serverless one.

## Render (recommended)

1. Push the repo to GitHub (already done).
2. Go to https://render.com, sign in with GitHub.
3. New + > Blueprint, pick the SteamSifter repo. Render reads `render.yaml`.
4. When prompted, set the secret `OPENAI_API_KEY` to your OpenAI key.
   (`LLM_PROVIDER=openai` is already set in the blueprint.)
5. Create the service and wait for the build. You get a public URL like
   `https://steamsifter.onrender.com`.

## Notes
- Free tier spins down when idle, so the first visit after a while is slow
  (cold start), then fast.
- The review cache lives on the local filesystem, which resets on each deploy
  on the free tier. A small paid disk would make it persistent.
- Per-visitor rate limits protect the shared API key (10 analyses/hour each).
- Single worker for now (in-memory progress state); heavier concurrency is
  future work.

## Run locally instead
    pip install -r requirements.txt
    python src/app.py        # http://127.0.0.1:5000
