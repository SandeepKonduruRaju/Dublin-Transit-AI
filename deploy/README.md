# Deploying Dublin Transit AI

One Azure VM runs everything with Docker Compose. Only **Caddy** is public (ports 80 and 443); it gets a free
HTTPS certificate for the VM's hostname and routes requests:

| Path | Goes to |
|---|---|
| `/chat` | `agent:8001` (rate-limited LLM agent) |
| `/health` | `api:8000` (feed versions, realtime age, join health) |
| everything else | `web:3000` (Next.js chat UI) |

`db`, `api`, `agent`, `web`, `rt-poller` and `static-refresh` have **no host ports** in production
(`docker-compose.prod.yml` resets them). `/docs` and the other API routes are not reachable from outside.

All commands below assume:

```powershell
$VM = "<label>.swedencentral.cloudapp.azure.com"
```

and, on the VM:

```bash
cd ~/dublin-transit-ai
alias dc='docker compose -f docker-compose.yml -f docker-compose.prod.yml'
```

## First deploy

1. **Server `.env`** (once). Copy your local `.env` without printing it, then add the hostname:
   ```powershell
   scp .env azureuser@${VM}:~/dublin-transit-ai/.env
   ssh azureuser@$VM "cd ~/dublin-transit-ai && sed -i -e '`$a\' .env && echo SITE_HOST=$VM >> .env && chmod 600 .env"
   ```
   The `sed` adds a final newline first; without it, `SITE_HOST` would be glued onto the last line of `.env`.
2. **Stop the local poller** so only one poller uses the NTA key: `docker compose stop rt-poller`.
3. **Deploy:** `.\deploy\deploy.ps1 -VmHost $VM`
4. **Database**, on the VM: apply the migrations in order, then load the timetable (about 100 MB download;
   the load takes several minutes):
   ```bash
   for f in backend/sql/0*.sql; do dc exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$f"; done
   dc run --rm static-refresh python -m app.static_refresh --once
   ```
   (`POSTGRES_USER`/`POSTGRES_DB` come from `.env`: run `set -a; . ./.env; set +a` first. That only exports
   them to your shell; nothing is printed.)

## Update

```powershell
.\deploy\deploy.ps1 -VmHost $VM
```

It packs the repo (never `.env`, `data/`, virtualenvs, `node_modules`, `.next`, `eval/results` or `.git`),
copies it to `~/releases/`, unpacks it over `~/dublin-transit-ai`, and runs `up -d --build`. The server's
`.env` is never overwritten. Files deleted locally are **not** deleted on the server.

## Logs

```bash
dc ps
dc logs -f --tail 50 rt-poller        # "Stored snapshot", "Rollup: N route-day summary rows refreshed"
dc logs --tail 20 static-refresh      # "Next static refresh in X hours"
dc logs --tail 50 agent caddy
```

Logs rotate automatically (`json-file`, 10 MB x 3 per service).

## Restart

```bash
dc restart agent                      # one service
dc up -d                              # everything, after a config change
```

Every service has `restart: unless-stopped`, and Docker starts on boot, so a VM reboot brings it all back.

## Roll back

The last 5 release tarballs are kept in `~/releases/`:

```bash
ls -1t ~/releases/
tar -xzf ~/releases/dublin-transit-<previous-stamp>.tar.gz -C ~/dublin-transit-ai
dc up -d --build
```

Database migrations are not rolled back automatically; they are written to be safe to re-run.

## Health checks from outside

```powershell
Invoke-RestMethod "https://$VM/health" | ConvertTo-Json -Depth 4   # realtime_age_seconds < 300, join_health >= 90
```
