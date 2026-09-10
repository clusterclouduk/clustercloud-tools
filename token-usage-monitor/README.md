# Token Usage Monitor

Mobile-first dashboard + ingest API for token usage across the ClusterCloud AI estate.
Small FastAPI service (SQLite-backed), deployed alongside clustercloud-tools on the AI VM — default port 8110.

## Deploy (AI VM)

    cd /opt/clustercloud-tools && git pull
    sudo cp token-usage-monitor/token-usage-monitor.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now token-usage-monitor

Open `http://<ai-vm>:8110` and set your daily token budget in the UI.

Notes:
- ExecStart expects the venv at `/opt/clustercloud-tools/venv` with fastapi + uvicorn installed. Adjust paths if different.
- DB file: `token-usage-monitor/usage.db` (env `TUM_DB` to override).

## API

    POST /api/usage          log one event
      {"source":"chat","model":"claude-sonnet-4","input_tokens":1200,"output_tokens":450}
    POST /api/usage/bulk      batch log (list of the same objects)
    POST /api/budget          {"daily_tokens":500000,"alert_pct":80}
    GET  /api/summary?days=7  today, budget status, daily series, per-source/model breakdown
    GET  /api/health          liveness + watcher status
    GET  /                    dashboard UI

- `cost_usd` optional — otherwise estimated from `TUM_RATES` (USD per 1M tokens, JSON env var, substring-matched on model name; defaults cover sonnet/opus/haiku/gpt-4o/gpt-4).
- `ts` optional ISO-8601, defaults to now (UTC).

## Automatic capture: Ollama watcher

All platform chats run Open WebUI → Ollama on this VM, so tailing Ollama captures
every token the platform spends (every user, every model) — no manual logging needed.

Enable Ollama's per-request token counts (debug logging), then point the watcher at
its journal:

    # 1) ollama token counts (one-time)
    sudo systemctl edit ollama
        [Service]
        Environment=OLLAMA_DEBUG=1
    sudo systemctl restart ollama      # between chats — kills in-flight generation

    # 2) watcher
    sudo systemctl edit token-usage-monitor
        [Service]
        Environment=OLLAMA_LOG_UNIT=ollama
    sudo systemctl restart token-usage-monitor

Alternative modes (set exactly one):
- `OLLAMA_LOG_UNIT=ollama` — tail a systemd unit's journal (recommended, above)
- `OLLAMA_LOG_PATH=/path` — tail a plain log file (e.g. a Docker json-file log)

Verify: `curl -s localhost:8110/api/health` → `watcher_mode: "journal:ollama"`, then
send any chat message and refresh the dashboard — rows appear under source `ollama`.

Notes:
- Capture starts when the watcher starts — nothing retroactive.
- `journalctl -u ollama -n 30 | grep eval_count` after a chat message confirms Ollama
  is emitting counts (needed once, after enabling OLLAMA_DEBUG).
- If your model is cloud-routed via Ollama and never emits eval_count, open an issue —
  fallback is middleware ingest or the manual form.

## Wiring other sources

Anything else that calls LLMs POSTs to `/api/usage`. The chat platform exposes no
usage API to the assistant, so the Ollama watcher above is the automated path for
platform chats; the manual form remains for anything off-platform.
