#!/usr/bin/env bash
# Daily storage measurement (Phase 6 test 11): append the size and row count of every rt_ table to ~/storage_log.csv.
# Installed in azureuser's crontab; reads POSTGRES_USER/POSTGRES_DB from the app's .env (never prints it).
set -euo pipefail
cd ~/dublin-transit-ai
set -a; . ./.env; set +a
LOG=~/storage_log.csv
[ -f "$LOG" ] || echo "measured_at_utc,table,total_bytes,live_rows,snapshots_total,first_snapshot_utc,last_snapshot_utc" > "$LOG"
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F, -c "
    SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), s.relname,
           pg_total_relation_size(s.relid), s.n_live_tup,
           (SELECT count(*) FROM rt_snapshots),
           (SELECT to_char(min(feed_timestamp) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM rt_snapshots),
           (SELECT to_char(max(feed_timestamp) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM rt_snapshots)
    FROM pg_stat_user_tables s WHERE s.relname LIKE 'rt\_%' ORDER BY s.relname;" >> "$LOG"
