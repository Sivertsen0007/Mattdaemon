import Cocoa
import WebKit
import Darwin

/// Boots the bundled Python terminal server as a child process, then shows a
/// single window whose WKWebView points at that local server. The server is
/// torn down on quit so nothing is left listening.
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?

    /// Where the terminal opens by default. Overridable via the config file.
    private var homeDir: String = NSHomeDirectory() + "/Documents/AI-Hub"

    /// First port we try; if taken we walk forward a few slots.
    private let basePort = 8722
    private let portAttempts = 8
    private var boundPort = 8722

    func applicationDidFinishLaunching(_ notification: Notification) {
        homeDir = loadOrCreateHome()

        guard let serverScript = Bundle.main.path(forResource: "server", ofType: "py") else {
            fatalStart("Bundled server.py is missing from the app - reinstall ML Suite Terminal.")
            return
        }

        guard let port = firstAvailablePort() else {
            fatalStart("No free port found in \(basePort)...\(basePort + portAttempts - 1).")
            return
        }
        boundPort = port

        startServer(script: serverScript, port: port)
        buildWindow()

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

    // MARK: - Config

    /// Reads `home` from ~/Library/Application Support/MLSuiteTerminal/mts-config.json,
    /// creating the file with the default when it does not yet exist.
    private func loadOrCreateHome() -> String {
        let fm = FileManager.default
        let support = fm.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/MLSuiteTerminal", isDirectory: true)
        let configURL = support.appendingPathComponent("mts-config.json")
        let defaultHome = NSHomeDirectory() + "/Documents/AI-Hub"

        if let data = try? Data(contentsOf: configURL),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let home = obj["home"] as? String, !home.isEmpty {
            return (home as NSString).expandingTildeInPath
        }

        // Absent or unreadable: write the default so the user has a file to edit.
        do {
            try fm.createDirectory(at: support, withIntermediateDirectories: true)
            let payload: [String: Any] = ["home": defaultHome]
            let data = try JSONSerialization.data(withJSONObject: payload,
                                                  options: [.prettyPrinted])
            try data.write(to: configURL)
        } catch {
            NSLog("Could not write default config: \(error)")
        }
        return defaultHome
    }

    // MARK: - Server process

    private func startServer(script: String, port: Int) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", script,
                             "--port", String(port),
                             "--home", homeDir]
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

    // MARK: - Window / web view

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1100, height: 720),
                            configuration: config)
        webView.autoresizingMask = [.width, .height]

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1100, height: 720),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false)
        window.title = "ML Suite Terminal"
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

    // MARK: - Errors

    /// Shows a blocking alert for an unrecoverable start-up problem and quits.
    private func fatalStart(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "ML Suite Terminal could not start"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.addButton(withTitle: "Quit")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
        stopServer()
        NSApp.terminate(nil)
    }
}
