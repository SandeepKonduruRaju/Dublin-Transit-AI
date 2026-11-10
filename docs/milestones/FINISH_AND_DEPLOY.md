# Dublin Transit AI: finish, deploy to Azure, test, live URL (Claude Code prompt)

Save as `docs/milestones/FINISH_AND_DEPLOY.md`. In Claude Code (repo root `C:\dev\dublin-transit-ai`):

> Read `docs/milestones/FINISH_AND_DEPLOY.md` and follow it phase by phase in build mode. Stop at every STOP and GATE and wait for me.

---

## Rules (apply to every phase)

- **Build mode:** one-line explanations while working; no teaching questions.
- **Inspect before editing:** run `git status` and `git log --oneline -5` first; read every file before changing it; never guess.
- **Git:** never run `commit`, `push`, `reset`, `restore`, `stash`, or delete files or tables unless I explicitly say so in chat. Ask at each STOP whether I want a commit.
- **PowerShell** on Windows. Run `uv run ...` from `backend\`, `npm ...` from `web\`, and `docker compose ...` from the repo root. Remote commands run over `ssh`.
- **Secrets:** never print, echo, log or commit any key or password (NTA, Anthropic, DB). Read them from `.env` only. On the server, `.env` gets `chmod 600`.
- **No account creation, logins, passwords or payment details by you.** Anything needing my browser or credentials is a **GATE**: tell me exactly what to do and wait.
- **Only one NTA poller at a time** (local *or* cloud, never both); wait 60 s between manual NTA calls.
- **Paid LLM calls** (eval runs, `/chat` tests) only after I confirm.
- **Honest status:** a step is done only when its checks pass. If something fails, stop, show the error, propose the smallest fix.
- **Pin versions:** exact versions at least 2 weeks old for npm packages, Docker images and the uv image.

---

## Phase 0: Where are we? (read-only)

Report, changing nothing:

```powershell
git status
git log --oneline -5
docker compose ps
Get-ChildItem backend\app, backend\sql, web -Name
```

Classify each as **done / partly done / not started**:

| Item | How to tell |
|---|---|
| Milestone 6 (eval + CI) | `backend/app/evals/`, `eval/cases.json`, `.github/workflows/` exist; last eval result in `eval/results/` |
| Milestone 6.5 (storage, refresh, rate limit) | `sql/005_rt_compact.sql`, `app/rt_store.py`, `app/static_refresh.py`, `app/rate_limit.py` exist; `static-refresh` service in Compose |
| Milestone 7 (chat UI) | `web/package.json` exists |
| Milestone 8 (deploy) | `docker-compose.prod.yml`, `deploy/` exist |

**STOP:** show the table.

---

## Phase 1: Finish Milestone 6.5 (only if not done)

Follow `docs/milestones/M6_5_production_readiness.md` in build mode, from the first unfinished step. Required before any public deploy:
- [ ] compact storage (upserts + daily summary) running in the local poller
- [ ] the `static-refresh` service running; `/health` shows `join_health.percent` ≥ 90
- [ ] `/chat` returns 429 with `Retry-After` over the limit
- [ ] all tests pass; oracle eval 100/100; the 15-case paid sample passes with safety 100% (after my OK)

**STOP:** report the results and the measured storage per day.

---

## Phase 2: Milestone 7, the chat UI (`web/`)

### Spec

- **Next.js** (current stable, App Router) + **TypeScript** + plain CSS modules (no UI kit needed). `output: 'standalone'` for a small Docker image.
- **One page, `/`:** a chat window.
  - Message list (user right, assistant left), an input box, a Send button, Enter to send.
  - Sends `POST /chat` with `{ message, history }` (keep the last 10 turns).
  - **Loading** state ("Checking live data…"), **error** state, and a friendly **429** message using `Retry-After` ("You've hit the limit; try again in N seconds").
  - Under each answer, small **source chips** from `tool_calls` ("live delays", "timetable", "reliability", "route check").
  - 4 **example question buttons**: "When is the next 39A from stop 767?", "Is the 39A running late right now?", "How reliable was route 15 this week?", "When is the next 46A?".
  - Footer: "Data: National Transport Authority (GTFS / GTFS-Realtime). Answers can be wrong; check TFI Live before you travel."
- **Same-origin calls:** the browser always calls `/chat` on its own origin.
  - **Local dev:** Next.js `rewrites` send `/chat` to `http://localhost:8001/chat`.
  - **Production:** Caddy routes `/chat` to the agent (Phase 3). No CORS needed.
- **Mobile-friendly:** works at 375 px wide.
- `web/Dockerfile`: multi-stage (deps → build → `node:<pinned LTS>-alpine` runtime running the standalone server on port 3000, as a non-root user).
- `web/.dockerignore`: `node_modules`, `.next`.
- Add `web` to `docker-compose.yml` (port 3000 locally).

### Checks

```powershell
cd web
npm ci
npm run lint
npm run build
cd ..
docker compose up -d --build web
```

Open `http://localhost:3000` and try the 4 example buttons (**paid calls; ask me first**).

- [ ] lint and build pass with no TypeScript errors
- [ ] the 39A question shows an answer with source chips
- [ ] the 46A question shows "not in the current timetable"
- [ ] spamming Send shows the friendly 429 message
- [ ] the layout works at 375 px (browser dev tools)
- [ ] add a `ci.yml` job: `npm ci && npm run lint && npm run build` in `web/`

**STOP:** screenshots or a description, plus the check results.

---

## Phase 3: Production config (still local)

