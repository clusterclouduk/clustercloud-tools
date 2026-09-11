import unittest
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import ssh_monitoring as m


class MonitoringSafetyTests(unittest.TestCase):
    def test_missing_token_fails_closed(self):
        with patch.dict(m.os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as error:
                m.authenticate(None)
            self.assertEqual(error.exception.status_code, 503)

    def test_bad_credentials(self):
        with patch.dict(m.os.environ, {"MONITORING_API_TOKEN": "x" * 32}):
            with self.assertRaises(HTTPException):
                m.authenticate(HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong"))

    def test_valid_credentials(self):
        with patch.dict(m.os.environ, {"MONITORING_API_TOKEN": "x" * 32}):
            m.authenticate(HTTPAuthorizationCredentials(scheme="Bearer", credentials="x" * 32))

    def test_confirmation_before_ssh(self):
        with patch.object(m, "ssh") as ssh:
            with self.assertRaises(HTTPException):
                m.repair(m.Repair(target="canary", action="start", confirm=False, approval_reference="test"))
            ssh.assert_not_called()

    def test_write_disabled_before_ssh(self):
        with patch.object(m, "targets", return_value={"canary": {"writes_enabled": False}}), patch.object(m, "ssh") as ssh:
            with self.assertRaises(HTTPException):
                m.repair(m.Repair(target="canary", action="start", confirm=True, approval_reference="test"))
            ssh.assert_not_called()

    def test_unknown_target(self):
        with patch.object(m, "targets", return_value={}), patch.object(m, "ssh") as ssh:
            with self.assertRaises(HTTPException):
                m.diagnose("unknown")
            ssh.assert_not_called()

    def test_arbitrary_command_rejected(self):
        with patch.object(m.subprocess, "run") as run:
            with self.assertRaises(HTTPException):
                m.ssh("192.0.2.10", "status; id")
            run.assert_not_called()

    def test_host_injection_rejected(self):
        with patch.object(m.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                m.ssh("-oProxyCommand=id", "status")
            run.assert_not_called()

    def test_pinned_ssh_and_fixed_action(self):
        with patch.object(m.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"active":"active"}'
            self.assertEqual(m.ssh("192.0.2.10", "status")["active"], "active")
            argv = run.call_args.args[0]
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("IdentitiesOnly=yes", argv)
            self.assertEqual(argv[-2:], ["clusterai@192.0.2.10", "status"])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_timeout_is_not_success(self):
        with patch.object(m.subprocess, "run", side_effect=m.subprocess.TimeoutExpired("ssh", 240)):
            with self.assertRaises(HTTPException) as error:
                m.ssh("192.0.2.10", "start")
            self.assertEqual(error.exception.status_code, 504)

    def test_audit_failure_blocks_diagnostics(self):
        with patch.object(m, "targets", return_value={"canary": {"address": "192.0.2.10"}}), patch.object(m, "audit", side_effect=HTTPException(503, "unavailable")), patch.object(m, "ssh") as ssh:
            with self.assertRaises(HTTPException):
                m.diagnose("canary")
            ssh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
