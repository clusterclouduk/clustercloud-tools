# Backup and resource-health correctness

This change is review/deploy-only; it does not enable backups, run production
commands in CI, or alter infrastructure configuration. `correctness.py` must be
deployed alongside `main.py`. Operation IDs and endpoint arguments are retained.

## Backup contract changes

* Compact inventory `backups` is now policy-authoritative: true, false, or null.
  `backup_status` is enabled/disabled/unknown; unknown includes a short error.
  `detail=true` intentionally remains the raw provider inventory, including its
  legacy features flag. Consumers must not treat that flag as policy evidence.
* Policy eligibility and post-action reads are validated and target the immutable
  Droplet ID. Empty, mismatched, malformed and unavailable policies fail closed.
* Both enable tools skip managed Kubernetes workers. This is a write exclusion,
  NOT an assertion about whether the node or its workloads have backups.
* Outcomes: already_enabled, verified_enabled, pending, failed,
  verification_unavailable, skipped_k8s_node.
* `success=true` is only returned for already_enabled or verified_enabled.
  Verification requires both a completed action and an enabled policy.
* `changed=false` means no action was submitted; `changed=true` means enablement
  was verified after completion; null means the change outcome is unknown.
* `action_requested` is false before submission, true after a valid action ID,
  and null when submission may have happened but no ID was obtained.
* Bulk results now include every inspected Droplet, including already-enabled,
  skipped and policy-error results. `missing_backups_found` counts only
  non-managed Droplets whose authoritative policy was observed disabled.
* Action completion with a still-disabled policy is verification_unavailable
  (possible propagation delay), not a reason to blindly resubmit the write.
* Policies are configuration evidence, not proof of completed backup images or
  restore success. Those require separate endpoints/verification.

## Health contract changes

The single and fleet tools share the same classification rules:

* inactive provider state: critical, even when metrics queries fail;
* observed resource threshold breach: attention;
* failed metrics collection without a known breach: metrics_error;
* complete usable CPU, RAM and filesystem percentages: healthy;
* some usable telemetry: partial_metrics;
* no usable telemetry: no_metrics.

`telemetry` exposes CPU/RAM/disk coverage. Fleet overall health is unknown for an
empty fleet or a fleet with no metrics; mixed/partial coverage is partial_metrics
unless an attention/critical/metrics_error finding takes precedence. The existing
fleet `attention` list and counts remain; incomplete-metrics count is added.

CPU remains calculated from the last two samples, explicitly labelled by
`cpu_calculation=last_two_samples`. The requested/clamped window is a retrieval
window, not a claim of sustained utilisation. Metric freshness, sustained load
thresholds, concurrent collection and per-metric partial error recovery are not
implemented in this PR.

## Verification and rollout

Run `python -m pip install -r requirements-test.txt` and then
`python -m unittest discover -s tests -v`. Tests mock subprocess/DO operations;
no doctl/kubectl installation or cloud credentials are required. The CI workflow
uses read-only repository permission and has no deployment steps.

Review the Actions result before merge. No local execution is claimed by the
chat author. Deploy both Python files together only after approval. Refresh the
tool connection if needed, then check an enabled policy, an unmonitored server,
and fleet health read-only. Do not test enable endpoints against production as a
smoke test. Roll back by restoring the previous deployed revision.

Incoming authentication/approval enforcement, audit logging, filesystem
confinement, GitHub write hardening and Velero/fleet-backup APIs remain separate
work. Existing chat approval requirements still apply to all write tools.
