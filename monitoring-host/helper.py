#!/usr/bin/python3 -I
"""Install root-owned 0755. Ubuntu/Debian only; fixed actions, no shell/eval."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import syslog

ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "DEBIAN_FRONTEND": "noninteractive"}
BASE = Path("/opt/clustercloud-monitoring")


def run(args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=ENV)


def state():
    package = run(["/usr/bin/dpkg-query", "-W", "-f=${Status}|${Version}", "do-agent"])
    service = run(["/usr/bin/systemctl", "show", "do-agent.service", "--no-pager",
                   "--property=LoadState,ActiveState,SubState,UnitFileState"])
    values = dict(line.split("=", 1) for line in service.stdout.splitlines() if "=" in line)
    return {"package_installed": package.returncode == 0 and package.stdout.startswith("install ok installed|"),
            "package_version": package.stdout.split("|", 1)[-1].strip() if package.returncode == 0 else None,
            "load": values.get("LoadState"), "active": values.get("ActiveState"),
            "substate": values.get("SubState"), "enabled": values.get("UnitFileState")}


def trusted(path):
    # Validate the file AND every parent; clusterai must never control staged code.
    for item in [path, *path.parents]:
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("staging path is not root-owned and protected")
    if not path.is_file():
        raise ValueError("staging file required")


def install():
    if state()["package_installed"]:
        return {"ok": True, "changed": False, "note": "Already installed; use separately approved start/restart if needed"}
    package = BASE / "do-agent.deb"
    checksum = BASE / "do-agent.sha256"
    trusted(package)
    trusted(checksum)
    digest = checksum.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("expected bare SHA256 digest")
    with package.open("rb") as stream:
        actual = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            actual.update(block)
    if actual.hexdigest() != digest:
        raise ValueError("package checksum mismatch")
    metadata = run(["/usr/bin/dpkg-deb", "-f", str(package), "Package"])
    if metadata.returncode != 0 or metadata.stdout.strip() != "do-agent":
        raise ValueError("only do-agent packages permitted")
    # No repository changes, apt upgrade, dependency auto-resolution or downloaded shell scripts.
    result = run(["/usr/bin/dpkg", "--install", str(package)], timeout=150)
    if result.returncode:
        return {"ok": False, "changed": "possibly_partial", "note": "dpkg failed; operator must review package state before retry"}
    result = run(["/usr/bin/systemctl", "enable", "--now", "do-agent.service"], timeout=30)
    return {"ok": result.returncode == 0, "changed": True}


def execute(action):
    if action == "status":
        return state()
    if action == "logs":
        result = run(["/usr/bin/journalctl", "--unit=do-agent.service", "--no-pager", "--output=short-iso", "--lines=60"])
        return {"ok": result.returncode == 0, "untrusted_logs": result.stdout[-12000:]}
    if action == "probe":
        result = run(["/usr/bin/curl", "--noproxy", "*", "--proto", "=https", "--tlsv1.2",
                      "--connect-timeout", "5", "--max-time", "10", "--silent", "--output", "/dev/null",
                      "--write-out", "%{http_code}", "https://sonar.digitalocean.com/"])
        return {"transport_ok": result.returncode == 0, "http_status": result.stdout[:3],
                "note": "Fixed DNS/TCP/TLS reachability probe only; does not prove agent ingestion"}
    if action == "install":
        return install()
    if not state()["package_installed"]:
        return {"ok": False, "changed": False, "note": "Package absent; installation requires separate approval"}
    cmd = (["/usr/bin/systemctl", "enable", "--now", "do-agent.service"] if action == "start"
           else ["/usr/bin/systemctl", "restart", "do-agent.service"])
    result = run(cmd, timeout=30)
    return {"ok": result.returncode == 0, "changed": True}


def main():
    if os.geteuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in {"status", "logs", "probe", "install", "start", "restart"}:
        sys.exit("Denied")
    action = sys.argv[1]
    syslog.openlog("clustercloud-monitoring")
    syslog.syslog(syslog.LOG_NOTICE, "requested action=" + action)
    try:
        # Serialize host operations even across API workers/clients or a disconnected SSH session.
        fd = os.open("/run/clustercloud-monitoring.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = execute(action)
        syslog.syslog(syslog.LOG_NOTICE, "completed action=" + action + " ok=" + str(result.get("ok", "read")))
        print(json.dumps(result))
    except Exception as exc:
        syslog.syslog(syslog.LOG_ERR, "failed action=" + action + " type=" + type(exc).__name__)
        print(json.dumps({"ok": False, "outcome": "unknown_or_partial", "error_type": type(exc).__name__}))
        sys.exit(1)


if __name__ == "__main__":
    main()
