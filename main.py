from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import json

app = FastAPI(
    title="ClusterCloud Infrastructure Tools",
    description="Read-only infrastructure tools for ClusterCloud AI",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def run(cmd):
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail=e.stderr or e.stdout or str(e)
        )

@app.get("/health", operation_id="infrastructure_health")
def health():
    return {"status": "ok"}

# DIGITALOCEAN

@app.get(
    "/digitalocean/kubernetes/clusters",
    operation_id="list_digitalocean_kubernetes_clusters"
)
def list_do_clusters():
    output = run([
        "doctl",
        "kubernetes",
        "cluster",
        "list",
        "--output",
        "json",
    ])
    return json.loads(output)

@app.get(
    "/digitalocean/droplets",
    operation_id="list_digitalocean_droplets"
)
def list_droplets():
    output = run([
        "doctl",
        "compute",
        "droplet",
        "list",
        "--output",
        "json",
    ])
    return json.loads(output)

# KUBERNETES

@app.get("/kubernetes/nodes", operation_id="get_kubernetes_nodes")
def nodes():
    return json.loads(run([
        "kubectl",
        "get",
        "nodes",
        "-o",
        "json",
    ]))

@app.get("/kubernetes/namespaces", operation_id="get_kubernetes_namespaces")
def namespaces():
    return json.loads(run([
        "kubectl",
        "get",
        "namespaces",
        "-o",
        "json",
    ]))

