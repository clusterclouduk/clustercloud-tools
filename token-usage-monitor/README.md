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
    GET  /                     dashboard UI

- `cost_usd` optional — otherwise estimated from `TUM_RATES` (USD per 1M tokens, JSON env var, substring-matched on model name; defaults cover sonnet/opus/haiku/gpt-4o/gpt-4).
- `ts` optional ISO-8601, defaults to now (UTC).

## Optional: Ollama log watcher

Set `OLLAMA_LOG_PATH` to tail Ollama's server log and auto-capture per-request token counts (best-effort; handles key=value and JSON log lines with prompt_eval_count / eval_count). Enable `OLLAMA_DEBUG=1` so Ollama emits eval counts in its logs.

## Wiring this chat

The chat platform exposes no usage API to the assistant, so chat sessions are logged via the dashboard's manual form (or POST /api/usage from middleware added later).
