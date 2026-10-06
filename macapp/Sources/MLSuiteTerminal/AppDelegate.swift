import Cocoa
import WebKit
import Darwin

/// Boots the bundled Python terminal server as a child process, then shows a
/// single window whose WKWebView points at that local server. The server is
/// torn down on quit so nothing is left listening.
final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKScriptMessageHandler,
                         NSWindowDelegate {
    /// Posted by a second copy of the app that found this one already running.
    /// Our cue to put a window back on screen - see `showWindow()`.
    static let reopenNotification =
        Notification.Name("com.mathiassivertsen.mlsuiteterminal.reopen")

    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?

    /// First port we try; if taken we walk forward a few slots.
    private let basePort = 8722
    private let portAttempts = 8
    private var boundPort = 8722

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let serverScript = Bundle.main.path(forResource: "server", ofType: "py") else {
            fatalStart("Bundled server.py is missing from the app - reinstall Mattdaemon.")
            return
        }

        guard let port = firstAvailablePort() else {
            fatalStart("No free port found in \(basePort)...\(basePort + portAttempts - 1).")
            return
        }
        boundPort = port

        buildMenu()
        startServer(script: serverScript, port: port)
        buildWindow()

        // A relaunched copy exits immediately, but posts this on its way out so
        // we can put a window back if ours has gone missing.
        DistributedNotificationCenter.default().addObserver(
            self, selector: #selector(handleReopenRequest),
            name: AppDelegate.reopenNotification, object: nil)

        // Sleeping drops the connections the page was streaming on, and nothing
        // in the web view is told: the sockets are simply never delivered from
        // again. This is the cue to re-establish them, and unlike focus or
        // visibility it arrives even for a window that stayed frontmost.
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(handleWake),
            name: NSWorkspace.didWakeNotification, object: nil)

        // Give the server a moment to bind before loading, then poll until the
        // port answers (or we give up and load anyway so the user sees an error).
        waitForServer(port: port, deadline: Date().addingTimeInterval(8)) { [weak self] in
            self?.loadTerminal(port: port)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    /// Clicking the Dock icon of a running app is the other way back in when
    /// the window is gone - AppKit calls this instead of launching a new copy.
    func applicationShouldHandleReopen(_ sender: NSApplication,
                                       hasVisibleWindows flag: Bool) -> Bool {
        if !flag { showWindow() }
        return true
    }

    @objc private func handleReopenRequest() {
        DispatchQueue.main.async { [weak self] in self?.showWindow() }
    }

    /// A WKWebView with no UI delegate silently drops `window.open`, which is
    /// how the page opens anything a session produces. There is no second web
    /// view to give it - what a session makes belongs in a real browser - so
    /// hand the URL to the default one and decline the new view. The bridge's
    /// openExternal covers the same ground for anything without a click behind
    /// it; this covers plain window.open, including from a popped-out window.
    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    /// `<input type="file">` - the Attach button.
    ///
    /// WebKit does not open a file picker on its own: without this delegate
    /// method the click is swallowed and nothing whatsoever happens, which is
    /// exactly how Attach behaved inside the app while working fine in a
    /// browser. Paste and drag-and-drop were unaffected, which is what made it
    /// look intermittent rather than simply missing.
    func webView(_ webView: WKWebView,
                 runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.prompt = "Attach"
        panel.message = "Choose a file to send into this session"

        let finish: (NSApplication.ModalResponse) -> Void = { response in
            // The completion handler must be called exactly once, cancel
            // included - WebKit keeps the input element disabled until it is,
            // so a dropped cancel makes Attach dead for the rest of the session.
            completionHandler(response == .OK ? panel.urls : nil)
        }
        if let window = window {
            panel.beginSheetModal(for: window, completionHandler: finish)
        } else {
            panel.begin(completionHandler: finish)
        }
    }

    /// Tells the page to rebuild its output streams after a wake. Delayed a
    /// second so the network stack is back before it tries; the page backs off
    /// and retries on its own regardless, and guards the call so a web view
    /// that has not finished loading is a no-op rather than a JS error.
    @objc private func handleWake() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.webView?.evaluateJavaScript(
                "window.__mtsWake && window.__mtsWake()", completionHandler: nil)
        }
    }

    // MARK: - Bridge from the page
    //
    // The few things a web page cannot do for itself, it asks us for, over
    // window.webkit.messageHandlers.mts.postMessage({cmd: ...}):
    //
    //   pickFolder - the setup screen and the settings panel both need a folder
    //     from the user. NSOpenPanel -> the chosen path goes back to
    //     window.__mtsFolderPicked. The page falls back to its own folder list
    //     when this bridge is absent (i.e. in a plain browser).
    //   popOut     - a session gets its own real window.
    //   copy       - text onto the system pasteboard.
    //   openExternal - a URL into the default browser. The page cannot do this
    //     itself: WebKit only honours window.open while a user gesture is live,
    //     so a plan that finished on its own would be dropped in silence.
    //
    // Everything here is best-effort and one-way: an unknown command is logged
    // and ignored, so a newer page against an older app degrades rather than
    // breaks.

    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "mts",
              let body = message.body as? [String: Any],
              let cmd = body["cmd"] as? String else { return }
        switch cmd {
        case "pickFolder":
            presentFolderPicker(startingAt: body["start"] as? String ?? "")
        case "popOut":
            guard let sid = body["sid"] as? String else { return }
            popOut(sid: sid,
                   path: body["url"] as? String ?? "/?solo=\(sid)",
                   title: body["title"] as? String ?? sid)
        case "openExternal":
            guard let raw = body["url"] as? String, let url = URL(string: raw),
                  url.scheme == "http" || url.scheme == "https" else { return }
            NSWorkspace.shared.open(url)
        case "copy":
            // The terminal's selection is xterm's own, not a DOM selection, so
            // WebKit's Copy has nothing to put on the pasteboard. The page
            // hands us the text instead and we write it ourselves - no user
            // gesture to lose.
            //
            // And we ANSWER. postMessage is one-way, so the page used to say
            // "Copied 412 characters" the instant it handed the text over -
            // true only if this write worked, and the page cannot see the
            // pasteboard to find out. A write that quietly did not land was
            // therefore indistinguishable from one that did: the toast said
            // yes and ⌘V produced the last thing you copied somewhere else.
            // Now the page is told, and falls back to its own clipboard routes
            // when the answer is no.
            let ok = writeToPasteboard(body["text"] as? String)
            message.webView?.evaluateJavaScript(
                "window.__mtsCopied && window.__mtsCopied(\(ok))",
                completionHandler: nil)
        default:
            NSLog("Ignoring unknown bridge command: \(cmd)")
        }
    }

    /// Put text on the system pasteboard and report whether it is actually
    /// there. `setString` returning true is not proof - the only proof is
    /// reading the string back off the pasteboard we just wrote.
    private func writeToPasteboard(_ text: String?) -> Bool {
        guard let text = text, !text.isEmpty else { return false }
        let pb = NSPasteboard.general
        pb.clearContents()
        let wrote = pb.setString(text, forType: .string)
        let landed = wrote && pb.string(forType: .string) == text
        if !landed {
            NSLog("Mattdaemon: pasteboard write failed (setString=\(wrote), \(text.count) chars)")
        }
        return landed
    }

    // MARK: - Pop-out windows
    //
    // A session dragged out of the grid gets a real window, which is the point:
    // it can go on a second monitor and stay there. It is the same page against
    // the same local server with ?solo=<sid>, so the terminal in it is the
    // terminal - no second implementation to keep in step.
    //
    // The shell is a tmux session and outlives any window onto it, so closing
    // one is not closing the session; the grid simply takes it back.

    private var popWindows: [String: NSWindow] = [:]

    private func popOut(sid: String, path: String, title: String) {
        // Already out: raise it rather than opening a second window onto the
        // same shell, which would be two views fighting over its size.
        if let existing = popWindows[sid] {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        guard let url = URL(string: "http://127.0.0.1:\(boundPort)\(path)") else { return }

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        let bridge = WKUserContentController()
        bridge.add(self, name: "mts")
        config.userContentController = bridge

        let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 900, height: 600),
                             configuration: config)
        view.autoresizingMask = [.width, .height]
        view.uiDelegate = self
        if #available(macOS 13.3, *) { view.isInspectable = true }
        view.load(URLRequest(url: url))

        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 600),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false)
        win.title = "\(title) - Mattdaemon"
        win.isReleasedWhenClosed = false
        win.contentView = view
        win.delegate = self
        // Each session remembers its own window's size and place, so a pane you
        // always put on the second screen lands there again.
        win.setFrameAutosaveName("MLSuiteTerminalPop-\(sid)")
        if win.frame.origin == .zero { win.center() }
        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        popWindows[sid] = win
    }

    /// A pop-out window closing hands its session back to the grid.
    func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow,
              let sid = popWindows.first(where: { $0.value === closing })?.key
        else { return }
        popWindows.removeValue(forKey: sid)
        let escaped = sid.replacingOccurrences(of: "\\", with: "\\\\")
                         .replacingOccurrences(of: "'", with: "\\'")
        webView?.evaluateJavaScript(
            "window.__mtsPopBack && window.__mtsPopBack('\(escaped)')",
            completionHandler: nil)
    }

    private func presentFolderPicker(startingAt start: String) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.prompt = "Use this folder"
        panel.message = "Choose the folder your terminal sessions should open in"

        let expanded = (start as NSString).expandingTildeInPath
        if !expanded.isEmpty, FileManager.default.fileExists(atPath: expanded) {
            panel.directoryURL = URL(fileURLWithPath: expanded)
        }

        let handler: (NSApplication.ModalResponse) -> Void = { [weak self] response in
            self?.deliverPickedFolder(response == .OK ? (panel.url?.path ?? "") : "")
        }
        // Cancelling must still answer the page, or its promise never resolves
        // and the Choose button is dead until the window is reloaded.
        if let window = window {
            panel.beginSheetModal(for: window, completionHandler: handler)
        } else {
            panel.begin(completionHandler: handler)
        }
    }

    private func deliverPickedFolder(_ path: String) {
        // JSON-encoded rather than interpolated: a folder name may legally
        // contain a quote or a backslash, and that must not become script.
        let encoded = (try? JSONSerialization.data(withJSONObject: [path]))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        webView?.evaluateJavaScript(
            "window.__mtsFolderPicked && window.__mtsFolderPicked(\(encoded)[0])",
            completionHandler: nil)
    }

    // MARK: - Server process

    private func startServer(script: String, port: Int) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        // No --home: the working directory is per-user config that the setup
        // screen writes and the server reads. The app bundle deliberately knows
        // nothing about anyone's folders, so a copy of it starts blank.
        process.arguments = ["python3", script, "--port", String(port)]
        // A Finder-launched .app inherits launchd's minimal PATH
        // (/usr/bin:/bin:/usr/sbin:/sbin), which misses tmux in a Homebrew
        // prefix or a userland (~/.local) build. Prepend those so the server -
        // and the tmux client it spawns, which inherits this environment - can
        // find tmux no matter how the app was launched.
        var env = ProcessInfo.processInfo.environment
        let home = env["HOME"] ?? NSHomeDirectory()
        let extraPaths = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
        let basePath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = (extraPaths + [basePath]).joined(separator: ":")
        process.environment = env
        // The server resolves terminal_manager.py and static/ relative to its
        // own directory, so run it from the bundle's Resources folder.
        process.currentDirectoryURL = URL(fileURLWithPath: script).deletingLastPathComponent()

        process.terminationHandler = { proc in
            NSLog("Terminal server exited with status \(proc.terminationStatus)")
        }

        do {
            try process.run()
            server = process
            NSLog("Started terminal server (pid \(process.processIdentifier)) on port \(port)")
        } catch {
            fatalStart("Could not launch the terminal server: \(error.localizedDescription)")
        }
    }

    private func stopServer() {
        guard let server = server, server.isRunning else { return }
        server.terminate()
        // Give it a beat to exit on SIGTERM; force-kill if it lingers.
        let deadline = Date().addingTimeInterval(3)
        while server.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if server.isRunning {
            kill(server.processIdentifier, SIGKILL)
        }
        self.server = nil
    }

    // MARK: - Ports

    /// Returns the first port in [basePort, basePort+portAttempts) that we can
    /// bind, or nil if they are all taken.
    private func firstAvailablePort() -> Int? {
        for offset in 0..<portAttempts {
            let port = basePort + offset
            if isPortAvailable(port) { return port }
        }
        return nil
    }

    /// True if we can bind a listening socket to 127.0.0.1:port right now.
    private func isPortAvailable(_ port: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }

        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")

        let bound = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                // Darwin. qualifier: NSObject has an instance method bind(_:to:withKeyPath:options:)
                // (Cocoa Bindings) that otherwise shadows the global socket bind().
                Darwin.bind(fd, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return bound == 0
    }

    /// True if something is now accepting TCP connections on 127.0.0.1:port.
    private func isPortAccepting(_ port: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }

        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")

        let result = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                connect(fd, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result == 0
    }

    /// Polls the port off the main thread, then fires `ready` on the main queue
    /// once it answers or the deadline passes.
    private func waitForServer(port: Int, deadline: Date, ready: @escaping () -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            while Date() < deadline {
                if self.isPortAccepting(port) { break }
                usleep(150_000)
            }
            DispatchQueue.main.async(execute: ready)
        }
    }

    // MARK: - Menu

    /// A menu bar is what maps Cmd-C/V/X/A to the cut:/copy:/paste:/selectAll:
    /// actions in the responder chain. Without it those shortcuts do nothing in
    /// the WKWebView - which is why pasting text (and pasting a screenshot into
    /// the prompt) did not work at all.
    private func buildMenu() {
        let mainMenu = NSMenu()

        // App menu
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "About Mattdaemon",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Mattdaemon",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Mattdaemon",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        // Edit menu - the important one (clipboard shortcuts for the web view).
        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editItem.submenu = editMenu
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        // Ours, not NSText's, and that is the whole point. AppKit dispatches a
        // menu key equivalent BEFORE the key event reaches the web view, so
        // Edit > Copy used to hand ⌘C straight to WebKit - which copies its own
        // DOM selection. A terminal's highlight is xterm's, not the DOM's, so
        // WebKit found nothing, and ⌘C in a terminal did nothing at all while
        // looking exactly like a copy. We ask the page for the terminal's
        // selection first and fall back to the ordinary copy when there is none.
        editMenu.addItem(withTitle: "Copy", action: #selector(mtsCopy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All",
                         action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        // View menu. Reload reloads the page, not the sessions: the shells live in
        // tmux and the server is a separate process, so this costs nothing and is
        // the only way to pick up a changed index.html without quitting the app.
        let viewItem = NSMenuItem()
        mainMenu.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        viewItem.submenu = viewMenu
        viewMenu.addItem(withTitle: "Reload",
                         action: #selector(reloadPage(_:)), keyEquivalent: "r")

        NSApp.mainMenu = mainMenu
    }

    /// Edit > Copy (⌘C). Asks the front web view for its terminal selection; if
    /// there is one the page copies it and we are done. Otherwise the event
    /// carries on to the responder chain, so ⌘C still works in a real text
    /// field - the folder box in settings, say.
    ///
    /// The page answers asynchronously, so the fallback happens a beat later.
    /// That is safe: nothing has consumed the event, and a DOM selection is
    /// still a DOM selection a millisecond after you asked about it.
    @objc private func mtsCopy(_ sender: Any?) {
        let view = (NSApp.keyWindow?.contentView as? WKWebView) ?? webView
        guard let wv = view else {
            NSApp.sendAction(#selector(NSText.copy(_:)), to: nil, from: sender)
            return
        }
        wv.evaluateJavaScript("!!(window.__mtsCopySelection && window.__mtsCopySelection())") { result, _ in
            if (result as? Bool) == true { return }
            NSApp.sendAction(#selector(NSText.copy(_:)), to: nil, from: sender)
        }
    }

    /// Reload the web view from the local server, bypassing any cached copy of
    /// the page - the point is to see the file that is on disk right now.
    @objc private func reloadPage(_ sender: Any?) {
        webView?.reloadFromOrigin()
    }

    // MARK: - Window / web view

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        // The page's one line back to native code: the folder picker.
        let bridge = WKUserContentController()
        bridge.add(self, name: "mts")
        config.userContentController = bridge
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1100, height: 720),
                            configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.uiDelegate = self
        // Attachable from Safari: Develop > this Mac > Mattdaemon. Without it a
        // bug inside the web view can only be guessed at from the outside,
        // which is no way to chase one that reproduces nowhere else.
        if #available(macOS 13.3, *) { webView.isInspectable = true }

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1100, height: 720),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false)
        window.title = "Mattdaemon"
        // NSWindow defaults to releasing itself on close, which predates ARC:
        // our strong `window` reference would be left dangling, and re-showing
        // it is then undefined. Own the lifetime here instead.
        window.isReleasedWhenClosed = false
        window.contentView = webView
        window.center()
        // Remember size/position across launches.
        window.setFrameAutosaveName("MLSuiteTerminalMainWindow")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func loadTerminal(port: Int) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/") else { return }
        webView.load(URLRequest(url: url))
    }

    /// Puts a window back on screen, rebuilding it if there is none left.
    ///
    /// The app has been seen alive with a healthy server, live tmux sessions
    /// and zero windows - nothing to click, and a relaunch hit the
    /// single-instance guard. Recreating the window is the way out of that.
    /// The sessions are tmux-backed, so a fresh web view just re-attaches and
    /// tmux redraws; nothing running is disturbed.
    private func showWindow() {
        if let existing = window {
            if existing.isMiniaturized { existing.deminiaturize(nil) }
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        buildWindow()
        loadTerminal(port: boundPort)
    }

    // MARK: - Errors

    /// Shows a blocking alert for an unrecoverable start-up problem and quits.
    private func fatalStart(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Mattdaemon could not start"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.addButton(withTitle: "Quit")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
        stopServer()
        NSApp.terminate(nil)
    }
}
