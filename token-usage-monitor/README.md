Unit file for the ClusterCloud Token Usage Monitor.
Copy to /etc/systemd/system/ then:
  sudo systemctl daemon-reload && sudo systemctl enable --now token-usage-monitor
Adjust ExecStart if your venv lives elsewhere. Port 8110 keeps it beside
clustercloud-tools (:8100).
[Unit]
Description=ClusterCloud Token Usage Monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/clustercloud-tools/token-usage-monitor
ExecStart=/opt/clustercloud-tools/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8110
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
