// Role Radar menu bar app: on/off switches for the Mac and Lambda runners,
// and for the Discord and email alerts.
//
// Runners. Both on: the Mac checks while this app is open; Lambda covers when
// it isn't. One on: only that runner checks. Both off: nothing checks.
// The Mac's checker (`role-radar start`, run by launchd) lives with this app:
// the app starts it, restarts it within a minute if it stops, and stops it on
// quit. Quitting and reopening the app restarts it on the current code.
// Alerts. Each goes out only to the channels switched on. Both off: sites are
// still checked and new matches saved, then sent once one is back on.
// Every read and write goes through the role-radar CLI (`switch --json`), so
// the rules live in one place. Build with scripts/build_menubar.sh.

import AppKit
import ServiceManagement
import SwiftUI

struct RunnerState: Decodable {
    struct Switches: Decodable {
        var laptop: Bool
        var lambda: Bool
        var discord: Bool
        var email: Bool

        subscript(name: String) -> Bool {
            get {
                switch name {
                case "laptop": return laptop
                case "lambda": return lambda
                case "discord": return discord
                default: return email
                }
            }
            set {
                switch name {
                case "laptop": laptop = newValue
                case "lambda": lambda = newValue
                case "discord": discord = newValue
                default: email = newValue
                }
            }
        }
    }

    struct Run: Decodable {
        let finished_at: String?
        let checked: Int?
    }

    var switches: Switches
    let checking: String?
    let lease_holder: String?
    let laptop_app_pid: Int?
    let last_runs: [String: Run]
    let login_item: Bool?

    /// The latest pass by a runner whose name starts with `prefix` ("laptop" or "lambda").
    func lastPass(_ prefix: String) -> Date? {
        let iso = ISO8601DateFormatter()
        return last_runs.filter { $0.key.hasPrefix(prefix) }
            .compactMap { $0.value.finished_at.flatMap(iso.date(from:)) }
            .max()
    }
}

@MainActor
final class Model: ObservableObject {
    @Published var state: RunnerState?
    @Published var busy: Set<String> = []
    @Published var error: String?

    nonisolated static let python: String = {
        let info = Bundle.main.infoDictionary ?? [:]
        return info["RRPython"] as? String ?? ProcessInfo.processInfo.environment["RR_PYTHON"] ?? "python3"
    }()
    nonisolated static let projectDir: String = {
        let info = Bundle.main.infoDictionary ?? [:]
        return info["RRProjectDir"] as? String ?? ProcessInfo.processInfo.environment["RR_PROJECT_DIR"]
            ?? FileManager.default.currentDirectoryPath
    }()
    private let python = Model.python
    private let projectDir = Model.projectDir

    init() {
        Task { [weak self] in
            while let self {
                await self.refresh()
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }

    var menuSymbol: String {
        switch state?.checking {
        case "laptop": return "laptopcomputer"
        case "lambda": return "cloud"
        case nil where state != nil: return "antenna.radiowaves.left.and.right.slash"
        default: return "dot.radiowaves.left.and.right"
        }
    }

    var menuLabel: String {
        switch state?.checking {
        case "laptop": return "Role Radar: the Mac is checking sites"
        case "lambda": return "Role Radar: Lambda is checking sites"
        case nil where state != nil: return "Role Radar: nothing is checking sites"
        default: return "Role Radar"
        }
    }

    /// Re-read the state, starting the Mac's checker first if it's switched on and not running.
    func refresh() async {
        await run(["switch", "--json", "--start"])
    }

    /// Ask the Mac's checker to finish the companies in flight and quit. Doesn't wait: the
    /// `role-radar stop` it launches outlives this app, and Lambda takes over once it's done.
    nonisolated static func stopChecker() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "role_radar", "stop"]
        process.currentDirectoryURL = URL(fileURLWithPath: projectDir)
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try? process.run()
    }

    /// Flip a switch: "laptop" or "lambda" (runners), "discord" or "email" (alerts).
    func set(_ name: String, on: Bool) async {
        busy.insert(name)
        state?.switches[name] = on
        // --start: switching the Mac on also starts its checker if it isn't running.
        await run(["switch", name, on ? "on" : "off", "--json", "--start"])
        busy.remove(name)
    }

