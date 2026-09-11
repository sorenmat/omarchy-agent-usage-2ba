import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root
  property var shell: null

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string usageDir: (Quickshell.env("XDG_STATE_HOME") || home + "/.local/state") + "/omarchy/agents/usage"
  readonly property string collectorPath: Qt.resolvedUrl("collectors/omarchy-agent-usage-2ba").toString().replace(/^file:\/\//, "")

  function refresh() {
    if (!collector.running)
      collector.running = true;
  }

  Component.onCompleted: mkdirProcess.running = true

  Process {
    id: mkdirProcess
    command: ["mkdir", "-p", root.usageDir]
    onExited: function(exitCode) {
      if (exitCode === 0)
        root.refresh();
      else
        console.warn("agent-usage-2ba: could not create usage directory");
    }
  }

  Timer {
    interval: 300000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Process {
    id: collector
    command: ["python3", root.collectorPath]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var record = null;
        try { record = JSON.parse(text); } catch (e) {}
        if (record && record.schemaVersion === 1 && record.id === "2ba")
          recordWriter.setText(text.trim() + "\n");
        else
          console.warn("agent-usage-2ba: invalid usage record");
      }
    }
    onExited: function(exitCode) {
      if (exitCode !== 0)
        console.warn("agent-usage-2ba: collector exited " + exitCode);
    }
  }

  FileView {
    id: recordWriter
    path: root.usageDir + "/2ba.json"
    watchChanges: false
    atomicWrites: true
    printErrors: true
  }
}
