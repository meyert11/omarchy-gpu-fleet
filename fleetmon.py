#!/usr/bin/env python3
"""GPU Fleet collector.

Keeps one long-lived SSH session per GPU host. Each session runs agent.py
(sent on stdin, nothing installed remotely) and streams one JSON sample per
interval. The merged view is written atomically to a state file that the
Omarchy shell service watches.

Hosts come from ~/.config/omarchy/gpu-fleet.json if present:
  {"interval": 2, "hosts": [{"name": "desktop", "ssh": "local"},
                            {"name": "gpu-box", "ssh": "user@10.0.0.5"}]}
  "ssh" is anything `ssh` accepts as a destination (user@host or a ~/.ssh/config
  alias); "local" runs the agent on this machine without SSH. Without a config
  file only the local machine is shown.

  fleetmon.py --state-file PATH   run as the shell service
  fleetmon.py --once              print one merged snapshot and exit
"""
import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.expanduser("~/.config/omarchy/gpu-fleet.json")
DEFAULT_HOSTS = [{"name": (socket.gethostname().split(".")[0] or "local")[:32], "ssh": "local"}]
# A destination, never an option: blocks "-oProxyCommand=..." style values.
TARGET_RE = re.compile(r"^(local|[A-Za-z0-9_.][A-Za-z0-9_.@:%\[\]-]{0,252})$")
MAX_LINE = 256 * 1024
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new", "-T"]


def load_config():
    """Return (config, error). Problems are reported, never fatal: a bad file
    falls back to the local machine and the panel shows what went wrong."""
    cfg = {"interval": 2.0, "hosts": DEFAULT_HOSTS}
    try:
        with open(CONFIG) as f:
            user = json.load(f)
    except FileNotFoundError:
        return cfg, ""
    except (OSError, ValueError) as e:
        return cfg, "%s: %s" % (os.path.basename(CONFIG), e)
    if not isinstance(user, dict):
        return cfg, "%s: top level must be an object" % os.path.basename(CONFIG)

    problems = []
    if isinstance(user.get("interval"), (int, float)):
        cfg["interval"] = max(0.5, min(30.0, float(user["interval"])))
    if isinstance(user.get("hosts"), list) and user["hosts"]:
        hosts, names = [], set()
        for i, h in enumerate(user["hosts"]):
            name = str(h.get("name", ""))[:32] if isinstance(h, dict) else ""
            if not name or not TARGET_RE.match(str(h.get("ssh", ""))):
                problems.append("host %d skipped (needs a name and a valid ssh target)" % (i + 1))
            elif name in names:  # tabs are keyed by name
                problems.append("duplicate host name %r skipped" % name)
            else:
                names.add(name)
                hosts.append({"name": name, "ssh": str(h["ssh"])})
        if hosts:
            cfg["hosts"] = hosts
    return cfg, "; ".join(problems)


# The shell can kill the collector outright (e.g. `omarchy restart shell`);
# without PR_SET_PDEATHSIG its ssh sessions are orphaned and stream forever.
# libc is resolved here, once: preexec_fn runs in a freshly forked child of a
# multi-threaded process, where loading libraries could deadlock.
try:
    import ctypes
    _prctl = ctypes.CDLL(None, use_errno=True).prctl
except (OSError, AttributeError):
    _prctl = None
PR_SET_PDEATHSIG = 1


def die_with_parent():
    """preexec_fn: SIGTERM the child when its parent thread dies. Host threads
    live as long as the collector, so this tracks the process lifetime."""
    if _prctl is not None:
        _prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


class Host:
    """One monitored machine: a reconnecting agent session on its own thread.

    Fields read by the main thread (status, error, last_seen, sample) are each
    replaced by a single assignment, so no lock is needed for a snapshot.
    """

    def __init__(self, spec, interval, agent_src, on_update):
        self.name = str(spec["name"])[:32]
        self.target = str(spec["ssh"])
        self.interval = interval
        self.agent_src = agent_src
        self.on_update = on_update
        self.status = "connecting"
        self.error = ""
        self.last_seen = 0.0
        self.sample = None
        self.proc = None
        self.stop = False

    def snapshot(self, now):
        status = self.status
        if status == "ok" and now - self.last_seen > 3 * self.interval + 5:
            status = "stale"
        return {"name": self.name, "target": self.target, "status": status,
                "error": self.error, "lastSeen": self.last_seen, "sample": self.sample}

    def run(self):
        backoff = 3
        while not self.stop:
            started = time.time()
            self._session()
            if self.stop:
                return
            if time.time() - started > 60:
                backoff = 3
            self.status = "down"
            self.on_update()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _session(self):
        if self.target == "local":
            cmd = [sys.executable, "-", str(self.interval)]
        else:
            cmd = ["ssh"] + SSH_OPTS + ["--", self.target, "python3", "-", str(self.interval)]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1,
                                         preexec_fn=die_with_parent)
        except OSError as e:
            self.error = str(e)[:200]
            return
        # Drain stderr concurrently: a chatty host filling the pipe would
        # otherwise block ssh and freeze the session. Keep the last line.
        last_err = []

        def drain():
            for err_line in self.proc.stderr:
                if err_line.strip():
                    last_err[:] = [err_line.strip()[:200]]

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        try:
            self.proc.stdin.write(self.agent_src)
            self.proc.stdin.close()
        except OSError:
            pass
        for line in self.proc.stdout:
            if self.stop:
                break
            if len(line) > MAX_LINE:
                continue
            try:
                sample = json.loads(line)
            except ValueError:
                continue
            if not isinstance(sample, dict) or not isinstance(sample.get("gpus"), list):
                continue
            self.sample = sample
            self.last_seen = time.time()
            self.status = "ok"
            self.error = ""
            self.on_update()
        self.proc.wait()
        drainer.join(timeout=2)
        if last_err:
            self.error = last_err[0]
        elif self.proc.returncode:
            self.error = "ssh exited %d" % self.proc.returncode

    def kill(self):
        self.stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def write_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-file")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    cfg, config_error = load_config()
    with open(os.path.join(HERE, "agent.py")) as f:
        agent_src = f.read()

    changed = threading.Event()
    hosts = [Host(h, cfg["interval"], agent_src, changed.set) for h in cfg["hosts"]]

    def merged():
        now = time.time()
        return {"schema": 1, "t": now, "interval": cfg["interval"],
                "configError": config_error,
                "hosts": [h.snapshot(now) for h in hosts]}

    def shutdown(*_):
        for h in hosts:
            h.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for h in hosts:
        threading.Thread(target=h.run, daemon=True).start()

    if args.once:
        deadline = time.time() + 20
        while time.time() < deadline and any(h.sample is None and h.status != "down"
                                             for h in hosts):
            time.sleep(0.2)
        print(json.dumps(merged(), indent=2))
        shutdown()

    if not args.state_file:
        ap.error("--state-file or --once is required")
    os.makedirs(os.path.dirname(args.state_file), exist_ok=True)
    parent = os.getppid()
    while True:
        changed.wait(timeout=cfg["interval"])
        changed.clear()
        write_state(args.state_file, merged())
        if os.getppid() != parent:  # shell went away
            shutdown()
        time.sleep(0.25)  # coalesce bursts from several hosts


if __name__ == "__main__":
    main()
