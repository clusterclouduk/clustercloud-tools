"""Shared backup verification and resource-health rules.

No infrastructure work is performed on import. CLI/action functions are injected
so the rules can be tested without credentials or production access.
"""

import json
import math

from fastapi import HTTPException


def _error_text(exc):
    return str(getattr(exc, "detail", None) or exc)[:300]


def read_backup_policies(droplet_id, run):
    """Return validated policies for exactly one requested Droplet.

    An absent/malformed policy is unknown, never evidence backups are disabled.
    """
    output = run([
        "doctl", "compute", "droplet", "backup-policies", "get",
        str(droplet_id), "--output", "json",
    ])
    try:
        policies = json.loads(output)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(502, "Backup policy unavailable: invalid JSON") from exc

    if isinstance(policies, dict):
        policies = policies.get("backup_policies", [policies])

    if not isinstance(policies, list) or len(policies) != 1:
        raise HTTPException(502, "Backup policy unavailable: expected one policy")

    policy = policies[0]
    if (
        not isinstance(policy, dict)
        or str(policy.get("droplet_id")) != str(droplet_id)
        or type(policy.get("backup_enabled")) is not bool
    ):
        raise HTTPException(502, "Backup policy unavailable: invalid ID or enabled state")

    return policies


def inventory_backup_fields(droplet, run):
    try:
        enabled = read_backup_policies(droplet["id"], run)[0]["backup_enabled"]
        return {
            "backups": enabled,
            "backup_status": "enabled" if enabled else "disabled",
        }
    except Exception as exc:
        return {
            "backups": None,
            "backup_status": "unknown",
            "backup_error": _error_text(exc),
        }


def enable_backup(droplet, run, submit_action, wait_for_action, is_k8s_node):
    """Fail closed on unknown eligibility; success requires a verified outcome.

    changed=None means the change outcome is not established. This avoids
    claiming either success or no change after a request/verification timeout.
    """
    result = {
        "droplet": droplet["name"],
        "id": droplet["id"],
        "status": "verification_unavailable",
        "changed": False,
        "success": False,
        "action_requested": False,
        "previous_backups_enabled": None,
        "backups_enabled": None,
    }

    if is_k8s_node(droplet):
        result.update(
            status="skipped_k8s_node",
            reason="Managed worker excluded from Droplet backup writes; workload backup coverage must be verified separately",
        )
        return result

    try:
        enabled = read_backup_policies(droplet["id"], run)[0]["backup_enabled"]
    except Exception as exc:
        result["error"] = _error_text(exc)
        return result

    result.update(previous_backups_enabled=enabled, backups_enabled=enabled)
    if enabled:
        result.update(status="already_enabled", success=True)
        return result

    # After submission starts, transport/CLI errors may leave the outcome unknown.
    result.update(changed=None, backups_enabled=None, action_requested=None)
    try:
        action = submit_action([
            "doctl", "compute", "droplet-action", "enable-backups",
            str(droplet["id"]), "--output", "json",
        ])
        result.update(action_id=action["id"], action_requested=True)
        action_status = wait_for_action(action["id"])
        result["action_status"] = action_status
    except Exception as exc:
        result["error"] = _error_text(exc)
        return result

    if action_status == "errored":
        result.update(status="failed", error="DigitalOcean action errored; backup enablement not verified")
        return result

    if action_status != "completed":
        result["status"] = "pending"
        return result

    try:
        enabled = read_backup_policies(droplet["id"], run)[0]["backup_enabled"]
    except Exception as exc:
        result["error"] = _error_text(exc)
        return result

    result["backups_enabled"] = enabled
    if enabled:
        result.update(status="verified_enabled", changed=True, success=True)
    else:
        result.update(
            status="verification_unavailable",
            error="Action completed but enabled policy not yet observed; recheck before retrying",
        )
    return result


def _usable(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 100
    )


def classify_resource_health(status, cpu_percent, memory_percent, filesystems, metrics_error=False):
    """Classify current utilisation, separately from provider availability.

    Load is supplementary and not part of required CPU/RAM/disk coverage.
    Filesystem presence alone is not usable disk-utilisation telemetry.
    Freshness and sustained-pressure detection are outside this classifier.
    """
    filesystems = filesystems or []
    disk_values = [fs.get("used_percent") for fs in filesystems]
    telemetry = {
        "cpu": "available" if _usable(cpu_percent) else "missing",
        "memory": "available" if _usable(memory_percent) else "missing",
        "disk": (
            "available" if disk_values and all(_usable(v) for v in disk_values)
            else "partial" if any(_usable(v) for v in disk_values)
            else "missing"
        ),
    }
    warnings = []
    if _usable(cpu_percent) and cpu_percent >= 80:
        warnings.append(f"High CPU usage: {cpu_percent}%")
    if _usable(memory_percent) and memory_percent >= 85:
        warnings.append(f"High memory usage: {memory_percent}%")
    for fs in filesystems:
        if _usable(fs.get("used_percent")) and fs["used_percent"] >= 85:
            warnings.append(f"High disk usage on {fs.get('mountpoint')}: {fs['used_percent']}%")

    if status != "active":
        health = "critical"
        warnings.append(f"Droplet state is {status}")
    elif warnings:
        health = "attention"
    elif metrics_error:
        health = "metrics_error"
    elif all(v == "available" for v in telemetry.values()):
        health = "healthy"
    elif any(v != "missing" for v in telemetry.values()):
        health = "partial_metrics"
    else:
        health = "no_metrics"

    return {"health": health, "warnings": warnings, "telemetry": telemetry}


def fleet_health_summary(results):
    attention = [r for r in results if r["health"] in ("critical", "attention", "metrics_error")]
    no_metrics = [r for r in results if r["health"] == "no_metrics"]
    incomplete = [
        r for r in results
        if any(v != "available" for v in r.get("telemetry", {}).values())
        or not r.get("telemetry")
    ]
    if attention:
        overall = "attention"
    elif not results or len(no_metrics) == len(results):
        overall = "unknown"
    elif incomplete:
        overall = "partial_metrics"
    else:
        overall = "healthy"
    return {
        "servers_needing_attention": len(attention),
        "servers_without_metrics": len(no_metrics),
        "servers_with_incomplete_metrics": len(incomplete),
        "overall_health": overall,
        "attention": attention,
    }
