from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import json

app = FastAPI(
    title="ClusterCloud Infrastructure Tools",
    description="Infrastructure tools for ClusterCloud AI - read-only by default; writes are confirmation-gated",
    version="1.1.2",
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

def _droplet_summary(d):
    features = d.get("features", [])
    image = d.get("image", {}) or {}
    dist = image.get("distribution") or ""
    image_name = image.get("name") or ""
    version = image_name.split(" ")[0] if image_name[:1].isdigit() else ""
    networks = d.get("networks", {}).get("v4", [])
    public_ips = [n.get("ip_address") for n in networks if n.get("type") == "public"]

    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "size": d.get("size_slug"),
        "vcpus": d.get("vcpus"),
        "memory_mb": d.get("memory"),
        "disk_gb": d.get("disk"),
        "status": d.get("status"),
        "region": (d.get("region") or {}).get("slug"),
        "public_ips": public_ips,
        "os": f"{dist} {version}".strip(),
        "backups": "backups" in features,
        "monitoring": "monitoring" in features,
        "volumes": len(d.get("volume_ids", [])),
        "created": (d.get("created_at") or "")[:10],
        "tags": d.get("tags", []),
        "usd_mo": (d.get("size") or {}).get("price_monthly"),
    }

@app.get(
    "/digitalocean/droplets",
    operation_id="list_digitalocean_droplets",
    summary="List DigitalOcean Droplets (compact summary by default)",
    description=(
        "List Droplets compactly: name, size, public IPs, OS, backups and "
        "monitoring flags, volume count, cost, created date. Use as the "
        "default for fleet questions. detail=true returns the full raw API "
        "response; droplet=<name or id> narrows to a single Droplet."
    ),
)
def list_droplets(detail: bool = False, droplet: str = ""):
    droplets = json.loads(run([
        "doctl",
        "compute",
        "droplet",
        "list",
        "--output",
        "json",
    ]))

    if droplet:
        droplets = [
            d for d in droplets
            if str(d.get("id")) == str(droplet)
            or d.get("name", "").lower() == droplet.lower()
        ]

        if not droplets:
            raise HTTPException(
                status_code=404,
                detail=f"Droplet '{droplet}' not found"
            )

    if detail:
        return {"count": len(droplets), "droplets": droplets}

    return {
        "count": len(droplets),
        "droplets": [_droplet_summary(d) for d in droplets]
    }

# KUBERNETES

@app.get(
    "/kubernetes/nodes",
    operation_id="get_kubernetes_nodes",
    summary="List Kubernetes nodes (compact summary by default)",
    description=(
        "List cluster nodes compactly: name, ready, memory/disk/PID "
        "pressure flags, created date. detail=true returns the full raw "
        "API response."
    ),
)
def nodes(detail: bool = False):
    raw = json.loads(run([
        "kubectl",
        "get",
        "nodes",
        "-o",
        "json",
    ]))

    if detail:
        return raw

    items = raw.get("items", [])

    out = []

    for node in items:
        conditions = {
            c.get("type"): c.get("status")
            for c in node.get("status", {}).get("conditions", [])
        }

        out.append({
            "name": node["metadata"]["name"],
            "ready": conditions.get("Ready") == "True",
            "memory_pressure": conditions.get("MemoryPressure") == "True",
            "disk_pressure": conditions.get("DiskPressure") == "True",
            "pid_pressure": conditions.get("PIDPressure") == "True",
            "created": (node["metadata"].get("creationTimestamp") or "")[:10],
        })

    return {"count": len(out), "nodes": out}

@app.get(
    "/kubernetes/namespaces",
    operation_id="get_kubernetes_namespaces",
    summary="List Kubernetes namespaces (compact summary by default)",
    description=(
        "List namespaces compactly: name, status, created date. "
        "detail=true returns the full raw API response."
    ),
)
def namespaces(detail: bool = False):
    raw = json.loads(run([
        "kubectl",
        "get",
        "namespaces",
        "-o",
        "json",
    ]))

    if detail:
        return raw

    items = raw.get("items", [])

    return {
        "count": len(items),
        "namespaces": [
            {
                "name": i["metadata"]["name"],
                "status": i.get("status", {}).get("phase"),
                "created": (i["metadata"].get("creationTimestamp") or "")[:10],
            }
            for i in items
        ]
    }

