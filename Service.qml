import QtQuick
import Quickshell
import Quickshell.Io

// GPU Fleet service: owns the single fleetmon.py process (one SSH session per
// GPU host) and publishes the parsed state to every bar surface.
Item {
  id: root

  // { t, interval, configError, hosts: [{ name, target, status, error, lastSeen,
  //   cpu, load, memUsedMiB, memTotalMiB, gpus }] }
  property var state: ({ hosts: [], configError: "" })
  property string lastText: ""
  property int failures: 0

  readonly property string pluginDir: {
    var path = String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")
    return path.charAt(path.length - 1) === "/" ? path : path + "/"
  }
  readonly property string runtimeDir: {
    var v = Quickshell.env("XDG_RUNTIME_DIR")
    v = v ? String(v).replace(/\/+$/, "") : ""
    return (v.charAt(0) === "/" && !/[\x00-\x1f]/.test(v)) ? v : ""
  }
  readonly property string statePath: runtimeDir === "" ? "" : runtimeDir + "/gpu-fleet/state.json"

  Process {
    id: monitor
    command: root.statePath === "" ? [] : [
      "/usr/bin/python3", root.pluginDir + "fleetmon.py", "--state-file", root.statePath
    ]
    running: root.statePath !== ""
    onExited: {
      root.failures = Math.min(root.failures + 1, 5)
      restart.interval = Math.min(3000 * Math.pow(2, root.failures - 1), 60000)
      restart.restart()
    }
  }

  Timer {
    id: restart
    interval: 3000
    onTriggered: if (root.statePath !== "") monitor.running = true
  }
  Timer {
    interval: 10000
    running: monitor.running
    onTriggered: root.failures = 0
  }

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: root.statePath !== ""
    printErrors: false
    onFileChanged: reload()
    onTextChanged: root.parse(stateFile.text())
  }

  // Fallback only: inotify on an atomically replaced file can miss an event.
  Timer {
    interval: 5000
    repeat: true
    running: root.statePath !== ""
    onTriggered: stateFile.reload()
  }

  function str(v, max) {
    if (typeof v !== "string") return ""
    return v.replace(/[\x00-\x1f]/g, " ").slice(0, max)
  }
  function n(v) {
    return (typeof v === "number" && isFinite(v)) ? v : null
  }

  function cleanGpu(g) {
    if (!g || typeof g !== "object") return null
    var procs = []
    if (Array.isArray(g.procs)) {
      for (var i = 0; i < g.procs.length && procs.length < 8; i++) {
        var p = g.procs[i]
        if (!p || typeof p !== "object") continue
        procs.push({ pid: root.n(p.pid), name: root.str(p.name, 48), cmd: root.str(p.cmd, 96),
                     vramMiB: root.n(p.vramMiB), rssMiB: root.n(p.rssMiB) })
      }
    }
    var temps = []
    if (g.temps && typeof g.temps === "object") {
      for (var k in g.temps) {
        if (temps.length >= 8) break
        if (root.n(g.temps[k]) !== null) temps.push({ label: root.str(k, 16), value: g.temps[k] })
      }
    }
    return {
      vendor: ["AMD", "NVIDIA", "Intel"].indexOf(g.vendor) >= 0 ? g.vendor : "?",
      shared: g.shared === true, tempSource: g.tempSource === "package" ? "package" : "gpu",
      name: root.str(g.name, 64), slot: root.str(g.slot, 16), index: root.n(g.index),
      util: root.n(g.util), memUtil: root.n(g.memUtil),
      vramUsedMiB: root.n(g.vramUsedMiB), vramTotalMiB: root.n(g.vramTotalMiB),
      gttUsedMiB: root.n(g.gttUsedMiB), gttTotalMiB: root.n(g.gttTotalMiB),
      temp: root.n(g.temp), temps: temps, powerW: root.n(g.powerW), powerCapW: root.n(g.powerCapW),
      fanPct: root.n(g.fanPct), fanRpm: root.n(g.fanRpm), sclkMhz: root.n(g.sclkMhz),
      ramMiB: root.n(g.ramMiB), procs: procs
    }
  }

  function parse(text) {
    if (typeof text !== "string" || text === root.lastText || text.length > 1048576) return
    root.lastText = text
    var v
    try { v = JSON.parse(text) } catch (e) { return }
    if (!v || v.schema !== 1 || !Array.isArray(v.hosts)) return
    var hosts = []
    for (var i = 0; i < v.hosts.length && hosts.length < 16; i++) {
      var h = v.hosts[i]
      if (!h || typeof h !== "object") continue
      var s = h.sample && typeof h.sample === "object" ? h.sample : null
      var gpus = []
      if (s && Array.isArray(s.gpus)) {
        for (var j = 0; j < s.gpus.length && gpus.length < 16; j++) {
          var g = root.cleanGpu(s.gpus[j])
          if (g) gpus.push(g)
        }
      }
      hosts.push({
        name: root.str(h.name, 32), target: root.str(h.target, 96),
        status: ["ok", "stale", "down", "connecting"].indexOf(h.status) >= 0 ? h.status : "down",
        error: root.str(h.error, 200), lastSeen: root.n(h.lastSeen),
        cpu: s ? root.n(s.cpu) : null, load: s ? root.n(s.load) : null,
        memUsedMiB: s && s.mem ? root.n(s.mem.usedMiB) : null,
        memTotalMiB: s && s.mem ? root.n(s.mem.totalMiB) : null,
        gpus: gpus
      })
    }
    root.state = { t: root.n(v.t), interval: root.n(v.interval),
                   configError: root.str(v.configError, 300), hosts: hosts }
  }

  function refresh() { if (root.statePath !== "") stateFile.reload() }
}
