import AppKit
import UserNotifications

struct ReplyEvent: Decodable {
    let title: String
    let provider: String
}

final class ReplyAlerts: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let center = UNUserNotificationCenter.current()

    func applicationWillFinishLaunching(_ notification: Notification) {
        center.delegate = self
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        for file in urls {
            let directory = file.deletingLastPathComponent()
            let data = directory.deletingLastPathComponent()
            guard file.isFileURL, directory.lastPathComponent == "notification-events",
                  file.pathExtension == "atlasreply",
                  let seq = Int64(file.deletingPathExtension().lastPathComponent), seq > 0,
                  let bytes = try? Data(contentsOf: file),
                  let event = try? JSONDecoder().decode(ReplyEvent.self, from: bytes),
                  launchURL(data: data, seq: seq) != nil else { continue }

            let content = UNMutableNotificationContent()
            content.title = "New reply · " + event.provider
            content.body = event.title
            content.userInfo = ["workspace": data.path, "reply": String(seq)]
            let request = UNNotificationRequest(identifier: file.path, content: content, trigger: nil)
            center.requestAuthorization(options: [.alert]) { allowed, error in
                if let error = error { NSLog("Thread Atlas notification permission: %@", error.localizedDescription) }
                if allowed {
                    self.center.add(request) { error in
                        if let error = error { NSLog("Thread Atlas notification: %@", error.localizedDescription) }
                    }
                }
            }
            try? FileManager.default.removeItem(at: file)
        }
    }

    private func launchURL(data: URL, seq: Int64) -> URL? {
        let file = data.appendingPathComponent("launch.url")
        guard let raw = try? String(contentsOf: file, encoding: .utf8),
              var url = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
              url.user == nil, url.password == nil,
              url.path.range(of: "^/launch/[A-Za-z0-9_-]+$", options: .regularExpression) != nil else { return nil }
        url.query = nil
        url.fragment = "reply-\(seq)"
        return url.url
    }

    private let health = URLSession(configuration: .ephemeral)

    private func openReply(data: URL, seq: Int64, attempt: Int = 0) {
        let canonicalData = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Thread Atlas", isDirectory: true)
        let parent = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let canRestart = parent.pathExtension == "app" && data.standardizedFileURL.path == canonicalData.standardizedFileURL.path

        func unavailable() {
            if canRestart && attempt < 15 {
                if attempt == 0 { NSWorkspace.shared.open(parent) }
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.openReply(data: data, seq: seq, attempt: attempt + 1) }
            } else {
                NSApp.activate(ignoringOtherApps: true)
                let alert = NSAlert()
                alert.messageText = "Open Thread Atlas to view this reply"
                alert.informativeText = "Start the local workspace and open its Inbox to see the reply."
                alert.runModal()
            }
        }
        guard let url = launchURL(data: data, seq: seq) else { unavailable(); return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        health.dataTask(with: request) { _, response, _ in
            DispatchQueue.main.async {
                if (response as? HTTPURLResponse)?.statusCode == 200 { NSWorkspace.shared.open(url) }
                else { unavailable() }
            }
        }.resume()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completion: @escaping (UNNotificationPresentationOptions) -> Void) {
        completion([.banner, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completion: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        if response.actionIdentifier == UNNotificationDefaultActionIdentifier,
           let path = info["workspace"] as? String, let raw = info["reply"] as? String,
           let seq = Int64(raw), seq > 0 {
            DispatchQueue.main.async { self.openReply(data: URL(fileURLWithPath: path), seq: seq) }
        }
        completion()
    }
}

let app = NSApplication.shared
let alerts = ReplyAlerts()
app.delegate = alerts
app.setActivationPolicy(.accessory)
app.run()
