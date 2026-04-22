# Playtomic Shoreditch Checker (Docker + GitHub deploy)

A production-friendly checker that:
- reads Playtomic availability **without browser automation**,
- filters to out-of-hours slots (outside 08:00–18:00 London time by default),
- deduplicates unchanged alerts,
- posts updates to a Telegram channel/group/chat.

## Why this is stable
It uses HTTP endpoints, not browser tooling:
- `https://api.playtomic.io/v1/tenants/{tenant_id}`
- `https://api.playtomic.io/v1/tenants/{tenant_id}/resources`
- `https://playtomic.com/api/clubs/availability?...`

---

## Project layout

- `app/main.py` — checker and Telegram sender
- `Dockerfile` — container image
- `docker-compose.yml` — long-running 30m loop
- `.env.example` — config template
- `deploy.sh` — pull/build/restart on server
- `.github/workflows/deploy.yml` — deploy on push to `main`

---

## Local run (quick test)

```bash
cd projects/playtomic_checker
cp .env.example .env
# edit .env with your real TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

# dry-run first
sed -i 's/DRY_RUN=false/DRY_RUN=true/' .env
python3 app/main.py
```

---

## Docker run

```bash
cd projects/playtomic_checker
cp .env.example .env
# edit .env

docker compose up --build -d
```

Logs:
```bash
docker compose logs -f
```

---

## Deploy from GitHub on push to main

1. Create GitHub repo and push this folder.
2. On server, clone repo to e.g. `~/playtomic-checker`.
3. Create `~/playtomic-checker/.env` with real values.
4. Add GitHub Actions secrets:
   - `DEPLOY_HOST`
   - `DEPLOY_USER`
   - `DEPLOY_SSH_KEY`
   - `DEPLOY_APP_DIR` (e.g. `/home/ubuntu/playtomic-checker`)
5. Push to `main`.

Workflow runs `deploy.sh`, which does:
- `git pull`
- `docker compose build --pull`
- `docker compose up -d`

---

## Telegram target
Use `TELEGRAM_CHAT_ID` for your channel/group/chat.
- Channels usually look like `-100...`
- Bot must be admin in that channel to post.

---

## Notes
- State is persisted in `./state/state.json` to avoid duplicate alerts.
- Default interval is 1800s (30 min), configurable via `CHECK_INTERVAL_SECONDS`.