@app.get(
    "/kubernetes/pods",
    operation_id="get_kubernetes_pods",
    summary="List Kubernetes pods (compact summary by default)",
    description=(
        "List pods compactly: name, phase, ready, restarts, node, age, "
        "volume types, resource-requests flag. detail=true returns the "
        "full raw API response."
    ),
)
def pods(namespace: str = "", detail: bool = False):
    cmd = ["kubectl", "get", "pods"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    raw = json.loads(run(cmd))

    if detail:
        return raw

    items = raw.get("items", [])

    return {
        "namespace": namespace or "all",
        "count": len(items),
        "pods": [_pod_summary(item) for item in items]
    }

@app.get(
    "/kubernetes/events",
    operation_id="get_kubernetes_events",
    summary="List Kubernetes events (compact, newest first)",
    description=(
        "List recent events compactly: type, reason, object, message "
        "(trimmed), count, last seen. Newest first, capped at 100 by "
        "default. detail=true returns the full raw API response; limit=N "
        "changes the cap (max 500)."
    ),
)
def events(namespace: str = "", detail: bool = False, limit: int = 100):
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

    raw = json.loads(run(cmd))

    if detail:
        return raw

    items = raw.get("items", [])

    limit = max(1, min(limit, 500))

    # kubectl sorts oldest-first; show newest first
    recent = list(reversed(items))[:limit]

    return {
        "namespace": namespace or "all",
        "total": len(items),
        "showing": len(recent),
        "events": [
            {
                "type": e.get("type"),
                "reason": e.get("reason"),
                "object": "{}/{}".format(
                    (e.get("involvedObject") or {}).get("kind"),
                    (e.get("involvedObject") or {}).get("name"),
                ),
                "message": (e.get("message") or "")[:160],
                "count": e.get("count"),
                "last_seen": e.get("lastTimestamp"),
            }
            for e in recent
        ]
    }

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
    operation_id="get_kubernetes_deployments",
    summary="List Kubernetes deployments (compact summary by default)",
    description=(
        "List deployments compactly: namespace, name, replicas, ready and "
        "available counts, created date. detail=true returns the full raw "
        "API response."
    ),
)
def deployments(namespace: str = "", detail: bool = False):
    cmd = ["kubectl", "get", "deployments"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    raw = json.loads(run(cmd))

    if detail:
        return raw

    items = raw.get("items", [])

    return {
        "namespace": namespace or "all",
        "count": len(items),
        "deployments": [
            {
                "namespace": i["metadata"]["namespace"],
                "name": i["metadata"]["name"],
                "replicas": i.get("spec", {}).get("replicas"),
                "ready": i.get("status", {}).get("readyReplicas", 0),
                "available": i.get("status", {}).get("availableReplicas", 0),
                "created": (i["metadata"].get("creationTimestamp") or "")[:10],
            }
            for i in items
        ]
    }

@app.get(
    "/kubernetes/services",
    operation_id="get_kubernetes_services",
    summary="List Kubernetes services (compact summary by default)",
    description=(
        "List services compactly: namespace, name, type, cluster IP, "
        "external IPs, ports. detail=true returns the full raw API "
        "response."
    ),
)
def services(namespace: str = "", detail: bool = False):
    cmd = ["kubectl", "get", "services"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    raw = json.loads(run(cmd))

    if detail:
        return raw

    items = raw.get("items", [])

    def _external_ips(svc):
        ingress = (svc.get("status", {}).get("loadBalancer") or {}).get("ingress") or []
        return [e.get("ip") or e.get("hostname") for e in ingress]

    return {
        "namespace": namespace or "all",
        "count": len(items),
        "services": [
            {
                "namespace": i["metadata"]["namespace"],
                "name": i["metadata"]["name"],
                "type": i.get("spec", {}).get("type"),
                "cluster_ip": i.get("spec", {}).get("clusterIP"),
                "external_ips": _external_ips(i),
                "ports": [
                    "{}/{}".format(p.get("port"), p.get("protocol", "TCP"))
                    for p in i.get("spec", {}).get("ports", [])
                ],
            }
            for i in items
        ]
    }

@app.get(
    "/kubernetes/ingresses",
    operation_id="get_kubernetes_ingresses",
    summary="List Kubernetes ingresses (compact summary by default)",
    description=(
        "List ingresses compactly: namespace, name, class, hosts, TLS "
        "secrets. detail=true returns the full raw API response."
    ),
)
def ingresses(namespace: str = "", detail: bool = False):
    cmd = ["kubectl", "get", "ingress"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    raw = json.loads(run(cmd))

    if detail:
        return raw

    items = raw.get("items", [])

    def _hosts(ing):
        hosts = []

        for rule in ing.get("spec", {}).get("rules", []):
            h = rule.get("host")

            if h and h not in hosts:
                hosts.append(h)

        return hosts

    return {
        "namespace": namespace or "all",
        "count": len(items),
        "ingresses": [
            {
                "namespace": i["metadata"]["namespace"],
                "name": i["metadata"]["name"],
                "class": (i.get("spec") or {}).get("ingressClassName"),
                "hosts": _hosts(i),
                "tls_secrets": [
                    t.get("secretName")
                    for t in (i.get("spec") or {}).get("tls", [])
                    if t.get("secretName")
                ],
            }
            for i in items
        ]
    }

@app.get(
    "/kubernetes/pvcs",
    operation_id="get_kubernetes_pvcs",
    summary="List Kubernetes PVCs (compact summary by default)",
    description=(
        "List persistent volume claims compactly: namespace, name, status, "
        "capacity, storage class, bound volume. detail=true returns the "
        "full raw API response."
    ),
)
def pvcs(namespace: str = "", detail: bool = False):
    cmd = ["kubectl", "get", "pvc"]

    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["-A"]

    cmd += ["-o", "json"]

    raw = json.loads(run(cmd))

    if detail:
        return raw

    items = raw.get("items", [])

    return {
        "namespace": namespace or "all",
        "count": len(items),
        "pvcs": [
            {
                "namespace": i["metadata"]["namespace"],
                "name": i["metadata"]["name"],
                "status": (i.get("status") or {}).get("phase"),
                "capacity": ((i.get("status") or {}).get("capacity") or {}).get("storage"),
                "storage_class": (i.get("spec") or {}).get("storageClassName"),
                "volume": (i.get("spec") or {}).get("volumeName"),
            }
            for i in items
        ]
    }

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

def _run_doctl_action(cmd):
    """
    Run a doctl action command and return the parsed action object.

    Raises if doctl returns anything other than a valid action, so a
    failed action can never be reported as success. (The previous
    enable-backups bug: 'doctl compute droplet backup' is not a valid
    subcommand - doctl printed help text, enabled nothing, and the
    endpoint still returned success.)
    """
    output = run(cmd)

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail="doctl returned non-JSON output; action was NOT applied"
        )

    action = data[0] if isinstance(data, list) and data else data

    if not isinstance(action, dict) or not action.get("id"):
        raise HTTPException(
            status_code=502,
            detail="doctl returned no valid action; action was NOT applied"
        )

    return action


def _wait_for_action(action_id, timeout_s: int = 30):
    """
    Poll a DigitalOcean action until it completes.

    Returns the final action status ('completed', 'errored', or
    'in-progress' if still pending at timeout).
    """
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        action = _run_doctl_action([
            "doctl",
            "compute",
            "action",
            "get",
            str(action_id),
            "--output",
            "json",
        ])

        if action.get("status") in ("completed", "errored"):
            return action.get("status")

        time.sleep(2)

    return "in-progress"


def _is_k8s_node(d):
    """
    Droplets tagged k8s* are DOKS-managed nodes. DigitalOcean no-ops
    droplet-backup enables on them, and cluster state is covered by
    Velero, so backup tooling should skip them.
    """
    return any(
        str(t).startswith("k8s")
        for t in d.get("tags", [])
    )


@app.post(
    "/digitalocean/droplet/enable-backups",
    operation_id="enable_digitalocean_droplet_backups",
    summary="Enable backups on a DigitalOcean Droplet",
    description=(
        "Enable DigitalOcean backups for a specific Droplet by name or ID. "
        "This is a write action and should only be called after explicit user "
        "confirmation. Waits for the action to complete and returns the "
        "verified backup state."
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

    action = _run_doctl_action([
        "doctl",
        "compute",
        "droplet-action",
        "enable-backups",
        str(d["id"]),
        "--output",
        "json",
    ])

    action_status = _wait_for_action(action["id"])

    updated = resolve_droplet(d["name"])

    return {
        "changed": True,
        "droplet": d["name"],
        "id": d["id"],
        "status": "backup_enable_requested",
        "action_id": action["id"],
        "action_status": action_status,
        "backups_enabled": "backups" in updated.get("features", []),
    }


@app.post(
    "/digitalocean/droplets/enable-missing-backups",
    operation_id="enable_missing_digitalocean_backups",
    summary="Enable backups on all DigitalOcean Droplets missing backups",
    description=(
        "Find every DigitalOcean Droplet that does not have backups enabled "
        "and enable backups on each one. Skips DOKS-managed nodes (k8s-tagged) "
        "- cluster state is covered by Velero. This is a write action and "
        "must only be used after explicit user confirmation. Waits for each "
        "action and reports the verified backup state."
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

    k8s_skipped = [
        d["name"] for d in droplets
        if _is_k8s_node(d)
        and "backups" not in d.get("features", [])
    ]

    missing = [
        d for d in droplets
        if "backups" not in d.get("features", [])
        and not _is_k8s_node(d)
    ]

    results = []

    for d in missing:
        try:
            action = _run_doctl_action([
                "doctl",
                "compute",
                "droplet-action",
                "enable-backups",
                str(d["id"]),
                "--output",
                "json",
            ])

            action_status = _wait_for_action(action["id"])

            results.append({
                "droplet": d["name"],
                "id": d["id"],
                "success": True,
                "action_id": action["id"],
                "action_status": action_status,
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
        "skipped_k8s_nodes": k8s_skipped,
        "results": results
    }


@app.get(
    "/digitalocean/droplet/backup-policies",
    operation_id="get_digitalocean_droplet_backup_policies",
    summary="Get the authoritative backup policy for a DigitalOcean Droplet",
    description=(
        "Returns the backup policy for a Droplet by name or ID. The droplet "
        "'features' backups flag does not reflect DigitalOcean's newer "
        "backup-policy system (verified live: console-enabled droplets still "
        "show features backups=false), so use this to confirm whether "
        "backups are actually enabled and when the next backup runs."
    ),
)
def droplet_backup_policies(droplet: str):
    d = resolve_droplet(droplet)

    output = run([
        "doctl",
        "compute",
        "droplet",
        "backup-policies",
        "get",
        str(d["id"]),
        "--output",
        "json",
    ])

    try:
        policies = json.loads(output)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail="doctl returned non-JSON output; backup policy unavailable"
        )

    if isinstance(policies, dict):
        policies = policies.get("backup_policies", [policies])

    if not isinstance(policies, list):
        raise HTTPException(
            status_code=502,
            detail="unexpected backup policy response shape; backup policy unavailable"
        )

    return {
        "droplet": d["name"],
        "id": d["id"],
        "policies": policies,
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


def _pod_volume_types(spec):
    types = []

    for v in spec.get("volumes", []):
        for key, label in [
            ("emptyDir", "emptyDir"),
            ("persistentVolumeClaim", "pvc"),
            ("configMap", "configMap"),
            ("secret", "secret"),
            ("hostPath", "hostPath"),
            ("projected", "projected"),
        ]:
            if key in v:
                if label not in types:
                    types.append(label)
                break

    return types


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
        "volume_types": _pod_volume_types(spec),
        "resources_set": any(
            bool((c.get("resources") or {}).get("requests"))
            for c in spec.get("containers", [])
        ),
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
