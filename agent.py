# GPU Fleet remote agent.
#
# Sent over SSH on stdin (`ssh host python3 - <interval>`), so nothing is
# installed on the GPU hosts. Python 3 stdlib only, read-only, unprivileged.
# Prints one JSON object per line every <interval> seconds:
#   host: hostname, cpu %, system RAM
#   gpus: AMD (amdgpu sysfs), Intel (i915/xe, DRM client counters) and NVIDIA
#         (nvidia-smi) cards with utilization, VRAM, GTT, temperature, power,
#         fan, clocks and per-GPU processes (GPU memory + host RSS of each
#         process, summed as the GPU's system RAM).
import glob
import json
import os
import shlex
import subprocess
import sys
import time

INTERVAL = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
PROC_EVERY = 2  # process scan every N samples
MIB = 1024 * 1024


def rd(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def num(value, scale=1.0):
    try:
        return float(value) / scale
    except (TypeError, ValueError):
        return None


_cpu_prev = None


def cpu_pct():
    global _cpu_prev
    line = rd("/proc/stat")
    if not line:
        return None
    parts = [int(x) for x in line.splitlines()[0].split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    prev, _cpu_prev = _cpu_prev, (idle, total)
    if not prev or total == prev[1]:
        return None
    return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def meminfo():
    info = {}
    for line in (rd("/proc/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        info[key] = num(rest.split()[0]) if rest.split() else None
    total, avail = info.get("MemTotal"), info.get("MemAvailable")
    if not total or avail is None:
        return None
    return {"usedMiB": round((total - avail) / 1024), "totalMiB": round(total / 1024)}


def rss_mib(pid):
    for line in (rd("/proc/%s/status" % pid) or "").splitlines():
        if line.startswith("VmRSS:"):
            return round(num(line.split()[1]) / 1024)
    return None


def comm(pid):
    return (rd("/proc/%s/comm" % pid) or "?")[:48]


def cmdline(pid):
    """Short command line: interpreter dropped, paths reduced to basenames."""
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            args = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return None
    if args and os.path.basename(args[0]).startswith("python") and len(args) > 1:
        args = args[1:]
        if args[0] == "-m" and len(args) > 1:
            args = args[1:]
    return " ".join(os.path.basename(a) if "/" in a and not a.startswith("-") else a
                    for a in args[:4])[:96]


# ------------------------------------------------------- AMD and Intel
_pci_names = {}
# Device IDs too new for the hosts' pci.ids (lspci prints "Device 7551").
KNOWN_AMD = {"0x7551": "Radeon AI PRO R9700", "0x73bf": "Radeon RX 6800/6900 (Navi 21)"}
INTEL_DRIVERS = ("i915", "xe")


def pci_name(slot, dev, known=None):
    if slot not in _pci_names:
        name = (known or {}).get(rd(dev + "/device") or "")
        if not name:
            try:
                out = subprocess.run(["lspci", "-mm", "-s", slot], capture_output=True,
                                     text=True, timeout=3).stdout
                fields = shlex.split(out)
                if len(fields) >= 4 and not fields[3].startswith("Device "):
                    name = fields[3]
                    if "[" in name:
                        name = name.split("[")[-1].rstrip("]")
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        _pci_names[slot] = name
    return _pci_names[slot]


def new_gpu(vendor, slot, name):
    return {
        "vendor": vendor, "slot": slot, "name": name, "shared": False,
        "util": None, "memUtil": None,
        "vramUsedMiB": None, "vramTotalMiB": None, "gttUsedMiB": None, "gttTotalMiB": None,
        "temp": None, "temps": {}, "tempSource": "gpu",
        "powerW": None, "powerCapW": None, "fanPct": None, "fanRpm": None, "sclkMhz": None,
        "procs": [],
    }


_energy_prev = {}


def read_hwmon(g, dev):
    """Temperatures, power, fan and clock from the card's hwmon, if it has one."""
    for hw in glob.glob(dev + "/hwmon/hwmon*"):
        for t in glob.glob(hw + "/temp*_input"):
            label = rd(t.replace("_input", "_label")) or os.path.basename(t)[:-6]
            val = num(rd(t), 1000)
            if val is not None:
                g["temps"][label] = round(val)
        power = num(rd(hw + "/power1_average") or rd(hw + "/power1_input"), 1e6)
        if power is None:  # Intel discrete cards only expose an energy counter
            uj, now = num(rd(hw + "/energy1_input")), time.monotonic()
            prev, _energy_prev[g["slot"]] = _energy_prev.get(g["slot"]), (uj, now)
            if uj is not None and prev and uj >= prev[0] and now > prev[1]:
                power = (uj - prev[0]) / 1e6 / (now - prev[1])
        g["powerW"] = power
        g["powerCapW"] = num(rd(hw + "/power1_cap") or rd(hw + "/power1_max"), 1e6)
        g["fanRpm"] = num(rd(hw + "/fan1_input"))
        pwm, pmax = num(rd(hw + "/pwm1")), num(rd(hw + "/pwm1_max")) or 255
        if pwm is not None:
            g["fanPct"] = round(100 * pwm / pmax)
        g["sclkMhz"] = num(rd(hw + "/freq1_input"), 1e6)
    t = g["temps"]
    g["temp"] = t.get("junction", t.get("edge", next(iter(t.values()), None)))


def drm_cards(vendor_id):
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        dev = card + "/device"
        if "-" not in os.path.basename(card) and rd(dev + "/vendor") == vendor_id:
            yield card, dev, os.path.basename(os.path.realpath(dev))


def amd_gpus():
    gpus = []
    for _card, dev, slot in drm_cards("0x1002"):
        if rd(dev + "/mem_info_vram_total") is None:
            continue
        g = new_gpu("AMD", slot, rd(dev + "/product_name") or pci_name(slot, dev, KNOWN_AMD)
                    or "AMD GPU")
        g.update({
            "util": num(rd(dev + "/gpu_busy_percent")),
            "memUtil": num(rd(dev + "/mem_busy_percent")),
            "vramUsedMiB": num(rd(dev + "/mem_info_vram_used"), MIB),
            "vramTotalMiB": num(rd(dev + "/mem_info_vram_total"), MIB),
            "gttUsedMiB": num(rd(dev + "/mem_info_gtt_used"), MIB),
            "gttTotalMiB": num(rd(dev + "/mem_info_gtt_total"), MIB),
        })
        read_hwmon(g, dev)
        gpus.append(g)
    return gpus


def pkg_temp():
    """CPU package temperature: the only sensor an integrated GPU shares."""
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        if rd(zone + "/type") == "x86_pkg_temp":
            val = num(rd(zone + "/temp"), 1000)
            return round(val) if val is not None else None
    return None


def intel_gpus():
    gpus = []
    for card, dev, slot in drm_cards("0x8086"):
        driver = os.path.basename(os.path.realpath(dev + "/driver"))
        if driver not in INTEL_DRIVERS:
            continue
        g = new_gpu("Intel", slot, pci_name(slot, dev) or "Intel GPU")
        g["shared"] = slot.startswith("0000:00:")  # integrated GPUs sit on the root bus
        g["sclkMhz"] = num(rd(card + "/gt_act_freq_mhz")
                           or rd(card + "/gt/gt0/rps_act_freq_mhz")
                           or next((rd(f) for f in glob.glob(dev + "/tile0/gt0/freq0/act_freq")),
                                   None))
        read_hwmon(g, dev)
        if g["temp"] is None and g["shared"]:
            g["temp"] = pkg_temp()
            if g["temp"] is not None:
                g["temps"]["package"] = g["temp"]
                g["tempSource"] = "package"
        gpus.append(g)
    return gpus


def drm_fdinfos():
    """Yield (pid, fdinfo path) for open /dev/dri/* files we may read.

    Checking the fd's link target first skips the thousands of non-GPU fds a
    desktop has open; reading every fdinfo file costs ~6x more CPU.
    """
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = "/proc/%s/fd" % pid
        try:
            fds = os.listdir(fd_dir)
        except OSError:  # not ours, or already gone
            continue
        for fd in fds:
            try:
                if os.readlink(fd_dir + "/" + fd).startswith("/dev/dri/"):
                    yield pid, "/proc/%s/fdinfo/%s" % (pid, fd)
            except OSError:
                continue


def drm_clients(slots):
    """One pass over readable DRM fds: [(slot, pid, client id, fields)], one per
    client (a client opened through several fds is counted once)."""
    clients, seen = [], set()
    for pid, path in drm_fdinfos():
        text = rd(path)
        if not text or "drm-pdev" not in text:
            continue
        fields = {}
        for line in text.splitlines():
            key, sep, val = line.partition(":")
            if sep:
                fields[key.strip()] = val.strip()
        slot, client = fields.get("drm-pdev", ""), fields.get("drm-client-id", "")
        if slot in slots and (slot, client) not in seen:
            seen.add((slot, client))
            clients.append((slot, int(pid), client, fields))
    return clients


def size_mib(value):
    """'123 KiB' / '4 MiB' / '512' (bytes) -> MiB."""
    parts = (value or "").split()
    size = num(parts[0]) if parts else None
    if size is None:
        return 0.0
    unit = parts[1] if len(parts) > 1 else "B"
    return size / {"B": MIB, "KiB": 1024, "MiB": 1, "GiB": 1 / 1024}.get(unit, MIB)


def client_mem_mib(g, fields):
    """GPU memory held by one client: VRAM, or shared system memory on an iGPU.
    The first key group present wins (newer kernels print both AMD spellings)."""
    if g["vendor"] == "AMD":
        groups = [("drm-memory-vram",), ("drm-resident-vram",)]
    elif g["shared"]:
        groups = [("drm-resident-system0", "drm-resident-stolen-system0"), ("drm-resident-gtt",)]
    else:
        groups = [("drm-resident-local0",), ("drm-resident-vram0",)]
    for group in groups:
        if any(k in fields for k in group):
            return sum(size_mib(fields.get(k)) for k in group)
    return 0.0


_engine_prev = {}  # slot -> {(pid, client, engine): (busy, clock)}


def engine_util(g, clients):
    """Utilization of the busiest engine (render, video, ...) since the last
    call, from per-client counters: i915 reports busy ns (clock = wall time),
    xe reports busy and total GPU cycles. Covers clients we can read."""
    now_ns = time.monotonic_ns()
    prev = _engine_prev.get(g["slot"])
    cur, busy = {}, {}
    for _slot, pid, client, f in clients:
        for key, val in f.items():
            if key.startswith("drm-engine-") and not key.startswith("drm-engine-capacity-"):
                engine, used, clock = key[11:], num(val.split()[0]), now_ns
            elif key.startswith("drm-cycles-"):
                engine, used = key[11:], num(val.split()[0])
                clock = num((f.get("drm-total-cycles-" + engine) or "").split()[0]
                            if f.get("drm-total-cycles-" + engine) else None)
            else:
                continue
            if used is None or clock is None:
                continue
            cur[(pid, client, engine)] = (used, clock)
            before = (prev or {}).get((pid, client, engine))
            if before and used >= before[0] and clock > before[1]:
                cap = num(f.get("drm-engine-capacity-" + engine)) or 1
                busy[engine] = busy.get(engine, 0.0) + (used - before[0]) / (clock - before[1]) / cap
    _engine_prev[g["slot"]] = cur
    if prev is None:
        return None  # first sample: no interval to measure yet
    return round(min(100.0, 100.0 * max(busy.values(), default=0.0)), 1)


def drm_procs(gpus, clients):
    """Attach per-process GPU memory to AMD/Intel GPUs from the client list."""
    by_slot = {g["slot"]: g for g in gpus}
    for slot, pid, _client, fields in clients:
        g = by_slot[slot]
        mem = round(client_mem_mib(g, fields))
        for p in g["procs"]:
            if p["pid"] == pid:
                p["vramMiB"] += mem
                break
        else:
            g["procs"].append({"pid": pid, "name": comm(pid), "cmd": cmdline(pid),
                               "vramMiB": mem, "rssMiB": rss_mib(pid)})
    for g in gpus:
        if g["shared"] and g["vendor"] == "Intel":  # no VRAM: report what clients hold
            g["vramUsedMiB"] = sum(p["vramMiB"] for p in g["procs"])


# --------------------------------------------------------------- NVIDIA
NV_FIELDS = ["index", "name", "pci.bus_id", "utilization.gpu", "utilization.memory",
             "memory.used", "memory.total", "temperature.gpu", "power.draw",
             "power.limit", "fan.speed", "clocks.sm"]


def nvidia_smi(args):
    try:
        res = subprocess.run(["nvidia-smi"] + args, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


def nv_gpus():
    out = nvidia_smi(["--query-gpu=" + ",".join(NV_FIELDS), "--format=csv,noheader,nounits"])
    gpus = []
    for line in (out or "").splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) != len(NV_FIELDS):
            continue
        gpus.append({
            "vendor": "NVIDIA", "index": int(f[0]), "name": f[1].replace("NVIDIA ", ""),
            "slot": f[2].lower()[-12:] if len(f[2]) >= 12 else f[2].lower(),
            "util": num(f[3]), "memUtil": num(f[4]),
            "vramUsedMiB": num(f[5]), "vramTotalMiB": num(f[6]),
            "gttUsedMiB": None, "gttTotalMiB": None,
            "temp": num(f[7]), "temps": {"gpu": num(f[7])},
            "powerW": num(f[8]), "powerCapW": num(f[9]),
            "fanPct": num(f[10]), "fanRpm": None, "sclkMhz": num(f[11]),
            "shared": False, "tempSource": "gpu", "procs": [],
        })
    return gpus


def nv_procs(gpus):
    out = nvidia_smi(["--query-compute-apps=gpu_bus_id,pid,process_name,used_memory",
                      "--format=csv,noheader,nounits"])
    by_slot = {g["slot"]: g for g in gpus}
    for line in (out or "").splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 4:
            continue
        slot = f[0].lower()[-12:]
        if slot not in by_slot:
            continue
        pid = f[1]
        by_slot[slot]["procs"].append({
            "pid": int(pid), "name": comm(pid) if os.path.exists("/proc/" + pid)
            else os.path.basename(f[2])[:48],
            "cmd": cmdline(pid), "vramMiB": num(f[-1]), "rssMiB": rss_mib(pid)})


# ----------------------------------------------------------------- main
def main():
    host = rd("/proc/sys/kernel/hostname") or "?"
    has_nv = nvidia_smi(["-L"]) is not None
    cpu_pct()
    procs_cache = {}
    n = 0
    next_t = time.monotonic()
    while True:
        drm = amd_gpus() + intel_gpus()
        intel = [g for g in drm if g["vendor"] == "Intel"]
        gpus = drm + (nv_gpus() if has_nv else [])
        scan_procs = n % PROC_EVERY == 0
        # Intel utilization comes from client counters, so it needs every sample.
        clients = drm_clients({g["slot"] for g in drm}) if drm and (scan_procs or intel) else []
        for g in intel:
            g["util"] = engine_util(g, [c for c in clients if c[0] == g["slot"]])
        if scan_procs:
            drm_procs(drm, clients)
            if has_nv:
                nv_procs(gpus)
            procs_cache = {g["slot"]: (g["procs"], g["vramUsedMiB"]) for g in gpus}
        else:
            for g in gpus:
                g["procs"], used = procs_cache.get(g["slot"], ([], None))
                if g["shared"]:
                    g["vramUsedMiB"] = used
        for g in gpus:
            g["procs"].sort(key=lambda p: -(p.get("vramMiB") or 0))
            rss = [p["rssMiB"] for p in g["procs"] if p.get("rssMiB") is not None]
            g["ramMiB"] = sum(rss) if rss else None
        loadavg = (rd("/proc/loadavg") or "").split()
        sys.stdout.write(json.dumps({
            "t": time.time(), "host": host, "cpu": cpu_pct(), "mem": meminfo(),
            "load": num(loadavg[0]) if loadavg else None,
            "gpus": gpus,
        }, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        n += 1
        # fixed cadence: subtract the time spent sampling, never burst to catch up
        next_t = max(next_t + INTERVAL, time.monotonic())
        time.sleep(max(0.0, next_t - time.monotonic()))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