@app.get("/kubernetes/pods", operation_id="get_kubernetes_pods")
def pods(namespace: str = ""):
    cmd = ["kubectl", "get", "pods"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    return json.loads(run(cmd))

@app.get("/kubernetes/events", operation_id="get_kubernetes_events")
def events(namespace: str = ""):
    cmd = ["kubectl", "get", "events"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += [
        "--sort-by=.lastTimestamp",
        "-o",
        "json",
    ]

    return json.loads(run(cmd))

@app.get(
    "/kubernetes/pod/logs",
    operation_id="get_kubernetes_pod_logs"
)
def pod_logs(
    namespace: str,
    pod: str,
    container: str = "",
    tail: int = 200
):
    tail = max(1, min(tail, 1000))

    cmd = [
        "kubectl",
        "logs",
        pod,
        "-n",
        namespace,
        "--tail",
        str(tail),
    ]

    if container:
        cmd += ["-c", container]

    return {"logs": run(cmd)}

@app.get(
    "/kubernetes/pod/describe",
    operation_id="describe_kubernetes_pod"
)
def describe_pod(namespace: str, pod: str):
    return {
        "output": run([
            "kubectl",
            "describe",
            "pod",
            pod,
            "-n",
            namespace,
        ])
    }

@app.get(
    "/kubernetes/deployments",
    operation_id="get_kubernetes_deployments"
)
def deployments(namespace: str = ""):
    cmd = ["kubectl", "get", "deployments"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    return json.loads(run(cmd))

@app.get(
    "/kubernetes/services",
    operation_id="get_kubernetes_services"
)
def services(namespace: str = ""):
    cmd = ["kubectl", "get", "services"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    return json.loads(run(cmd))

@app.get(
    "/kubernetes/ingresses",
    operation_id="get_kubernetes_ingresses"
)
def ingresses(namespace: str = ""):
    cmd = ["kubectl", "get", "ingress"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    return json.loads(run(cmd))

@app.get(
    "/kubernetes/pvcs",
    operation_id="get_kubernetes_pvcs"
)
def pvcs(namespace: str = ""):
    cmd = ["kubectl", "get", "pvc"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    return json.loads(run(cmd))

@app.get(
    "/kubernetes/workload/logs",
    operation_id="get_kubernetes_workload_logs",
    summary="Get logs for a Kubernetes workload",
    description=(
        "Retrieve recent logs for all pods belonging to or matching a workload, "
        "deployment, application, or pod-name prefix. Use this when the user gives "
        "a friendly workload name such as clusterbot-api rather than an exact pod name."
    ),
)
def workload_logs(
    namespace: str,
    workload: str,
    tail: int = 200
):
    tail = max(1, min(tail, 1000))

    pods_raw = run([
        "kubectl",
        "get",
        "pods",
        "-n",
        namespace,
        "-o",
        "json",
    ])

    pods = json.loads(pods_raw)

    matches = []

    for item in pods.get("items", []):
        name = item["metadata"]["name"]
        labels = item["metadata"].get("labels", {})

        label_values = [str(v) for v in labels.values()]

        if (
            name == workload
            or name.startswith(workload + "-")
            or workload in label_values
        ):
            matches.append(item)

    if not matches:
        raise HTTPException(
            status_code=404,
            detail=f"No pods matching workload '{workload}' in namespace '{namespace}'"
        )

    results = []

    for item in matches:
        pod_name = item["metadata"]["name"]

        containers = [
            c["name"]
            for c in item.get("spec", {}).get("containers", [])
        ]

        pod_result = {
            "pod": pod_name,
            "phase": item.get("status", {}).get("phase"),
            "containers": []
        }

        for container in containers:
            try:
                logs = run([
                    "kubectl",
                    "logs",
                    pod_name,
                    "-n",
                    namespace,
                    "-c",
                    container,
                    "--tail",
                    str(tail),
                ])

                pod_result["containers"].append({
                    "container": container,
                    "logs": logs
                })

            except HTTPException as e:
                pod_result["containers"].append({
                    "container": container,
                    "error": str(e.detail)
                })

        results.append(pod_result)

    return {
        "namespace": namespace,
        "workload": workload,
        "matching_pods": len(results),
        "pods": results
    }

import os
import time
import requests

def get_do_token():
    # doctl stores the active token in its config.
    config = os.path.expanduser("~/.config/doctl/config.yaml")

    if not os.path.exists(config):
        raise HTTPException(
            status_code=500,
            detail="DigitalOcean doctl config not found"
        )

    with open(config, "r") as f:
        for line in f:
            if line.strip().startswith("access-token:"):
                token = line.split(":", 1)[1].strip()
                if token:
                    return token

    raise HTTPException(
        status_code=500,
        detail="DigitalOcean access token not found"
    )


def do_api(path, params=None):
    token = get_do_token()

    response = requests.get(
        f"https://api.digitalocean.com{path}",
        params=params or {},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )

    if not response.ok:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text,
        )

    return response.json()


def resolve_droplet(droplet: str):

    droplets = json.loads(run([
        "doctl",
        "compute",
        "droplet",
        "list",
        "--output",
        "json"
    ]))

    for d in droplets:
        if str(d.get("id")) == str(droplet):
            return d

        if d.get("name", "").lower() == droplet.lower():
            return d

    raise HTTPException(
        status_code=404,
        detail=f"Droplet '{droplet}' not found"
    )


def latest_metric(result):
    try:
        values = result["data"]["result"]

        if not values:
            return None

        series = values[0].get("values", [])

        if not series:
            return None

        timestamp, value = series[-1]

        return {
            "timestamp": timestamp,
            "value": float(value)
        }

    except (KeyError, IndexError, TypeError, ValueError):
        return None


@app.get(
    "/digitalocean/droplet/metrics",
    operation_id="get_digitalocean_droplet_metrics",
    summary="Get live resource metrics for a DigitalOcean Droplet",
    description=(
        "Retrieve recent CPU, memory and filesystem metrics for a DigitalOcean "
        "Droplet. Accepts either the Droplet name or ID. Use this when the user "
        "asks about CPU, RAM, memory, disk or resource usage of a Droplet."
    ),
)
def droplet_metrics(
    droplet: str,
    minutes: int = 15
):
    d = resolve_droplet(droplet)

    host_id = str(d["id"])

    end = int(time.time())
    start = end - (max(5, min(minutes, 1440)) * 60)

    params = {
        "host_id": host_id,
        "start": start,
        "end": end
    }

    cpu_raw = do_api(
        "/v2/monitoring/metrics/droplet/cpu",
        params
    )

    mem_total_raw = do_api(
        "/v2/monitoring/metrics/droplet/memory_total",
        params
    )

    mem_available_raw = do_api(
        "/v2/monitoring/metrics/droplet/memory_available",
        params
    )

    fs_size_raw = do_api(
        "/v2/monitoring/metrics/droplet/filesystem_size",
        params
    )

    fs_free_raw = do_api(
        "/v2/monitoring/metrics/droplet/filesystem_free",
        params
    )

    cpu = latest_metric(cpu_raw)
    mem_total = latest_metric(mem_total_raw)
    mem_available = latest_metric(mem_available_raw)

    memory = None

    if mem_total and mem_available and mem_total["value"] > 0:

        used = mem_total["value"] - mem_available["value"]

        memory = {
            "total_bytes": mem_total["value"],
            "available_bytes": mem_available["value"],
            "used_bytes": used,
            "used_percent": round(
                (used / mem_total["value"]) * 100,
                2
            )
        }

    # Filesystem endpoints may return multiple devices/mounts,
    # so return their raw DO response as well as top-level info.

    return {
        "droplet": {
            "id": d["id"],
            "name": d["name"],
            "status": d["status"],
            "memory_mb": d["memory"],
            "vcpus": d["vcpus"],
            "disk_gb": d["disk"],
            "features": d.get("features", [])
        },
        "window_minutes": minutes,
        "cpu": cpu,
        "memory": memory,
        "filesystem_size": fs_size_raw,
        "filesystem_free": fs_free_raw
    }

# ============================================================================
# DIGITALOCEAN HUMAN-FRIENDLY METRICS SUMMARY
# ============================================================================

def _metric_results(raw):
    try:
        return raw.get("data", {}).get("result", [])
    except Exception:
        return []


def _last_value(series):
    values = series.get("values", [])
    if not values:
        return None

    try:
        return {
            "timestamp": int(values[-1][0]),
            "value": float(values[-1][1])
        }
    except Exception:
        return None


def _cpu_percent(raw):
    """
    DigitalOcean CPU metrics are cumulative CPU seconds split by mode.
    Calculate utilisation from the delta between the final two samples.

    usage = 100 * (1 - idle_delta / total_delta)
    """

    results = _metric_results(raw)

    if not results:
        return None

    total_delta = 0.0
    idle_delta = 0.0
    timestamp = None

    for series in results:
        values = series.get("values", [])

        if len(values) < 2:
            continue

        try:
            previous = float(values[-2][1])
            current = float(values[-1][1])
            delta = current - previous

            # Ignore resets / malformed counters
            if delta < 0:
                continue

            mode = series.get("metric", {}).get("mode", "")

            total_delta += delta

            if mode == "idle":
                idle_delta += delta

            timestamp = int(values[-1][0])

        except Exception:
            continue

    if total_delta <= 0:
        return None

    usage = (1.0 - (idle_delta / total_delta)) * 100.0

    return {
        "used_percent": round(max(0.0, min(100.0, usage)), 2),
        "timestamp": timestamp
    }


def _single_metric_latest(raw):
    results = _metric_results(raw)

    if not results:
        return None

    # Usually one result, but tolerate multiple series.
    latest = []

    for series in results:
        value = _last_value(series)
        if value:
            latest.append(value)

    if not latest:
        return None

    latest.sort(key=lambda x: x["timestamp"], reverse=True)
    return latest[0]


def _filesystem_summary(size_raw, free_raw):
    size_results = _metric_results(size_raw)
    free_results = _metric_results(free_raw)

    free_lookup = {}

    for series in free_results:
        metric = series.get("metric", {})

        key = (
            metric.get("device", ""),
            metric.get("mountpoint", ""),
            metric.get("fstype", "")
        )

        value = _last_value(series)

        if value:
            free_lookup[key] = value

    filesystems = []

    for series in size_results:
        metric = series.get("metric", {})

        key = (
            metric.get("device", ""),
            metric.get("mountpoint", ""),
            metric.get("fstype", "")
        )

        size = _last_value(series)
        free = free_lookup.get(key)

        if not size:
            continue

        size_bytes = size["value"]
        free_bytes = free["value"] if free else None

        item = {
            "device": metric.get("device"),
            "mountpoint": metric.get("mountpoint"),
            "fstype": metric.get("fstype"),
            "size_bytes": size_bytes,
            "size_gb": round(size_bytes / (1024 ** 3), 2),
        }

        if free_bytes is not None and size_bytes > 0:
            used_bytes = max(0, size_bytes - free_bytes)

            item.update({
                "free_bytes": free_bytes,
                "free_gb": round(free_bytes / (1024 ** 3), 2),
                "used_bytes": used_bytes,
                "used_gb": round(used_bytes / (1024 ** 3), 2),
                "used_percent": round((used_bytes / size_bytes) * 100, 2)
            })

        filesystems.append(item)

    return filesystems


@app.get(
    "/digitalocean/droplet/metrics-summary",
    operation_id="get_digitalocean_droplet_metrics_summary",
    summary="Get CPU, RAM, disk and load for a DigitalOcean Droplet",
    description=(
        "Returns human-friendly current resource utilisation for a DigitalOcean "
        "Droplet including calculated CPU percentage, RAM percentage, filesystem "
        "usage and load average. Accepts a Droplet name or ID. Prefer this tool "
        "when the user asks about CPU, memory, RAM, disk, load or resource pressure."
    ),
)
def droplet_metrics_summary(
    droplet: str,
    minutes: int = 15
):
    d = resolve_droplet(droplet)

    host_id = str(d["id"])

    minutes = max(5, min(minutes, 1440))

    end = int(time.time())
    start = end - (minutes * 60)

    params = {
        "host_id": host_id,
        "start": start,
        "end": end
    }

    cpu_raw = do_api(
        "/v2/monitoring/metrics/droplet/cpu",
        params
    )

    mem_total_raw = do_api(
        "/v2/monitoring/metrics/droplet/memory_total",
        params
    )

    mem_available_raw = do_api(
        "/v2/monitoring/metrics/droplet/memory_available",
        params
    )

    fs_size_raw = do_api(
        "/v2/monitoring/metrics/droplet/filesystem_size",
        params
    )

    fs_free_raw = do_api(
        "/v2/monitoring/metrics/droplet/filesystem_free",
        params
    )

    load_raw = do_api(
        "/v2/monitoring/metrics/droplet/load_1",
        params
    )

    # ------------------------------------------------------------------------
    # CPU
    # ------------------------------------------------------------------------

    cpu = _cpu_percent(cpu_raw)

    # ------------------------------------------------------------------------
    # MEMORY
    # ------------------------------------------------------------------------

    total = _single_metric_latest(mem_total_raw)
    available = _single_metric_latest(mem_available_raw)

    memory = None

    if total and available and total["value"] > 0:
        total_bytes = total["value"]
        available_bytes = available["value"]
        used_bytes = max(0, total_bytes - available_bytes)

        memory = {
            "total_bytes": total_bytes,
            "total_gb": round(total_bytes / (1024 ** 3), 2),

            "available_bytes": available_bytes,
            "available_gb": round(available_bytes / (1024 ** 3), 2),

            "used_bytes": used_bytes,
            "used_gb": round(used_bytes / (1024 ** 3), 2),

            "used_percent": round(
                (used_bytes / total_bytes) * 100,
                2
            )
        }

    # ------------------------------------------------------------------------
    # FILESYSTEM
    # ------------------------------------------------------------------------

    filesystems = _filesystem_summary(
        fs_size_raw,
        fs_free_raw
    )

    # ------------------------------------------------------------------------
    # LOAD
    # ------------------------------------------------------------------------

    load = _single_metric_latest(load_raw)

    load_1 = round(load["value"], 2) if load else None

    # ------------------------------------------------------------------------
    # SIMPLE HEALTH CLASSIFICATION
    # ------------------------------------------------------------------------

    warnings = []

    if cpu and cpu["used_percent"] >= 80:
        warnings.append(
            f"High CPU usage: {cpu['used_percent']}%"
        )

    if memory and memory["used_percent"] >= 85:
        warnings.append(
            f"High memory usage: {memory['used_percent']}%"
        )

    for filesystem in filesystems:
        if filesystem.get("used_percent", 0) >= 85:
            warnings.append(
                f"High disk usage on "
                f"{filesystem.get('mountpoint')}: "
                f"{filesystem['used_percent']}%"
            )

    health = "healthy"

    if warnings:
        health = "attention"

    return {
        "droplet": {
            "id": d["id"],
            "name": d["name"],
            "status": d["status"],
            "vcpus": d["vcpus"],
            "configured_memory_mb": d["memory"],
            "configured_disk_gb": d["disk"],
            "features": d.get("features", [])
        },

        "window_minutes": minutes,

        "cpu": cpu,

        "memory": memory,

        "load_1": load_1,

        "filesystems": filesystems,

        "health": health,

        "warnings": warnings
    }

@app.get(
    "/digitalocean/droplets/health",
    operation_id="check_all_digitalocean_droplet_health",
    summary="Check all DigitalOcean Droplets for resource pressure",
    description=(
        "Check every DigitalOcean Droplet for current CPU, memory, disk and "
        "load pressure. Use this when the user asks whether any servers, "
        "Droplets or DigitalOcean systems are under stress, overloaded, "
        "running out of resources, or need attention."
    ),
)
def check_all_droplet_health(minutes: int = 15):

    droplets = json.loads(run([
        "doctl",
        "compute",
        "droplet",
        "list",
        "--output",
        "json"
    ]))

    results = []

    for d in droplets:

        host_id = str(d["id"])

        end = int(time.time())
        start = end - (max(5, min(minutes, 1440)) * 60)

        params = {
            "host_id": host_id,
            "start": start,
            "end": end
        }

        item = {
            "id": d["id"],
            "name": d["name"],
            "status": d["status"],
            "vcpus": d["vcpus"],
            "configured_memory_mb": d["memory"],
            "configured_disk_gb": d["disk"],
            "features": d.get("features", []),
            "cpu_percent": None,
            "memory_percent": None,
            "load_1": None,
            "filesystems": [],
            "health": "unknown",
            "warnings": []
        }

        try:

            cpu_raw = do_api(
                "/v2/monitoring/metrics/droplet/cpu",
                params
            )

            mem_total_raw = do_api(
                "/v2/monitoring/metrics/droplet/memory_total",
                params
            )

            mem_available_raw = do_api(
                "/v2/monitoring/metrics/droplet/memory_available",
                params
            )

            fs_size_raw = do_api(
                "/v2/monitoring/metrics/droplet/filesystem_size",
                params
            )

            fs_free_raw = do_api(
                "/v2/monitoring/metrics/droplet/filesystem_free",
                params
            )

            load_raw = do_api(
                "/v2/monitoring/metrics/droplet/load_1",
                params
            )

            # CPU
            cpu = _cpu_percent(cpu_raw)

            if cpu:
                item["cpu_percent"] = cpu["used_percent"]

            # Memory
            total = _single_metric_latest(mem_total_raw)
            available = _single_metric_latest(mem_available_raw)

            if total and available and total["value"] > 0:

                used = max(
                    0,
                    total["value"] - available["value"]
                )

                item["memory_percent"] = round(
                    (used / total["value"]) * 100,
                    2
                )

            # Load
            load = _single_metric_latest(load_raw)

            if load:
                item["load_1"] = round(load["value"], 2)

            # Filesystems
            filesystems = _filesystem_summary(
                fs_size_raw,
                fs_free_raw
            )

            item["filesystems"] = [
                {
                    "device": fs.get("device"),
                    "mountpoint": fs.get("mountpoint"),
                    "used_percent": fs.get("used_percent"),
                    "used_gb": fs.get("used_gb"),
                    "free_gb": fs.get("free_gb")
                }
                for fs in filesystems
            ]

            # Thresholds
            if (
                item["cpu_percent"] is not None
                and item["cpu_percent"] >= 80
            ):
                item["warnings"].append(
                    f"CPU high: {item['cpu_percent']}%"
                )

            if (
                item["memory_percent"] is not None
                and item["memory_percent"] >= 85
            ):
                item["warnings"].append(
                    f"Memory high: {item['memory_percent']}%"
                )

            for fs in filesystems:
                used = fs.get("used_percent")

                if used is not None and used >= 85:
                    item["warnings"].append(
                        f"Disk high on {fs.get('mountpoint')}: {used}%"
                    )

            # Classification
            metrics_available = any([
                item["cpu_percent"] is not None,
                item["memory_percent"] is not None,
                bool(item["filesystems"])
            ])

            if item["status"] != "active":
                item["health"] = "critical"
                item["warnings"].append(
                    f"Droplet state is {item['status']}"
                )

            elif item["warnings"]:
                item["health"] = "attention"

            elif metrics_available:
                item["health"] = "healthy"

            else:
                item["health"] = "no_metrics"

        except Exception as e:
            item["health"] = "metrics_error"
            item["warnings"].append(
                f"Metrics query failed: {str(e)}"
            )

        results.append(item)

    attention = [
        x for x in results
        if x["health"] in [
            "critical",
            "attention",
            "metrics_error"
        ]
    ]

    no_metrics = [
        x for x in results
        if x["health"] == "no_metrics"
    ]

    return {
        "checked": len(results),
        "window_minutes": minutes,
        "servers_needing_attention": len(attention),
        "servers_without_metrics": len(no_metrics),
        "overall_health": (
            "attention"
            if attention
            else "healthy"
        ),
        "attention": attention,
        "servers": results
    }

@app.post(
    "/digitalocean/droplet/enable-backups",
    operation_id="enable_digitalocean_droplet_backups",
    summary="Enable backups on a DigitalOcean Droplet",
    description=(
        "Enable DigitalOcean backups for a specific Droplet by name or ID. "
        "This is a write action and should only be called after explicit user confirmation."
    ),
)
def enable_droplet_backups(droplet: str):
    d = resolve_droplet(droplet)

    if "backups" in d.get("features", []):
        return {
            "changed": False,
            "droplet": d["name"],
            "id": d["id"],
            "status": "backups_already_enabled"
        }

    output = run([
        "doctl",
        "compute",
        "droplet",
        "backup",
        str(d["id"])
    ])

    return {
        "changed": True,
        "droplet": d["name"],
        "id": d["id"],
        "status": "backup_enable_requested",
        "result": output
    }


@app.post(
    "/digitalocean/droplets/enable-missing-backups",
    operation_id="enable_missing_digitalocean_backups",
    summary="Enable backups on all DigitalOcean Droplets missing backups",
    description=(
        "Find every DigitalOcean Droplet that does not have backups enabled "
        "and enable backups on each one. This is a write action and must only "
        "be used after explicit user confirmation."
    ),
)
def enable_missing_backups():

    droplets = json.loads(run([
        "doctl",
        "compute",
        "droplet",
        "list",
        "--output",
        "json"
    ]))

    missing = [
        d for d in droplets
        if "backups" not in d.get("features", [])
    ]

    results = []

    for d in missing:
        try:
            output = run([
                "doctl",
                "compute",
                "droplet",
                "backup",
                str(d["id"])
            ])

            results.append({
                "droplet": d["name"],
                "id": d["id"],
                "success": True,
                "result": output
            })

        except Exception as e:
            results.append({
                "droplet": d["name"],
                "id": d["id"],
                "success": False,
                "error": str(e)
            })

    return {
        "missing_backups_found": len(missing),
        "results": results
    }

from datetime import datetime, timezone


def _pod_age(timestamp):
    if not timestamp:
        return None

    try:
        created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created

        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60

        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return None


def _pod_summary(item):
    metadata = item.get("metadata", {})
    status = item.get("status", {})
    spec = item.get("spec", {})

    container_statuses = status.get("containerStatuses", []) or []

    ready = sum(
        1 for c in container_statuses
        if c.get("ready") is True
    )

    total = len(spec.get("containers", []))

    restarts = sum(
        c.get("restartCount", 0)
        for c in container_statuses
    )

    waiting_reasons = []

    for c in container_statuses:
        waiting = (
            c.get("state", {})
             .get("waiting", {})
             .get("reason")
        )

        if waiting:
            waiting_reasons.append(waiting)

    phase = status.get("phase", "Unknown")

    healthy = (
        phase in ("Running", "Succeeded")
        and (
            phase == "Succeeded"
            or ready == total
        )
        and not waiting_reasons
    )

    return {
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "phase": phase,
        "ready": f"{ready}/{total}",
        "restarts": restarts,
        "waiting_reasons": waiting_reasons,
        "node": spec.get("nodeName"),
        "age": _pod_age(metadata.get("creationTimestamp")),
        "healthy": healthy
    }


@app.get(
    "/kubernetes/pods/summary",
    operation_id="get_kubernetes_pod_status_summary",
    summary="Get compact Kubernetes pod health summary",
    description=(
        "Returns a compact health summary of Kubernetes pods without the large "
        "raw Kubernetes API response. Use this as the default tool when the user "
        "asks how pods are looking, whether workloads are healthy, or whether "
        "anything in Kubernetes needs attention."
    ),
)
def pod_status_summary(namespace: str = ""):

    cmd = ["kubectl", "get", "pods"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    raw = json.loads(run(cmd))

    pods = [
        _pod_summary(item)
        for item in raw.get("items", [])
    ]

    unhealthy = [
        p for p in pods
        if not p["healthy"]
        and p["phase"] != "Succeeded"
    ]

    restarted = [
        p for p in pods
        if p["restarts"] > 0
    ]

    return {
        "namespace": namespace or "all",
        "total_pods": len(pods),
        "healthy_pods": len([
            p for p in pods if p["healthy"]
        ]),
        "unhealthy_pods": len(unhealthy),
        "pods_with_restarts": len(restarted),

        "unhealthy": unhealthy,

        "recent_restart_attention": sorted(
            restarted,
            key=lambda x: x["restarts"],
            reverse=True
        )[:20],

        "pods": pods
    }


@app.get(
    "/kubernetes/health",
    operation_id="check_kubernetes_cluster_health",
    summary="Perform a Kubernetes cluster health check",
    description=(
        "Perform a compact read-only health assessment of Kubernetes nodes "
        "and pods. Use this first for broad questions such as 'how is Kubernetes', "
        "'is the cluster healthy', 'anything wrong with the cluster', or "
        "'how are my pods looking'."
    ),
)
def kubernetes_health():

    nodes_raw = json.loads(run([
        "kubectl", "get", "nodes", "-o", "json"
    ]))

    pods_raw = json.loads(run([
        "kubectl", "get", "pods", "-A", "-o", "json"
    ]))

    nodes = []

    for node in nodes_raw.get("items", []):
        conditions = {
            c.get("type"): c.get("status")
            for c in node.get("status", {}).get("conditions", [])
        }

        nodes.append({
            "name": node["metadata"]["name"],
            "ready": conditions.get("Ready") == "True",
            "memory_pressure": conditions.get("MemoryPressure") == "True",
            "disk_pressure": conditions.get("DiskPressure") == "True",
            "pid_pressure": conditions.get("PIDPressure") == "True",
            "network_unavailable": conditions.get("NetworkUnavailable") == "True"
        })

    pods = [
        _pod_summary(item)
        for item in pods_raw.get("items", [])
    ]

    unhealthy = [
        p for p in pods
        if not p["healthy"]
        and p["phase"] != "Succeeded"
    ]

    node_problems = [
        n for n in nodes
        if (
            not n["ready"]
            or n["memory_pressure"]
            or n["disk_pressure"]
            or n["pid_pressure"]
            or n["network_unavailable"]
        )
    ]

    return {
        "overall_health": (
            "attention"
            if node_problems or unhealthy
            else "healthy"
        ),
        "nodes": {
            "total": len(nodes),
            "problems": node_problems,
            "items": nodes
        },
        "pods": {
            "total": len(pods),
            "unhealthy_count": len(unhealthy),
            "unhealthy": unhealthy
        }
    }


# ── READ-ONLY pod file inspection ──
import re
import subprocess

_PODFILE_ALLOWED_PREFIXES = (
    "/var/www/vhosts/",
    "/usr/local/lsws/logs/",
)

_PODFILE_NAME_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"
)

def _podfile_validate(namespace: str, pod: str, path: str):
    if (
        not _PODFILE_NAME_RE.match(namespace)
        or not _PODFILE_NAME_RE.match(pod)
    ):
        raise HTTPException(
            status_code=400,
            detail="invalid namespace or pod name"
        )

    if not path.startswith(_PODFILE_ALLOWED_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail=f"path must start with one of {_PODFILE_ALLOWED_PREFIXES}"
        )

    if ".." in path or any(ord(c) < 32 for c in path):
        raise HTTPException(
            status_code=400,
            detail="suspicious path"
        )


@app.get(
    "/kubernetes/pod-file",
    operation_id="get_kubernetes_pod_file",
    summary="Inspect files and web logs inside a Kubernetes pod",
    description=(
        "READ-ONLY inspection of files inside Kubernetes pods. "
        "Use for WordPress/OpenLiteSpeed security investigations, "
        "web-server logs, listing site directories, identifying recently "
        "modified PHP/PHTML files, or checking suspicious patterns. "
        "Allowed paths are /var/www/vhosts/ and /usr/local/lsws/logs/. "
        "This tool does not modify files or restart workloads."
    ),
)
def get_kubernetes_pod_file(
    namespace: str,
    pod: str,
    path: str,
    action: str = "ls",
    lines: int = 200,
    hours: int = 48,
    pattern: str = "",
):
    _podfile_validate(namespace, pod, path)

    if action == "ls":
        cmd = ["ls", "-la", path]

    elif action == "tail":
        cmd = [
            "tail",
            "-n",
            str(max(1, min(lines, 1000))),
            path,
        ]

    elif action == "recent":
        cmd = [
            "find",
            path,
            "-type",
            "f",
            "(",
            "-name",
            "*.php",
            "-o",
            "-name",
            "*.phtml",
            ")",
            "-mmin",
            "-" + str(max(1, min(hours, 720)) * 60),
            "-printf",
            "%TY-%Tm-%Td %TH:%TM %s %p\n",
        ]

    elif action == "grep":
        if not pattern or len(pattern) > 80:
            raise HTTPException(
                status_code=400,
                detail="bad pattern (1-80 chars)"
            )

        cmd = [
            "grep",
            "-c",
            "-E",
            "--",
            pattern,
            path,
        ]

    else:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action '{action}'"
        )

    try:
        proc = subprocess.run(
            [
                "kubectl",
                "-n",
                namespace,
                "exec",
                pod,
                "--",
            ] + cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="kubectl exec timed out"
        )

    if proc.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=(
                proc.stderr.strip()
                or "kubectl exec failed"
            )[:500]
        )

    return {
        "namespace": namespace,
        "pod": pod,
        "action": action,
        "path": path,
        "output": proc.stdout[-20000:],
    }
# ===================== GITHUB TOOLS (branch -> commit -> PR) =====================
import os as _gh_os
import base64 as _gh_b64
from fastapi import HTTPException
from pydantic import BaseModel

_GH_MAX_FILE_BYTES = 400_000
_GH_MAX_FILES = 20

def _gh_headers():
    t = _gh_os.environ.get("GITHUB_TOKEN", "")
    if not t:
        raise HTTPException(500, "GITHUB_TOKEN not set on tools service")
    return {"Authorization": f"Bearer {t}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}

def _gh(method, path, expected=(200, 201, 202, 204), **kw):
    r = requests.request(method, "https://api.github.com" + path,
                         headers=_gh_headers(), timeout=20, **kw)
    if r.status_code not in expected:
        raise HTTPException(r.status_code, f"GitHub {method} {path}: {r.text[:1500]}")
    return r.json() if r.content else {}

def _gh_check_repo(repo: str):
    repo = repo.strip().strip("/")
    if len(repo.split("/")) != 2 or not all(repo.split("/")):
        raise HTTPException(400, "repo must be 'owner/name'")

def _gh_default(repo):
    return _gh("GET", f"/repos/{repo}").get("default_branch", "main")

@app.get("/github/repos")
def get_github_repos(per_page: int = 50):
    """List GitHub repos this integration can access. READ-ONLY, safe to run automatically."""
    data = _gh("GET", f"/user/repos?per_page={min(per_page, 100)}&sort=updated")
    return {"repo_count": len(data), "repos": [
        {"repo": r["full_name"], "private": r["private"], "default_branch": r["default_branch"]}
        for r in data]}

@app.get("/github/file")
def get_github_file(repo: str, path: str = "", ref: str = ""):
    """Read one file (returns content) or list a folder from a GitHub repo. READ-ONLY."""
    _gh_check_repo(repo)
    data = _gh("GET", f"/repos/{repo}/contents/{path.strip('/')}",
               params={"ref": ref} if ref else {}, expected=(200,))
    if isinstance(data, list):
        return {"path": path or "/", "entries": [
            {"name": e["name"], "path": e["path"], "type": e["type"], "size": e.get("size", 0)}
            for e in data]}
    if data.get("size", 0) > _GH_MAX_FILE_BYTES:
        raise HTTPException(413, f"file too large for chat ({data['size']} bytes)")
    return {"path": data["path"], "sha": data["sha"], "size": data.get("size", 0),
            "content": _gh_b64.b64decode(data["content"]).decode("utf-8", errors="replace")}

class _GhFile(BaseModel):
    path: str
    content: str

class _GhBranch(BaseModel):
    repo: str
    branch: str
    from_ref: str = ""
    confirm: bool = False

class _GhCommit(BaseModel):
    repo: str
    branch: str
    message: str
    files: list[_GhFile]
    allow_direct: bool = False
    confirm: bool = False

class _GhPR(BaseModel):
    repo: str
    head: str
    title: str
    body: str = ""
    base: str = ""
    draft: bool = False
    confirm: bool = False

@app.post("/github/branch")
def create_github_branch(req: _GhBranch):
    """Create a branch from a ref (or default branch). WRITE - needs confirm=true after user approval."""
    _gh_check_repo(req.repo)
    if not req.confirm:
        return {"status": "needs_confirmation",
                "summary": f"Create branch '{req.branch}' on {req.repo} from '{req.from_ref or 'default'}'. Ask user, then resend confirm=true."}
    src = req.from_ref or _gh_default(req.repo)
    sha = _gh("GET", f"/repos/{req.repo}/git/ref/heads/{src}", expected=(200,))["object"]["sha"]
    try:
        out = _gh("POST", f"/repos/{req.repo}/git/refs",
                  json={"ref": f"refs/heads/{req.branch}", "sha": sha}, expected=(201,))
        return {"status": "created", "branch": req.branch, "sha": out["object"]["sha"]}
    except HTTPException as e:
        if e.status_code == 422:
            return {"status": "exists", "branch": req.branch}
        raise

@app.post("/github/commit")
def commit_github_files(req: _GhCommit):
    """Commit multiple files in ONE commit. WRITE - needs confirm=true.
    Auto-creates the branch from default if missing. Refuses default-branch commits unless allow_direct=true."""
    _gh_check_repo(req.repo)
    if not req.files or len(req.files) > _GH_MAX_FILES:
        raise HTTPException(400, f"1-{_GH_MAX_FILES} files required")
    default = _gh_default(req.repo)
    if req.branch == default and not req.allow_direct:
        raise HTTPException(403, f"refusing to commit to default branch '{default}' - use a branch")
    if not req.confirm:
        return {"status": "needs_confirmation",
                "summary": f"Commit {len(req.files)} file(s) to '{req.branch}' on {req.repo}: {[f.path for f in req.files]}. Ask user, then resend confirm=true."}
    try:
        head = _gh("GET", f"/repos/{req.repo}/git/ref/heads/{req.branch}", expected=(200,))["object"]["sha"]
        created = False
    except HTTPException as e:
        if e.status_code != 404:
            raise
        dsha = _gh("GET", f"/repos/{req.repo}/git/ref/heads/{default}", expected=(200,))["object"]["sha"]
        head = _gh("POST", f"/repos/{req.repo}/git/refs",
                   json={"ref": f"refs/heads/{req.branch}", "sha": dsha}, expected=(201,))["object"]["sha"]
        created = True
    tree = []
    for f in req.files:
        blob = _gh("POST", f"/repos/{req.repo}/git/blobs",
                   json={"content": _gh_b64.b64encode(f.content.encode()).decode(), "encoding": "base64"},
                   expected=(201,))
        tree.append({"path": f.path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    base_tree = _gh("GET", f"/repos/{req.repo}/git/commits/{head}", expected=(200,))["tree"]["sha"]
    new_tree = _gh("POST", f"/repos/{req.repo}/git/trees",
                   json={"base_tree": base_tree, "tree": tree}, expected=(201,))
    commit = _gh("POST", f"/repos/{req.repo}/git/commits",
                 json={"message": req.message, "tree": new_tree["sha"], "parents": [head]}, expected=(201,))
    _gh("PATCH", f"/repos/{req.repo}/git/refs/heads/{req.branch}",
        json={"sha": commit["sha"]}, expected=(200,))
    return {"status": "committed", "branch": req.branch, "branch_created": created,
            "commit_sha": commit["sha"], "commit_url": commit["html_url"],
            "files": [f.path for f in req.files]}

@app.post("/github/pr")
def create_github_pr(req: _GhPR):
    """Open a pull request head -> base. WRITE - needs confirm=true."""
    _gh_check_repo(req.repo)
    base = req.base or _gh_default(req.repo)
    if not req.confirm:
        return {"status": "needs_confirmation",
                "summary": f"Open PR '{req.title}' on {req.repo}: {req.head} -> {base}. Ask user, then resend confirm=true."}
    try:
        pr = _gh("POST", f"/repos/{req.repo}/pulls",
                 json={"title": req.title, "head": req.head, "base": base,
                       "body": req.body, "draft": req.draft}, expected=(201,))
        return {"status": "opened", "pr_number": pr["number"], "url": pr["html_url"]}
    except HTTPException as e:
        if e.status_code == 422:
            existing = _gh("GET", f"/repos/{req.repo}/pulls?state=open&head={req.head}", expected=(200,))
            if existing:
                return {"status": "exists", "pr_number": existing[0]["number"], "url": existing[0]["html_url"]}
        raise

# ===================== GITHUB COMMIT HISTORY =====================

@app.get("/github/commits")
def get_github_commits(
    repo: str,
    branch: str = "",
    per_page: int = 10,
    since: str = "",
    until: str = ""
):
    """
    List recent commits from a GitHub repository.
    READ-ONLY and safe to run automatically.

    Dates must be ISO 8601, for example:
    2026-09-01T00:00:00Z
    """
    _gh_check_repo(repo)

    per_page = max(1, min(int(per_page), 100))

    params = {
        "per_page": per_page,
        "page": 1,
    }

    if branch.strip():
        params["sha"] = branch.strip()

    if since.strip():
        params["since"] = since.strip()

    if until.strip():
        params["until"] = until.strip()

    commits = _gh(
        "GET",
        f"/repos/{repo}/commits",
        expected=(200,),
        params=params
    )

    results = []

    for item in commits:
        commit_data = item.get("commit") or {}
        author_data = commit_data.get("author") or {}
        committer_data = commit_data.get("committer") or {}
        github_author = item.get("author") or {}

        results.append({
            "sha": item.get("sha"),
            "short_sha": (item.get("sha") or "")[:7],
            "message": (commit_data.get("message") or "").splitlines()[0],
            "full_message": commit_data.get("message") or "",
            "author": {
                "name": author_data.get("name"),
                "email": author_data.get("email"),
                "date": author_data.get("date"),
                "github_login": github_author.get("login")
            },
            "committer": {
                "name": committer_data.get("name"),
                "email": committer_data.get("email"),
                "date": committer_data.get("date")
            },
            "url": item.get("html_url")
        })

    return {
        "repo": repo,
        "branch": branch or None,
        "count": len(results),
        "commits": results
    }

