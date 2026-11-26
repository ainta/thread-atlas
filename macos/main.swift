import AppKit
import Foundation

final class MenuApp: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private var service: Process?
    private var timer: Timer?
    private var waitingToOpen = true
    private let resources = Bundle.main.resourceURL!.appendingPathComponent("workspace")
    private let data = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("Thread Atlas", isDirectory: true)

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Thread Atlas"
        let menu = NSMenu()
        for (name, action) in [("Open workspace", #selector(openWorkspace)), ("Browser extension folder", #selector(openExtension)), ("Quit Thread Atlas", #selector(quit))] {
            let entry = NSMenuItem(title: name, action: action, keyEquivalent: "")
            entry.target = self
            menu.addItem(entry)
        }
        item.menu = menu
        startService()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            if self?.waitingToOpen == true { self?.openWhenReady() }
        }
    }

    private func startService() {
        let proc = Process()
        proc.executableURL = resources.appendingPathComponent("Start.command")
        proc.arguments = ["--no-open", "--notify"]
        var environment = ProcessInfo.processInfo.environment
        environment["ATLAS_RUNTIME"] = data.appendingPathComponent("runtime", isDirectory: true).path
        proc.environment = environment
        proc.currentDirectoryURL = resources
        proc.terminationHandler = { [weak self] ended in
            DispatchQueue.main.async { self?.serviceEnded(ended) }
        }
        do {
            try proc.run()
            service = proc
            item.button?.title = "Thread Atlas"
        } catch {
            let alert = NSAlert()
            alert.messageText = "Thread Atlas could not start"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    private var quitting = false
    private func serviceEnded(_ ended: Process) {
        guard !quitting, service === ended else { return }
        service = nil
        // Status 0 means another instance already serves this data directory; its launch URL stays usable.
        if ended.terminationStatus == 0 { return }
        item.button?.title = "Thread Atlas ⚠︎"
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Thread Atlas stopped"
        alert.informativeText = "The local service exited with status \(ended.terminationStatus). Check that port 43821 is free and that uv or python3 can install its dependency, then restart it."
        alert.addButton(withTitle: "Restart")
        alert.addButton(withTitle: "Dismiss")
        if alert.runModal() == .alertFirstButtonReturn { waitingToOpen = true; startService() }
    }

    private var checking = false
    private func openWhenReady() {
        guard !checking,
              let raw = try? String(contentsOf: data.appendingPathComponent("launch.url"), encoding: .utf8),
              let url = URL(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.host == "127.0.0.1", url.scheme == "http" else { return }
        checking = true
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.checking = false
                if self.waitingToOpen, let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 {
                    self.waitingToOpen = false
                    NSWorkspace.shared.open(url)
                }
            }
        }.resume()
    }

    @objc func openWorkspace() { waitingToOpen = true; openWhenReady() }
    @objc func openExtension() { NSWorkspace.shared.open(resources.appendingPathComponent("extension")) }
    @objc func quit() {
        quitting = true
        timer?.invalidate()
        if service?.isRunning == true { service?.terminate() }
        NSApplication.shared.terminate(nil)
    }
}
let app = NSApplication.shared
let delegate = MenuApp()
app.setActivationPolicy(.accessory)
app.delegate = delegate
app.run()
