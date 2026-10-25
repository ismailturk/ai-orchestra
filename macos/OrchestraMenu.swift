// AI Orchestra menu bar: a thin client of the local Command Center API (bin/orchestra.py).
// It never touches task state itself; Pause/Resume only toggle "no new role starts".
import AppKit

final class OrchestraMenu: NSObject, NSApplicationDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var summary: [String: Any] = [:]
    var online = false

    var configDir: URL {
        if let x = ProcessInfo.processInfo.environment["XDG_CONFIG_HOME"], !x.isEmpty {
            return URL(fileURLWithPath: x).appendingPathComponent("ai-team")
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/ai-team")
    }
    var port: Int {
        guard let d = try? Data(contentsOf: configDir.appendingPathComponent("server.json")),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let p = j["port"] as? Int else { return 8765 }
        return p
    }
    var token: String? {
        (try? String(contentsOf: configDir.appendingPathComponent("token"), encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
    var base: String { "http://127.0.0.1:\(port)" }

    func applicationDidFinishLaunching(_ n: Notification) {
        if let img = NSImage(systemSymbolName: "circle.hexagongrid", accessibilityDescription: "AI Orchestra") {
            img.isTemplate = true
            item.button?.image = img
        } else {
            item.button?.title = "AIO"
        }
        rebuild()
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.poll() }
        poll()
    }

    func call(_ path: String, post: Bool = false, done: @escaping ([String: Any]?) -> Void) {
        guard let url = URL(string: base + path), let tok = token else { done(nil); return }
        var r = URLRequest(url: url, timeoutInterval: 4)
        r.setValue(tok, forHTTPHeaderField: "X-Orchestra-Token")
        if post {
            r.httpMethod = "POST"
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = Data("{}".utf8)
        }
        URLSession.shared.dataTask(with: r) { data, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            let j = ok ? data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } : nil
            DispatchQueue.main.async { done(j) }
        }.resume()
    }

    func poll() {
        call("/api/summary") { j in
            self.online = j != nil
            self.summary = j ?? [:]
            self.rebuild()
        }
    }

    func info(_ m: NSMenu, _ s: String) {
        let i = NSMenuItem(title: s, action: nil, keyEquivalent: "")
        i.isEnabled = false
        m.addItem(i)
    }

    func action(_ m: NSMenu, _ title: String, _ sel: Selector, enabled: Bool = true) {
        let i = NSMenuItem(title: title, action: enabled ? sel : nil, keyEquivalent: "")
        i.target = self
        m.addItem(i)
    }

    func rebuild() {
        let m = NSMenu()
        m.autoenablesItems = false
        let paused = summary["paused"] as? Bool ?? false
        let approvals = summary["approvals"] as? Int ?? 0
        if !online {
            info(m, "Command Center kapalı")
            action(m, "Start Command Center", #selector(startServer))
        } else {
            let runners = summary["online"] as? [String] ?? []
            info(m, runners.isEmpty ? "Runner: offline" : "Runner: online (\(runners.joined(separator: ", ")))")
            info(m, "Aktif proje: \(summary["active_project"] as? String ?? "-")")
            info(m, "Aktif görev: \(summary["active_tasks"] as? Int ?? 0)")
            let agents = summary["agents"] as? [String] ?? []
            if agents.isEmpty { info(m, "Çalışan ajan yok") }
            for a in agents.prefix(5) { info(m, "Çalışıyor · \(a)") }
            info(m, "Bekleyen onay: \(approvals)")
            if let e = summary["last_error"] as? String { info(m, "Son hata: " + String(e.prefix(90))) }
            if paused { info(m, "⏸ Yeni görev başlatma duraklatıldı") }
        }
        m.addItem(.separator())
        action(m, "Open Dashboard", #selector(openDashboard), enabled: online)
        action(m, "View Logs", #selector(viewLogs), enabled: online)
        action(m, "Pause New Tasks", #selector(pause), enabled: online && !paused)
        action(m, "Resume", #selector(resume), enabled: online && paused)
        m.addItem(.separator())
        action(m, "Quit UI", #selector(quit))
        item.menu = m
        item.button?.title = !online ? " ·" : paused ? " ⏸" : approvals > 0 ? " \(approvals)" : ""
    }

    @objc func openDashboard() { NSWorkspace.shared.open(URL(string: base + "/#lead")!) }
    @objc func viewLogs() { NSWorkspace.shared.open(URL(string: base + "/#activity")!) }
    @objc func pause() { call("/api/pause", post: true) { _ in self.poll() } }
    @objc func resume() { call("/api/resume", post: true) { _ in self.poll() } }
    @objc func quit() { NSApp.terminate(nil) }  // the UI only: runners and Command Center keep running

    @objc func startServer() {
        let bin = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/team-orchestra")
        let p = Process()
        p.executableURL = bin
        let log = configDir.appendingPathComponent("orchestra.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        if let h = try? FileHandle(forWritingTo: log) { h.seekToEndOfFile(); p.standardOutput = h; p.standardError = h }
        do { try p.run() } catch {
            let a = NSAlert(); a.messageText = "team-orchestra başlatılamadı"; a.informativeText = "\(bin.path)\n\(error)"; a.runModal()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.poll() }
    }
}

let app = NSApplication.shared
let delegate = OrchestraMenu()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
