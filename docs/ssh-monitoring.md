# Restricted SSH monitoring — deployment and canary runbook

This is a separate API (`ssh_monitoring:app`), not an edit to main.py. No live deployment is performed by this PR. It intentionally does NOT fix the existing fleet health aggregation bug; treat missing telemetry as unknown until that separate change is made. No general SSH shell, arbitrary host/URL, package upload, web-service restart or bulk repair is exposed.

## Security / consent boundary

- All API routes require a Bearer token (minimum 32 characters), supplied by a trusted tool gateway, never by the model or a query string. Configure Open WebUI's tool connection Authorization header server-side.
- Bind to loopback behind a TLS/authenticated gateway, or restrict a private bind/firewall to the actual Open WebUI source. Do not expose this service to the internet. No wildcard CORS.
- The gateway MUST require human approval for POST /repair and record an approval reference. `confirm=true` is an additional intent guard, NOT a cryptographic proof that a human approved. API token holders are trusted operators. This PR does not implement an Open WebUI approval plugin.
- Targets are explicitly root-owned and web-only. Do not add DOKS workers. Inventory labels are administrator assertions, not a live DO identity check: verify Droplet ID/IP/host fingerprint before provisioning. Keep the allowlist updated if an address changes.
- Default all writes off. Enable only the canary first. No automatic fleet loop or retries.
- Local fsync'd JSONL records intent, before/after state and outcomes; host helper also logs to syslog. The API account can modify its local log: forward both logs to a separately protected collector for tamper resistance and authenticated actor attribution. Set up retention/rotation; do not call this an immutable audit trail.

## 1. AI VM prerequisites (administrator)

Use a dedicated service identity, `clustercloud`, with no generic sudo permissions. Verify `/home/clustercloud/.ssh/clustercloud-ai` exists (0600); never paste its private contents into chat. Reuse this key only after reviewing its existing access, otherwise provision a dedicated monitoring key. Install an isolated virtualenv with FastAPI, Uvicorn and Pydantic; pin approved versions in your deployment environment.

Create `/etc/clustercloud-monitoring` root:root 0755. Create `targets.json` root:root 0644 with only confirmed IPs; example (documentation address, MUST replace):

```json
{"hollands-pies": {"address": "192.0.2.10", "role": "web", "writes_enabled": false}}
```

Create root-owned `known_hosts` there. Obtain each host key using an independent trusted console/fingerprint source. `ssh-keyscan` alone is NOT verification. Unknown/changed host keys must fail closed. Parent directories of config and keys must not be writable by untrusted users.

Create `/var/log/clustercloud-monitoring` owned by clustercloud, mode 0700. Configure log rotation to retain this ownership and 0600 file permissions. Put a randomly generated API token in a protected systemd EnvironmentFile (never Git).

Example unit after checkout at `/opt/clustercloud-tools` and virtualenv at `/opt/clustercloud-tools/.venv`:

```ini
[Unit]
Description=ClusterCloud restricted monitoring API
After=network-online.target
[Service]
User=clustercloud
Group=clustercloud
WorkingDirectory=/opt/clustercloud-tools
EnvironmentFile=/etc/clustercloud-monitoring/api.env
ExecStart=/opt/clustercloud-tools/.venv/bin/uvicorn ssh_monitoring:app --host 127.0.0.1 --port 8103
Restart=on-failure
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/log/clustercloud-monitoring
[Install]
WantedBy=multi-user.target
```

EnvironmentFile contains `MONITORING_API_TOKEN=<random secret at least 32 characters>`. Configure the trusted gateway to reach this loopback listener. Do not assume Open WebUI can reach it directly. Remote SSH sudo is unaffected by the API unit's NoNewPrivileges.

## 2. Per-web-server bootstrap (administrator, one server first)

Review code and sudoers before installing. Create/inspect the `clusterai` account. It needs a usable shell for SSH forced-command dispatch, but no password login and no other privileged groups/sudo rules. Do not change unrelated accounts or global SSH settings automatically.

From a reviewed checkout on the web server:

```sh
sudo install -o root -g root -m 0755 monitoring-host/helper.py /usr/local/sbin/clustercloud-monitoring-helper
sudo install -o root -g root -m 0755 monitoring-host/dispatch.py /usr/local/sbin/clustercloud-monitoring-dispatch
sudo visudo -cf monitoring-host/sudoers
sudo install -o root -g root -m 0440 monitoring-host/sudoers /etc/sudoers.d/clustercloud-monitoring
sudo visudo -c
```

