#!/usr/bin/env python3
"""Verify the QA wrapper's Linux namespace containment before rig side effects."""
import os
from pathlib import Path
import stat
import sys


def isolation_errors(environment, current, init, sockets, init_name):
    if environment.get("QA_NETNS_ISOLATED") != "1":
        return ["L2_ISOLATED requires the QA namespace wrapper marker"]
    errors = []
    host = {name: environment.get(f"QA_HOST_{name.upper()}NS_ID") for name in ("net", "mnt", "pid")}
    if all(host.values()):
        if any(not current.get(name) or current[name] == host[name] for name in host):
            errors.append("network, mount and PID namespaces must differ from the wrapper's recorded host namespaces")
    elif (current.get("net") and current.get("mnt") and current["net"] != init.get("net")
          and current["mnt"] != init.get("mnt")):
        pass  # Host init is visible; private net + mount namespaces are proven.
    else:
        # With a remounted /proc, PID 1 is the wrapper in its private PID ns.
        # Require its explicit PID namespace attestation and masked host control
        # sockets as well; a marker alone is never sufficient.
        if (environment.get("QA_PRIVATE_PIDNS") != "1" or init_name not in ("unshare", "bash", "sh", "tini")
                or not current.get("pid") or current.get("pid") != init.get("pid")):
            errors.append("cannot verify isolated net/mount/PID containment")
    if any(sockets.values()):
        errors.append("host systemd/dbus control sockets must be hidden inside the private mount namespace")
    return errors


def verify():
    if sys.platform != "linux": return ["L2 isolation requires native Linux namespaces"]
    def namespaces(pid):
        result = {}
        for name in ("net", "mnt", "pid"):
            try: result[name] = os.readlink(f"/proc/{pid}/ns/{name}")
            except OSError: result[name] = None
        return result
    sockets = {}
    for name in ("/run/systemd/private", "/run/dbus/system_bus_socket"):
        try: sockets[name] = stat.S_ISSOCK(Path(name).stat().st_mode)
        except FileNotFoundError: sockets[name] = False
        except OSError: sockets[name] = True
    try: init_name = Path("/proc/1/comm").read_text().strip()
    except OSError: init_name = ""
    return isolation_errors(os.environ, namespaces("self"), namespaces("1"), sockets, init_name)


if __name__ == "__main__":
    errors = verify()
    if errors:
        print("Isolation rejected: " + "; ".join(errors), file=sys.stderr)
    raise SystemExit(2 if errors else 0)