    private func run(_ args: [String]) async {
        let config = projectDir + "/config/companies.yaml"
        let result = await Self.cli(python: python, dir: projectDir, args: ["-m", "role_radar"] + args + ["--config", config])
        switch result {
        case .success(let data):
            do {
                state = try JSONDecoder().decode(RunnerState.self, from: data)
                error = nil
            } catch {
                self.error = "Unexpected reply from role-radar: \(error.localizedDescription)"
            }
        case .failure(let message):
            error = message
            await refreshQuietly()
        }
    }

    /// After a failed switch, re-read the real state so the toggles don't lie.
    private func refreshQuietly() async {
        let config = projectDir + "/config/companies.yaml"
        if case .success(let data) = await Self.cli(
            python: python, dir: projectDir, args: ["-m", "role_radar", "switch", "--json", "--config", config]),
            let fresh = try? JSONDecoder().decode(RunnerState.self, from: data) {
            state = fresh
        }
    }

    enum CLIResult { case success(Data), failure(String) }

    nonisolated static func cli(python: String, dir: String, args: [String]) async -> CLIResult {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: python)
                process.arguments = args
                process.currentDirectoryURL = URL(fileURLWithPath: dir)
                var env = ProcessInfo.processInfo.environment
                env["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
                process.environment = env
                let out = Pipe(), err = Pipe()
                process.standardOutput = out
                process.standardError = err
                do {
                    try process.run()
                } catch {
                    continuation.resume(returning: .failure("Couldn't run \(python): \(error.localizedDescription)"))
                    return
                }
                let data = out.fileHandleForReading.readDataToEndOfFile()
                let errText = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                process.waitUntilExit()
                if process.terminationStatus == 0 {
                    continuation.resume(returning: .success(data))
                } else {
                    let last = errText.split(separator: "\n").last.map(String.init) ?? "exit \(process.terminationStatus)"
                    continuation.resume(returning: .failure(last))
                }
            }
        }
    }
}

struct SwitchRow: View {
    let title: String
    let symbol: String
    let detail: String
    let isOn: Bool
    let active: Bool
    let busy: Bool
    var action: (title: String, run: () -> Void)? = nil
    let toggle: (Bool) -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(isOn ? Color.white : Color.secondary)
                .frame(width: 28, height: 28)
                .background(Circle().fill(isOn ? (active ? Color.green : Color.accentColor) : Color.secondary.opacity(0.18)))
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(size: 13, weight: .medium))
                Text(detail).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 8)
            if busy {
                ProgressView().controlSize(.small)
            } else if let action {
                Button(action.title, action: action.run).controlSize(.small)
            }
            Toggle(title, isOn: Binding(get: { isOn }, set: toggle))
                .toggleStyle(.switch)
                .labelsHidden()
                .controlSize(.small)
                .disabled(busy)
        }
    }
}

struct Panel: View {
    @ObservedObject var model: Model
    @State private var openAtLogin = SMAppService.mainApp.status == .enabled