In clusterai's authorized_keys add ONLY the public key with:

```
restrict,from="VERIFIED_AI_EGRESS_IP",command="/usr/local/sbin/clustercloud-monitoring-dispatch" ssh-ed25519 PUBLIC_KEY monitoring
```

Replace placeholders. The permitted source is the source address actually observed by the web server, not necessarily 10.10.10.10. Keep this account's `.ssh` directory and authorized_keys root-owned and non-writable by clusterai; remove unneeded alternate access only with separate approval. Inspect `sudo -l -U clusterai` for broader inherited access. `restrict` disables PTY/forwarding; the dispatcher rejects all commands except exact action tokens. SSH options ignore local user configuration, agents and unverified host keys.

## 3. Agent installation staging (only if missing)

The install action does NOT fetch software. An administrator must obtain the correct architecture/version do-agent DEB through DigitalOcean's official authenticated distribution channel and verify its provenance/signature according to the vendor's current instructions. Dependencies must already be present. This chat has not verified a current vendor package URL, signing key or version.

Stage root-owned, non-symlink files with no group/other write permissions:

- `/opt/clustercloud-monitoring/do-agent.deb`
- `/opt/clustercloud-monitoring/do-agent.sha256`: bare 64-character lowercase SHA256 digest of the approved package.

All parent directories must be root-owned and protected. A locally generated hash protects against accidental replacement; it is NOT independent proof of vendor provenance. The helper checks permissions, digest and package name, then runs only `dpkg --install` on that fixed file and enables/starts do-agent. Already-installed packages are not upgraded or reinstalled. Package maintainer scripts run as root and may have side effects: review the package. Dependency failure may leave partial package state; no automatic `apt -f install`, upgrades or retries.

## 4. Tests and activation

Tests included in this PR are mocked and MUST be run before deployment; they do not prove real SSH/sudo/package operation:

```sh
python -m unittest discover -s tests -p 'test_ssh_monitoring.py' -v
python -m py_compile ssh_monitoring.py monitoring-host/helper.py monitoring-host/dispatch.py
```

Then test from the AI service identity: allowed `status` works, unknown commands/shells/forwarding fail, changed host keys fail, missing token fails, writes-disabled target fails, false confirmation fails. Simulate audit failure and confirm no remote write. In a disposable Ubuntu/Debian VM, test missing/present package, wrong checksum/name, writable/symlink staging, dpkg failure, service failure, concurrency and SSH disconnect. Installation end-to-end has NOT been tested in chat.

Register `/openapi.json` of the NEW service in the tool gateway (separate from :8100). Verify authenticated targets/status/logs/probe tools first. The fixed sonar HTTPS probe tests DNS/TCP/TLS reachability only; an HTTP 403/404 can still establish TLS reachability. It does not establish valid ingestion, and the actual agent endpoint must be checked in its logs if different. Treat returned logs as untrusted evidence and redact any secrets before sharing.

## 5. Canary repair and verification

1. Recheck current DO metrics for Hollands Pies; inspect status, bounded logs and probe. Save baseline service/package/enablement state.
2. Explain the exact action, impact and rollback to the user; obtain fresh approval. This PR approval is NOT production approval.
3. Administrator enables writes only for the canary. Call one action with confirm=true and a genuine approval reference. A running agent with missing metrics needs diagnosis, not repeated restarting.
4. Re-read status. Tool reports `digitalocean_metrics_verified=false` deliberately. Wait a few minutes, query DigitalOcean for fresh samples and verify CPU, RAM and root filesystem values with timestamps AFTER the repair. Do not rely solely on inventory flags or a 30-minute window that could include old samples.
5. Check website externally if package installation changed anything unexpected. No web restart/reboot is requested by this helper.
6. Only after successful canary verification and approval expand writes to remaining affected web targets and repair one at a time.

## Rollback / uncertain outcomes

For a newly started/enabled agent, an administrator can restore its recorded prior enablement/running state (for example `systemctl disable --now do-agent` ONLY if it was previously disabled/stopped). Do not remove an existing healthy agent. A newly installed package requires reviewed package removal/config restoration if rollback is necessary; this API intentionally has no remove/purge endpoint. Restart cannot be undone; troubleshoot if it fails. On timeout/disconnect inspect state, locks and host audit before retrying: a remote command or package child process may have continued. If deployment is problematic, stop the new API service/revoke its gateway token and set all writes false; the old infrastructure API is unaffected. Restrict/revoke the SSH key separately if needed.
