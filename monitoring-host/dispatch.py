#!/usr/bin/python3 -I
"""Root-owned SSH forced-command dispatcher; runs as clusterai, NOT root."""
import os
import sys

ACTIONS = {"status", "logs", "probe", "install", "start", "restart"}
action = os.environ.get("SSH_ORIGINAL_COMMAND", "")
if action not in ACTIONS:
    sys.exit("Denied: only fixed monitoring actions are permitted")
os.execve("/usr/bin/sudo", ["sudo", "-n", "/usr/local/sbin/clustercloud-monitoring-helper", action],
          {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