    private let relative: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f
    }()

    private func ago(_ date: Date?) -> String {
        date.map { relative.localizedString(for: $0, relativeTo: Date()) } ?? "never"
    }

    private var headline: (String, Color) {
        guard let s = model.state else { return ("Loading…", .secondary) }
        switch s.checking {
        case "laptop": return ("The Mac is checking sites", .green)
        case "lambda": return ("Lambda is checking sites", .green)
        default: return ("Nothing is checking sites", .orange)
        }
    }

    private var macDetail: String {
        guard let s = model.state else { return "" }
        if !s.switches.laptop { return "Off" }
        if s.checking == "laptop" { return "Checking · last pass \(ago(s.lastPass("laptop")))" }
        if s.laptop_app_pid == nil { return "On, but role-radar start isn't running" }
        return "Taking over from Lambda"
    }

    /// Switched on but `role-radar start` isn't running: offer to start it (via the login item).
    private var macNeedsStart: Bool {
        guard let s = model.state else { return false }
        return s.switches.laptop && s.laptop_app_pid == nil
    }

    private var lambdaDetail: String {
        guard let s = model.state else { return "" }
        if !s.switches.lambda { return "Off" }
        if s.checking == "lambda" { return "Checking every 5 min · last pass \(ago(s.lastPass("lambda")))" }
        return "Standing by · covers when the Mac isn't"
    }

    private var alertsOff: Bool {
        guard let s = model.state else { return false }
        return !s.switches.discord && !s.switches.email
    }

    private func alertDetail(_ name: String) -> String {
        guard let s = model.state else { return "" }
        if alertsOff { return "Off · new matches are saved for later" }
        return s.switches[name] ? "New matches are sent here" : "Off"
    }

    private func alertRow(_ title: String, _ name: String, symbol: String) -> SwitchRow {
        let on = model.state?.switches[name] ?? false
        return SwitchRow(title: title, symbol: symbol, detail: alertDetail(name), isOn: on, active: on,
                         busy: model.busy.contains(name)) { on in Task { await model.set(name, on: on) } }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Role Radar").font(.system(size: 13, weight: .semibold))
                Spacer()
                Button {
                    Task { await model.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Refresh")
            }
            Label(headline.0, systemImage: "circle.fill")
                .font(.system(size: 12))
                .foregroundStyle(headline.1)
                .labelStyle(DotLabel())

            VStack(spacing: 10) {
                SwitchRow(title: "Mac", symbol: "laptopcomputer", detail: macDetail,
                          isOn: model.state?.switches.laptop ?? false, active: model.state?.checking == "laptop",
                          busy: model.busy.contains("laptop"),
                          action: macNeedsStart ? ("Start", { Task { await model.set("laptop", on: true) } }) : nil
                ) { on in Task { await model.set("laptop", on: on) } }
                SwitchRow(title: "Lambda", symbol: "cloud", detail: lambdaDetail,
                          isOn: model.state?.switches.lambda ?? false, active: model.state?.checking == "lambda",
                          busy: model.busy.contains("lambda")) { on in Task { await model.set("lambda", on: on) } }
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
            .disabled(model.state == nil)

            Text("Both on: the Mac checks while this app is open, Lambda covers when it isn't.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            Text("Alerts").font(.system(size: 12, weight: .semibold))
            VStack(spacing: 10) {
                alertRow("Discord", "discord", symbol: "bubble.left.and.bubble.right")
                alertRow("Email", "email", symbol: "envelope")
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
            .disabled(model.state == nil)

            Text(alertsOff
                 ? "Both off: sites are still checked. New matches are sent when you switch one back on."
                 : "Only the channels switched on get new matches.")
                .font(.system(size: 11)).foregroundStyle(alertsOff ? Color.orange : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if let error = model.error {
                Text(error).font(.system(size: 11)).foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider()
            HStack {
                Toggle("Open at Login", isOn: Binding(get: { openAtLogin }, set: setOpenAtLogin))
                    .toggleStyle(.checkbox)
                    .font(.system(size: 12))
                Spacer()
                Button("Log") {
                    let log = FileManager.default.homeDirectoryForCurrentUser
                        .appendingPathComponent("Library/Logs/role-radar.log")
                    NSWorkspace.shared.open(log)
                }
                Button("Quit") { NSApp.terminate(nil) }
                    .help("Also stops the Mac's checker; Lambda takes over")
            }
            .controlSize(.small)
        }
        .padding(14)
        .frame(width: 310)
        .task { await model.refresh() }
    }

    private func setOpenAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            model.error = "Open at Login: \(error.localizedDescription)"
        }
        openAtLogin = SMAppService.mainApp.status == .enabled
    }
}

struct DotLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 6) {
            configuration.icon.font(.system(size: 7))
            configuration.title.foregroundStyle(.primary)
        }
    }
}

#if !PANEL_SNAPSHOT
final class AppDelegate: NSObject, NSApplicationDelegate {
    /// Quit (or logging out) stops the Mac's checker with the app.
    func applicationWillTerminate(_ notification: Notification) {
        Model.stopChecker()
    }
}

@main
struct RoleRadarMenuApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = Model()

    var body: some Scene {
        MenuBarExtra {
            Panel(model: model)
        } label: {
            Image(systemName: model.menuSymbol)
                .accessibilityLabel(model.menuLabel)
        }
        .menuBarExtraStyle(.window)
    }
}
#endif
