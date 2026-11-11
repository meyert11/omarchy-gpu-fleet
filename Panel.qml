import QtQuick
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// GPU Fleet: GPUs on this machine and remote hosts (over SSH) in the Omarchy bar.
//
// Bar: one tiny meter pair per GPU (utilization | VRAM) for the selected host.
// A meter turns red at hotTemp; a host that stops answering dims; a red dot
// means another host is hot or offline.
// Panel: a tab per host, then a card per GPU with utilization, VRAM, the host
// RAM held by its processes, temperature, power, fan and the processes.
//
// The service replaces its whole state every sample, so delegates are driven
// by integer counts and look their data up by index: Repeaters then keep their
// items (and running animations) instead of rebuilding them every update.
Panel {
  id: root
  moduleName: "io.github.meyert11.gpu-fleet"
  ipcTarget: "io.github.meyert11.gpu-fleet"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  readonly property bool vertical: bar ? bar.vertical : false
  readonly property var svc: bar && bar.shell ? bar.shell.serviceFor("io.github.meyert11.gpu-fleet") : null
  readonly property var hosts: svc && svc.state && svc.state.hosts ? svc.state.hosts : []
  readonly property real hotTemp: Number(setting("hotTemp", 85))
  readonly property color fg: bar ? bar.barForeground : Commons.Color.foreground
  readonly property color hot: bar ? bar.urgent : Commons.Color.urgent
  readonly property color accent: Commons.Color.accent

  // The host shown on the bar = the tab last picked in the panel, persisted
  // in this widget's shell.json entry as "barHost".
  // Every monitor's bar has its own instance, so the choice lives in the
  // shared settings entry; pendingHost only covers the write round-trip.
  readonly property string savedHost: String(setting("barHost", ""))
  property string pendingHost: ""
  onSavedHostChanged: pendingHost = ""
  readonly property string selectedHost: pendingHost !== "" ? pendingHost : savedHost
  readonly property var currentHost: {
    for (var i = 0; i < hosts.length; i++)
      if (hosts[i].name === selectedHost) return hosts[i]
    return hosts.length > 0 ? hosts[0] : null
  }
  readonly property int gpuCount: currentHost ? currentHost.gpus.length : 0
  readonly property string configError: svc && svc.state && svc.state.configError ? svc.state.configError : ""
  readonly property var noGpu: ({
    vendor: "?", name: "", slot: "", index: null, util: null, memUtil: null,
    shared: false, tempSource: "gpu",
    vramUsedMiB: null, vramTotalMiB: null, gttUsedMiB: null, gttTotalMiB: null,
    temp: null, temps: [], powerW: null, powerCapW: null, fanPct: null, fanRpm: null,
    sclkMhz: null, ramMiB: null, procs: []
  })
  function gpuAt(i) {
    var g = currentHost ? currentHost.gpus[i] : null
    return g ? g : noGpu
  }

  // Tab options only change when a host's name or liveness does; rebuilding
  // them every sample would recreate the tab buttons (and drop their hover).
  property var tabOptions: []
  property string tabKey: ""
  onHostsChanged: {
    var key = hosts.map(function(h) { return h.name + (live(h) ? "" : "!") }).join("\n")
    if (key === tabKey) return
    tabKey = key
    tabOptions = hosts.map(function(h) {
      return { value: h.name, label: h.name + (live(h) ? "" : " ⚠") }
    })
  }

  // ---------------------------------------------------------------- format
  function gib(mib) {
    if (mib === null || mib === undefined) return "—"
    return (mib / 1024).toFixed(mib >= 10240 ? 0 : 1)
  }
  function pct(v) { return v === null || v === undefined ? "—" : Math.round(v) + "%" }
  function frac(a, b) { return (a === null || !b) ? 0 : Math.max(0, Math.min(1, a / b)) }
  function isHot(g) { return g.temp !== null && g.temp >= root.hotTemp }
  // An integrated GPU has no VRAM: show the shared memory its clients hold,
  // against the host's RAM.
  function memFrac(g) {
    return g.shared ? frac(g.vramUsedMiB, currentHost ? currentHost.memTotalMiB : null)
                    : frac(g.vramUsedMiB, g.vramTotalMiB)
  }
  function memText(g) {
    return g.shared ? gib(g.vramUsedMiB) + " G of system RAM"
                    : gib(g.vramUsedMiB) + " / " + gib(g.vramTotalMiB) + " G"
  }
  function live(h) { return h.status === "ok" }

  function hostSummary(h) {
    if (!live(h)) return h.name + " — " + (h.status === "connecting" ? "connecting…" : h.status + (h.error ? ": " + h.error : ""))
    var used = 0, total = 0, util = 0
    for (var i = 0; i < h.gpus.length; i++) {
      used += h.gpus[i].vramUsedMiB || 0
      total += h.gpus[i].vramTotalMiB || 0
      util += h.gpus[i].util || 0
    }
    var avg = h.gpus.length ? util / h.gpus.length : 0
    return h.name + " · " + h.gpus.length + " GPU · " + Math.round(avg) + "% · VRAM "
      + gib(used) + "/" + gib(total) + "G · RAM " + gib(h.memUsedMiB) + "/" + gib(h.memTotalMiB) + "G"
  }
  function tooltip() {
    if (hosts.length === 0) return "GPU Fleet — starting…"
    var lines = []
    if (currentHost) lines.push("▸ " + hostSummary(currentHost))
    for (var i = 0; i < hosts.length; i++)
      if (!currentHost || hosts[i].name !== currentHost.name) lines.push("   " + hostSummary(hosts[i]))
    return lines.join("\n") + "\nclick: pick host in panel"
  }

  function selectHost(name) {
    if (!name || name === selectedHost) return
    pendingHost = name
    if (!bar || !bar.shell || typeof bar.shell.updateEntryInline !== "function") return
    var merged = {}
    var cur = settings || {}
    for (var k in cur) merged[k] = cur[k]
    merged.barHost = name
    bar.shell.updateEntryInline(moduleName, merged)
  }

  // Something worth a glance on a host that isn't on the bar: hot GPU or offline.
  readonly property bool hiddenAlert: {
    for (var i = 0; i < hosts.length; i++) {
      var h = hosts[i]
      if (currentHost && h.name === currentHost.name) continue
      if (h.status === "down" || h.status === "stale") return true
      for (var j = 0; j < h.gpus.length; j++) if (isHot(h.gpus[j])) return true
    }
    return false
  }

  function stepHost(delta) {
    if (hosts.length === 0) return
    var idx = 0
    for (var i = 0; i < hosts.length; i++) if (currentHost && hosts[i].name === currentHost.name) idx = i
    idx = (idx + delta + hosts.length) % hosts.length
    selectHost(hosts[idx].name)
  }

  // ------------------------------------------------------------------- bar
  readonly property int meterW: Style.space(3)
  readonly property int meterGap: Style.space(1)
  readonly property int gpuGap: Style.space(3)
  readonly property int hostGap: Style.space(7)
  readonly property int meterH: Math.round((bar ? bar.barSize : 26) * 0.5)

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "gpu"
    labelVisible: false
    fixedWidth: vertical ? -1 : meters.implicitWidth + Style.space(14)
    tooltipText: root.tooltip()
    onPressed: function(b) {
      if (b === Qt.MiddleButton) { if (root.svc) root.svc.refresh() }
      else root.toggle()
    }

    Row {
      id: meters
      anchors.centerIn: parent
      spacing: root.hostGap
      scale: button.pressScale

      Text {
        visible: root.hosts.length === 0
        textFormat: Text.PlainText
        text: "GPU …"
        color: root.fg
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.body
      }

      Row {
          id: hostRow
          visible: root.currentHost !== null
          spacing: root.gpuGap
          opacity: root.currentHost && root.live(root.currentHost) ? 1 : 0.35

          // a host that isn't streaming still shows a placeholder slot
          Rectangle {
            visible: root.gpuCount === 0
            width: root.meterW * 2 + root.meterGap
            height: root.meterH
            radius: 1
            anchors.verticalCenter: parent.verticalCenter
            color: "transparent"
            border.width: 1
            border.color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.4)
          }

          Repeater {
            model: root.gpuCount

            Row {
              id: gpuMeter
              required property int index
              readonly property var modelData: root.gpuAt(index)
              spacing: root.meterGap
              anchors.verticalCenter: parent ? parent.verticalCenter : undefined

              // utilization
              Rectangle {
                width: root.meterW
                height: root.meterH
                radius: 1
                color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.14)
                Rectangle {
                  anchors.bottom: parent.bottom
                  width: parent.width
                  height: Math.max(1, Math.round(parent.height * root.frac(gpuMeter.modelData.util, 100)))
                  radius: 1
                  color: root.isHot(gpuMeter.modelData) ? root.hot : root.accent
                  Behavior on height { NumberAnimation { duration: Style.duration(250); easing.type: Easing.OutCubic } }
                }
              }
              // VRAM
              Rectangle {
                width: root.meterW
                height: root.meterH
                radius: 1
                color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.14)
                Rectangle {
                  anchors.bottom: parent.bottom
                  width: parent.width
                  height: Math.round(parent.height * root.memFrac(gpuMeter.modelData))
                  radius: 1
                  color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.6)
                  Behavior on height { NumberAnimation { duration: Style.duration(250); easing.type: Easing.OutCubic } }
                }
              }
            }
          }
      }

      // another host is hot or offline
      Rectangle {
        visible: root.hiddenAlert
        width: Style.space(4)
        height: width
        radius: width / 2
        anchors.verticalCenter: parent.verticalCenter
        color: root.hot
      }
    }
  }

  // ----------------------------------------------------------------- panel
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

    content: PanelKeyCatcher {
      anchors.fill: parent
      implicitHeight: column.implicitHeight
      onMoveRequested: function(dx, dy) { if (dx !== 0) root.stepHost(dx) }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(10)

        // ---- host tabs
        ButtonGroup {
          focusable: false
          foreground: root.fg
          fontSize: Style.font.bodySmall
          value: root.currentHost ? root.currentHost.name : ""
          options: root.tabOptions
          onChanged: function(v) { root.selectHost(v) }
        }

        PlainLabel {
          visible: root.configError !== ""
          width: parent.width
          text: "Config: " + root.configError
          color: root.hot
          wrapMode: Text.Wrap
          font.pixelSize: Style.font.caption
        }

        // ---- host line
        Item {
          visible: root.currentHost !== null
          width: parent.width
          implicitHeight: hostInfo.implicitHeight

          Column {
            id: hostInfo
            width: parent.width
            spacing: Style.space(4)

            Row {
              width: parent.width
              PlainLabel {
                width: parent.width - hostStatus.implicitWidth
                text: root.currentHost
                  ? "CPU " + root.pct(root.currentHost.cpu) + "  ·  load "
                    + (root.currentHost.load === null ? "—" : root.currentHost.load.toFixed(2))
                  : ""
                color: Qt.darker(root.fg, 1.15)
                font.pixelSize: Style.font.caption
                font.bold: true
              }
              PlainLabel {
                id: hostStatus
                text: !root.currentHost ? "" : root.currentHost.status === "ok"
                  ? root.currentHost.target : root.currentHost.status.toUpperCase()
                color: root.currentHost && root.live(root.currentHost) ? Qt.darker(root.fg, 1.5) : root.hot
                font.pixelSize: Style.font.caption
              }
            }
            Meter {
              label: "RAM"
              value: root.currentHost
                ? root.gib(root.currentHost.memUsedMiB) + " / " + root.gib(root.currentHost.memTotalMiB) + " G" : "—"
              fraction: root.currentHost ? root.frac(root.currentHost.memUsedMiB, root.currentHost.memTotalMiB) : 0
              fillColor: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.55)
            }
            PlainLabel {
              visible: root.currentHost !== null && !root.live(root.currentHost) && root.currentHost.error !== ""
              width: parent.width
              text: root.currentHost ? root.currentHost.error : ""
              color: root.hot
              wrapMode: Text.Wrap
              font.pixelSize: Style.font.caption
            }
          }
        }

        // ---- one card per GPU
        Repeater {
          model: root.gpuCount

          Column {
            id: card
            required property int index
            readonly property var g: root.gpuAt(index)
            width: column.width
            spacing: Style.space(4)
            opacity: root.currentHost && root.live(root.currentHost) ? 1 : 0.5

            PanelSeparator { foreground: root.fg; width: parent.width }

            Row {
              width: parent.width
              PlainLabel {
                width: parent.width - utilText.implicitWidth
                text: "GPU" + (card.g.index !== null ? card.g.index : card.index) + "  " + card.g.name
                color: root.fg
                font.pixelSize: Style.font.bodySmall
                font.bold: true
                elide: Text.ElideRight
              }
              PlainLabel {
                id: utilText
                text: root.pct(card.g.util)
                color: root.isHot(card.g) ? root.hot : root.accent
                font.pixelSize: Style.font.heading
                font.bold: true
              }
            }

            Meter {
              label: "UTIL"
              value: card.g.memUtil !== null ? "mem bus " + root.pct(card.g.memUtil) : ""
              fraction: root.frac(card.g.util, 100)
              fillColor: root.isHot(card.g) ? root.hot : root.accent
            }
            Meter {
              label: card.g.shared ? "GPU MEM" : "VRAM"
              value: root.memText(card.g)
              fraction: root.memFrac(card.g)
              fillColor: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.6)
            }

            // system RAM held by this GPU's processes (+ GTT on AMD)
            PlainLabel {
              width: parent.width
              text: "SYS RAM " + (card.g.ramMiB !== null ? root.gib(card.g.ramMiB) + " G" : "—")
                + (card.g.gttUsedMiB !== null ? "   ·   GTT " + root.gib(card.g.gttUsedMiB) + " G" : "")
              color: Qt.darker(root.fg, 1.15)
              font.pixelSize: Style.font.caption
              font.bold: true
            }

            PlainLabel {
              width: parent.width
              text: [
                card.g.temp !== null ? Math.round(card.g.temp) + "°C" + (card.g.tempSource === "package" ? " (CPU package)" : "") : "",
                card.g.powerW !== null ? Math.round(card.g.powerW) + (card.g.powerCapW ? "/" + Math.round(card.g.powerCapW) : "") + " W" : "",
                card.g.fanPct !== null ? "fan " + Math.round(card.g.fanPct) + "%" : (card.g.fanRpm ? "fan " + Math.round(card.g.fanRpm) + " rpm" : ""),
                card.g.sclkMhz ? Math.round(card.g.sclkMhz) + " MHz" : ""
              ].filter(function(s) { return s !== "" }).join("   ·   ")
              color: root.isHot(card.g) ? root.hot : Qt.darker(root.fg, 1.4)
              font.pixelSize: Style.font.caption
            }

            Repeater {
              model: Math.min(4, card.g.procs.length)
              Row {
                required property int index
                readonly property var modelData: card.g.procs[index] || ({ pid: null, name: "", cmd: "", vramMiB: null, rssMiB: null })
                width: card.width
                spacing: Style.space(8)
                PlainLabel {
                  width: parent.width - procMem.implicitWidth - parent.spacing
                  text: (modelData.cmd || modelData.name) + "  · " + modelData.pid
                  color: root.fg
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }
                PlainLabel {
                  id: procMem
                  text: root.gib(modelData.vramMiB) + (card.g.shared ? "G gpu · " : "G vram · ")
                    + root.gib(modelData.rssMiB) + "G ram"
                  color: Qt.darker(root.fg, 1.4)
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }
        }

        PlainLabel {
          visible: root.hosts.length === 0
          text: "Connecting to GPU hosts…"
          color: Qt.darker(root.fg, 1.4)
          font.pixelSize: Style.font.bodySmall
        }
      }
    }
  }

  // ------------------------------------------------------------ components
  component PlainLabel: Text {
    textFormat: Text.PlainText
    font.family: root.bar ? root.bar.fontFamily : Style.font.family
  }

  component Meter: Column {
    property string label: ""
    property string value: ""
    property real fraction: 0
    property color fillColor: root.accent
    width: parent ? parent.width : 0
    spacing: Style.space(2)

    Row {
      width: parent.width
      PlainLabel {
        width: parent.width - meterValue.implicitWidth
        text: label
        color: Qt.darker(root.fg, 1.15)
        font.pixelSize: Style.font.caption
        font.bold: true
      }
      PlainLabel {
        id: meterValue
        text: value
        color: Qt.darker(root.fg, 1.4)
        font.pixelSize: Style.font.caption
      }
    }
    Rectangle {
      width: parent.width
      height: Style.space(6)
      radius: height / 2
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
      Rectangle {
        width: Math.max(fraction > 0 ? parent.height : 0, parent.width * fraction)
        height: parent.height
        radius: parent.radius
        color: fillColor
        Behavior on width { NumberAnimation { duration: Style.duration(250); easing.type: Easing.OutCubic } }
      }
    }
  }
}
