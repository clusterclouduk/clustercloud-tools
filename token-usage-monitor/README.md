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

## Automatic capture

### Cloud + local models (recommended): Ollama usage proxy

Cloud models (`*:cloud`) never write token counts to the journal — counts only
appear in the HTTP response. The usage proxy sits on `:11434` (where Open WebUI
already points) and forwards to Ollama on `127.0.0.1:11435`, POSTing each
chat/generate's `prompt_eval_count` / `eval_count` to this monitor.

    # Ollama listens internally
    sudo systemctl edit ollama
        [Service]
        Environment=OLLAMA_HOST=127.0.0.1:11435
        Environment=OLLAMA_DEBUG=1

    sudo cp token-usage-monitor/ollama-usage-proxy.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl restart ollama
    sudo systemctl enable --now ollama-usage-proxy

Verify: `curl -s localhost:8110/api/health` and a cloud chat should add rows
under source `ollama` (model name from the response).

### Legacy: journal watcher (local models only)

Set `OLLAMA_LOG_UNIT=ollama` on `token-usage-monitor` to also tail journal
timing lines. Useful as a backup for local models; **not sufficient for cloud**.

## Wiring other sources

Anything else that calls LLMs POSTs to `/api/usage`. The manual dashboard form
remains for off-platform usage.