1. **`docker-compose.prod.yml`** (an override used with `-f docker-compose.yml -f docker-compose.prod.yml`):
   - **No host ports** for `db`, `api` and `agent`; only Caddy is public.
   - `agent`: `TRUST_PROXY_HEADERS=true`, `CHAT_GLOBAL_PER_DAY=200` (tighter for launch).
   - Restart policy `unless-stopped` everywhere; log rotation (`json-file`, `max-size: 10m`, `max-file: 3`).
2. **`caddy` service** (`caddy:<pinned 2.x>`), ports 80 and 443, with volumes for its data and config (certificates persist).
3. **`deploy/Caddyfile`:**
   ```
   {$SITE_HOST} {
       encode gzip
       handle /chat* {
           reverse_proxy agent:8001
       }
       handle /health {
           reverse_proxy api:8000
       }
       handle {
           reverse_proxy web:3000
       }
   }
   ```
   `SITE_HOST` comes from `.env` on the server. `/docs` and the other API routes are **not** public.
4. **`deploy/deploy.ps1`:** packs the repo **without** `data/`, `.venv`, `node_modules`, `.next`, `eval/results` or `.env` (using `tar`), copies it to the VM with `scp`, unpacks it, and runs `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.
5. **`deploy/README.md`:** how to deploy, update, view logs, restart and roll back.

Check locally: `docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet`.

**STOP:** show the files.

---

## Phase 4: Azure (needs me)

### GATE A: my tasks before you start

1. Azure for Students is **active** (portal shows the subscription and credit).
2. Azure CLI installed, and I've run `az login` myself. You check with `az account show` (print only the name, never tokens).
3. I've set the **Anthropic monthly spend limit** in the console.
4. I confirm the **region** (default `northeurope`, Dublin) and the **VM size** (default `Standard_B2s`: 2 vCPU, 4 GB RAM), after checking its monthly cost against my credit in the Azure pricing page.

### Then you

```powershell
az group create -n rg-dublin-transit -l northeurope
az vm create -g rg-dublin-transit -n vm-dublin-transit --image Ubuntu2404 --size Standard_B2s --admin-username azureuser --generate-ssh-keys --public-ip-sku Standard --public-ip-address-dns-name <unique-label> --os-disk-size-gb 64
az vm open-port -g rg-dublin-transit -n vm-dublin-transit --port 80,443 --priority 1010
```

- **Restrict SSH (22) to my current public IP only**: look up the NSG rule name first, then set `--source-address-prefixes <my-ip>/32`.
- If the region or size is blocked by the student subscription policy, **STOP** and suggest the nearest allowed option.
- The live hostname is `<unique-label>.northeurope.cloudapp.azure.com`. No domain purchase is needed; Caddy gets a free HTTPS certificate for it.

### Server setup (over SSH)

- Install Docker Engine and the Compose plugin from **Ubuntu's or Docker's official apt repository**; enable Docker on boot; add `azureuser` to the `docker` group.
- Enable unattended security upgrades.
- Create `~/dublin-transit-ai/.env` from my local `.env` (copy it with `scp`, never print it), add `SITE_HOST=<hostname>`, then `chmod 600 .env`.

**STOP:** confirm the VM is reachable, Docker works and SSH is restricted.

---

## Phase 5: Deploy + load data

1. **Stop my local poller first** (`docker compose stop rt-poller`), so only one poller uses the NTA key.
2. Run `deploy/deploy.ps1`.
3. On the VM, apply all `backend/sql/0*.sql` migrations to the database (in order), then load the timetable:
   `docker compose ... run --rm static-refresh python -m app.static_refresh --once`
   (downloads about 170 MB; the load takes several minutes).
4. Check: `docker compose ... ps` shows all services up, `db` healthy and `caddy` running.

---

## Phase 6: Test the live site

Run from my laptop (paid `/chat` calls need my OK):

| # | Test | Pass when |
|---|---|---|
| 1 | `https://<host>/` | the chat page loads over HTTPS with a valid certificate |
| 2 | `https://<host>/health` | the static version is shown, `realtime_age_seconds` < 300, join health ≥ 90% |
| 3 | 39A delay question in the UI | a real answer with source chips |
| 4 | "When is the next 46A?" | "not in the current timetable", with no times |
| 5 | 12 rapid `/chat` calls | 429 with `Retry-After` (the limiter keys on my real IP) |
| 6 | `https://<host>/docs` and `:8000`, `:5432` from outside | not reachable |
| 7 | Reboot the VM (`az vm restart`) | everything comes back by itself in a few minutes |
| 8 | Poller logs after 10 min | `Stored snapshot` and `Rollup` lines |
| 9 | Static-refresh logs | `Next static refresh in X hours` |

**STOP: final report**
- **Live URL**
- each test's result
- the VM size, region and **estimated monthly cost** vs the remaining credit
- what is **not** done (honest list)
- reminders for me:
  - re-check the Anthropic spend after a week
  - watch the Azure credit
  - decide what to commit and push

---

## Phase 7 (optional, after I approve): continuous deployment

A GitHub Actions `deploy.yml` on push to `main`: SSH to the VM with a **dedicated deploy key** stored as a GitHub secret, pull, then `docker compose ... up -d --build`. It runs only after `ci` passes.

**GATE:** I add the secrets myself (`VM_HOST`, `VM_SSH_KEY`). The repo must be on GitHub (my push).

---

## Later (not in this prompt)

- Zero-downtime timetable reloads (staging tables + swap)
- Alerts (join health, poller down) to email
- Managed Postgres / Container Apps migration
- Luas and rail live data
- A demo video and README architecture diagram for the CV
