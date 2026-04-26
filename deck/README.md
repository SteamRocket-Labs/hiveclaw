# Hive · VC Pitch Deck

Standalone Railway service serving the VC pitch deck as a static HTML page.

## What's here

| File | Purpose |
|------|---------|
| `index.html` | The deck (single-file HTML, copied from `docs/VC_BRIEF.html`) |
| `Caddyfile` | Caddy config: serves `/srv`, gzip, noindex headers, 5-min cache |
| `Dockerfile` | `caddy:2-alpine` + Caddyfile + index.html (~30MB image) |
| `railway.json` | Railway build config (Dockerfile-based) |

## Deploy on Railway

This is a **separate service** in the existing Hive Railway project (`Paimon.Finance`).

**One-time setup** (Railway dashboard):

1. Open the Hive project → New Service → Deploy from GitHub repo
2. Pick `rocky2431/hiveclaw`
3. Settings → Service → set **Root Directory** to `deck`
4. Settings → Networking → **Generate Domain** (gives you `*.up.railway.app`)
5. (Optional) Custom domain → e.g. `deck.hive.app`

After this, every push to `main` that changes files under `deck/` triggers an automatic redeploy.

## Updating the deck

1. Edit `docs/VC_BRIEF.md` (source of truth)
2. Regenerate `docs/VC_BRIEF.html` (or copy it manually):
   ```bash
   cp docs/VC_BRIEF.html deck/index.html
   ```
3. Commit + push → Railway auto-deploys in ~1 min

## Local preview

```bash
# Just open the file
open deck/index.html

# Or run Caddy locally to test the actual prod stack
docker build -t hive-deck deck/
docker run -p 8080:80 hive-deck
# → http://localhost:8080
```

## Notes

- Image is ~30MB (Caddy alpine), cold start <1s
- Search engines blocked via `X-Robots-Tag: noindex, nofollow`
- Anyone with the URL can view — no auth
- For per-VC tracking, append a query token (e.g. `/?k=tier1-a16z`) and read from JS analytics
