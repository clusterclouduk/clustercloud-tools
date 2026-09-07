import json
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

import main
from correctness import (
    classify_resource_health, enable_backup, fleet_health_summary,
    inventory_backup_fields, read_backup_policies,
)


DROPLET = {"id": 123, "name": "web-01", "status": "active", "features": [], "tags": [], "vcpus": 2, "memory": 2048, "disk": 60}


def policy(enabled, droplet_id=123):
    return json.dumps([{"droplet_id": droplet_id, "backup_enabled": enabled}])


def metric(value):
    return {"data": {"result": [{"metric": {}, "values": [[100, str(value)]]}]}}


EMPTY = {"data": {"result": []}}


class BackupTests(unittest.TestCase):
    def test_policy_response_shapes(self):
        record = {"droplet_id": 123, "backup_enabled": True}
        for shape in ([record], record, {"backup_policies": [record]}):
            with self.subTest(shape=shape):
                self.assertTrue(read_backup_policies(123, Mock(return_value=json.dumps(shape)))[0]["backup_enabled"])

    def test_malformed_policies_are_unknown(self):
        for value in ("help text", "null", "[]", "{}", policy(True, 999), '[{"droplet_id":123,"backup_enabled":"false"}]'):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException):
                    read_backup_policies(123, Mock(return_value=value))
                fields = inventory_backup_fields(DROPLET, Mock(return_value=value))
                self.assertIsNone(fields["backups"])
                self.assertEqual(fields["backup_status"], "unknown")

    def test_inventory_ignores_legacy_flag(self):
        for legacy, enabled in (([], True), (["backups"], False)):
            with self.subTest(legacy=legacy):
                droplet = dict(DROPLET, features=legacy)
                with patch.object(main, "run", return_value=policy(enabled)):
                    self.assertIs(main._droplet_summary(droplet)["backups"], enabled)

    def test_already_enabled_never_submits(self):
        submit = Mock()
        result = enable_backup(DROPLET, Mock(return_value=policy(True)), submit, Mock(), lambda d: False)
        self.assertEqual(result["status"], "already_enabled")
        self.assertTrue(result["success"])
        self.assertFalse(result["changed"])
        submit.assert_not_called()

    def test_unknown_eligibility_never_submits(self):
        for reader in (Mock(return_value="[]"), Mock(side_effect=HTTPException(503, "unavailable"))):
            submit = Mock()
            result = enable_backup(DROPLET, reader, submit, Mock(), lambda d: False)
            self.assertEqual(result["status"], "verification_unavailable")
            self.assertFalse(result["success"])
            self.assertFalse(result["action_requested"])
            submit.assert_not_called()

    def test_action_outcomes(self):
        for state, expected in (("completed", "verified_enabled"), ("errored", "failed"), ("in-progress", "pending")):
            with self.subTest(state=state):
                reader = Mock(side_effect=[policy(False), policy(True)])
                result = enable_backup(DROPLET, reader, Mock(return_value={"id": 7}), Mock(return_value=state), lambda d: False)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["success"], state == "completed")
                self.assertEqual(reader.call_count, 2 if state == "completed" else 1)
                self.assertEqual(result["action_id"], 7)
                if state == "completed":
                    self.assertIs(result["backups_enabled"], True)
                    self.assertTrue(result["changed"])
                else:
                    self.assertIsNone(result["changed"])

    def test_completed_requires_policy_verification(self):
        for after in (policy(False), "[]", HTTPException(503, "unavailable")):
            with self.subTest(after=after):
                result = enable_backup(DROPLET, Mock(side_effect=[policy(False), after]), Mock(return_value={"id": 7}), Mock(return_value="completed"), lambda d: False)
                self.assertFalse(result["success"])
                self.assertEqual(result["status"], "verification_unavailable")
                self.assertIsNone(result["changed"])

    def test_submission_or_poll_timeout_is_not_success(self):
        for submit, wait in ((Mock(side_effect=TimeoutError("timeout")), Mock()), (Mock(return_value={"id": 7}), Mock(side_effect=TimeoutError("timeout")))):
            result = enable_backup(DROPLET, Mock(return_value=policy(False)), submit, wait, lambda d: False)
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "verification_unavailable")
            self.assertIsNone(result["changed"])

    def test_k8s_skip_never_reads_or_writes(self):
        reader, submit = Mock(), Mock()
        result = enable_backup(dict(DROPLET, tags=["k8s:worker"]), reader, submit, Mock(), main._is_k8s_node)
        self.assertEqual(result["status"], "skipped_k8s_node")
        reader.assert_not_called()
        submit.assert_not_called()

    def test_single_endpoint_uses_policy_not_features(self):
        with patch.object(main, "resolve_droplet", return_value=dict(DROPLET, features=["backups"])), patch.object(main, "run", side_effect=[policy(False), policy(True)]), patch.object(main, "_run_doctl_action", return_value={"id": 7}) as submit, patch.object(main, "_wait_for_action", return_value="completed"):
            result = main.enable_droplet_backups("web-01")
            self.assertEqual(result["status"], "verified_enabled")
            submit.assert_called_once()
            self.assertIn("123", submit.call_args.args[0])

    def test_policy_endpoint_preserves_shape(self):
        with patch.object(main, "resolve_droplet", return_value=DROPLET), patch.object(main, "run", return_value=policy(True)):
            result = main.droplet_backup_policies("123")
            self.assertEqual(result["id"], 123)
            self.assertTrue(result["policies"][0]["backup_enabled"])

    def test_bulk_skips_unknown_and_k8s_but_processes_eligible(self):
        droplets = [dict(DROPLET, id=123), dict(DROPLET, id=124, name="unknown"), dict(DROPLET, id=125, name="worker", tags=["k8s"]), dict(DROPLET, id=126, name="enabled")]
        def fake_run(cmd):
            if cmd[1:4] == ["compute", "droplet", "list"]:
                return json.dumps(droplets)
            target = cmd[5]
            if target == "123":
                return policy(False)
            if target == "124":
                return "[]"
            if target == "126":
                return policy(True, 126)
            raise AssertionError(f"Unexpected command {cmd}")
        with patch.object(main, "run", side_effect=fake_run), patch.object(main, "_run_doctl_action", return_value={"id": 7}) as submit, patch.object(main, "_wait_for_action", return_value="errored"):
            result = main.enable_missing_backups()
            self.assertEqual(result["missing_backups_found"], 1)
            self.assertEqual(result["skipped_k8s_nodes"], ["worker"])
            self.assertEqual([r["status"] for r in result["results"]], ["failed", "verification_unavailable", "skipped_k8s_node", "already_enabled"])
            submit.assert_called_once()


