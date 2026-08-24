import Cocoa

// Single-instance guard: a second copy would spawn a second Python server and
// fight over the same port range, so bail if another one is already running.
let myPid = ProcessInfo.processInfo.processIdentifier
if let bundleId = Bundle.main.bundleIdentifier,
   let other = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId)
       .first(where: { $0.processIdentifier != myPid }) {
    // Stepping aside silently used to be a dead end. If the running copy had
    // lost its window - live process, healthy server, nothing to click - a
    // relaunch landed right here and exited, so the only way back in was to
    // kill it. Ask it to show itself before we go.
    DistributedNotificationCenter.default().postNotificationName(
        AppDelegate.reopenNotification, object: nil, userInfo: nil,
        deliverImmediately: true)
    other.activate(options: [])
    NSLog("Another Mattdaemon instance is already running - asked it to show its window")
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// A normal windowed app: show in the Dock and own the menu bar.
app.setActivationPolicy(.regular)
app.run()
