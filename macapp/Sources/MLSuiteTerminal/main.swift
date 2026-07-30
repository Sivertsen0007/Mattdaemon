import Cocoa

// Single-instance guard: a second copy would spawn a second Python server and
// fight over the same port range, so bail if another one is already running.
let myPid = ProcessInfo.processInfo.processIdentifier
if let bundleId = Bundle.main.bundleIdentifier,
   NSRunningApplication.runningApplications(withBundleIdentifier: bundleId)
       .contains(where: { $0.processIdentifier != myPid }) {
    NSLog("Another ML Suite Terminal instance is already running - exiting")
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// A normal windowed app: show in the Dock and own the menu bar.
app.setActivationPolicy(.regular)
app.run()