class HealthTests(unittest.TestCase):
    def classify(self, cpu=None, memory=None, filesystems=None, status="active", error=False):
        return classify_resource_health(status, cpu, memory, filesystems or [], error)

    def test_required_coverage(self):
        cases = [
            (None, None, [], "no_metrics"),
            (None, None, [{"size_gb": 60}], "no_metrics"),
            (10, None, [], "partial_metrics"),
            (10, 40, [], "partial_metrics"),
            (10, 40, [{"used_percent": 20}, {"used_percent": None}], "partial_metrics"),
            (10, 40, [{"used_percent": 20}], "healthy"),
            (90, None, [], "attention"),
            (10, 90, [{"used_percent": 20}], "attention"),
            (10, 40, [{"used_percent": 90}], "attention"),
        ]
        for cpu, memory, fs, expected in cases:
            with self.subTest(expected=expected, fs=fs):
                self.assertEqual(self.classify(cpu, memory, fs)["health"], expected)

    def test_invalid_percentages_are_missing(self):
        for value in (float("nan"), float("inf"), -1, 101, True, "10"):
            self.assertEqual(self.classify(cpu=value)["health"], "no_metrics")

    def test_inactive_takes_precedence_over_metrics_error(self):
        self.assertEqual(self.classify(status="off", error=True)["health"], "critical")
        self.assertEqual(self.classify(error=True)["health"], "metrics_error")

    def test_fleet_aggregation(self):
        none = self.classify()
        healthy = self.classify(10, 40, [{"used_percent": 20}])
        for items, expected in (([], "unknown"), ([none], "unknown"), ([healthy, none], "partial_metrics"), ([healthy], "healthy"), ([self.classify(error=True)], "attention")):
            self.assertEqual(fleet_health_summary(items)["overall_health"], expected)

    def test_single_endpoint_no_metrics(self):
        with patch.object(main, "resolve_droplet", return_value=DROPLET), patch.object(main, "do_api", return_value=EMPTY):
            result = main.droplet_metrics_summary("123")
            self.assertEqual(result["health"], "no_metrics")
            self.assertEqual(result["cpu_calculation"], "last_two_samples")

    def test_single_endpoint_partial_metrics(self):
        def fake_api(path, params):
            if path.endswith("memory_total"):
                return metric(100)
            if path.endswith("memory_available"):
                return metric(50)
            return EMPTY
        with patch.object(main, "resolve_droplet", return_value=DROPLET), patch.object(main, "do_api", side_effect=fake_api):
            result = main.droplet_metrics_summary("123")
            self.assertEqual(result["health"], "partial_metrics")

    def test_single_endpoint_inactive_even_on_query_error(self):
        with patch.object(main, "resolve_droplet", return_value=dict(DROPLET, status="off")), patch.object(main, "do_api", side_effect=HTTPException(503, "unavailable")):
            result = main.droplet_metrics_summary("123")
            self.assertEqual(result["health"], "critical")
            self.assertIn("metrics_error", result)

    def test_fleet_endpoint_empty_and_missing(self):
        for droplets, expected in (([], "unknown"), ([DROPLET], "unknown")):
            with patch.object(main, "run", return_value=json.dumps(droplets)), patch.object(main, "do_api", return_value=EMPTY):
                result = main.check_all_droplet_health(1)
                self.assertEqual(result["overall_health"], expected)
                self.assertEqual(result["window_minutes"], 5)

    def test_fleet_inactive_error_still_critical(self):
        with patch.object(main, "run", return_value=json.dumps([dict(DROPLET, status="off")])), patch.object(main, "do_api", side_effect=HTTPException(503, "unavailable")):
            result = main.check_all_droplet_health()
            self.assertEqual(result["servers"][0]["health"], "critical")
            self.assertEqual(result["overall_health"], "attention")


if __name__ == "__main__":
    unittest.main()
