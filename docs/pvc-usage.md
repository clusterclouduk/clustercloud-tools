# /kubernetes/pvc-usage — per-PVC used space

Read-only endpoint (`get_kubernetes_pvc_usage`), added in v1.1.3.

## Why

Per-PVC used space was invisible with existing tooling (verified 2026-09-07):

- PVC objects store requested capacity only — 246Gi provisioned across 15 PVCs
- DO droplet metrics for the DOKS node report only the root filesystem; the
  15 attached block volumes are not broken out
- Pod-file inspection paths exclude database data dirs, and 7 of the 15
  volumes are MariaDB/Postgres

## Design

Query the kubelet stats-summary API —
`kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary` — one call per
node returns `usedBytes`/`capacityBytes` for every PVC-backed volume, joined
via `pvcRef`. No pod exec. Covers database volumes. Compact output per the
architecture KB tool-design rule.

Per PVC: namespace, name, pod, node, capacity/used/available bytes,
`used_percent`, `flag_over_80pct`. Plus totals and a `no_stats_available`
list (volumes the kubelet reports nothing for — future df-exec fallback
candidates).

## Verify (post-deploy)

Expect `count=15`; durhamcricket `openlitespeed-pvc` capacity ≈ 80Gi.
