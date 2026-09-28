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
import Charts
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

    /// The last 24 hours, from each pass's counts (oldest hour first).
    struct Activity: Decodable {
        struct Hour: Decodable, Identifiable {
            let start: String
            let mac: Int
            let lambda: Int
            var id: String { start }
            var date: Date { ISO8601DateFormatter().date(from: start) ?? .distantPast }
        }

        let hours: [Hour]
        let checked: Int
        let failed: Int
        let new_jobs: Int
        let matches: Int
        let alerts: Int
    }

    /// The latest pass by any runner, with what it left behind.
    struct Pass: Decodable {
        let runner: String
        let finished_at: String?
        let checked: Int?
        let failed: Int?
        let seconds: Double?
        let failing: Int?  // sites whose last check failed
        let pending: Int?  // matches not sent yet
    }

    var switches: Switches
    let checking: String?
    let lease_holder: String?
    let laptop_app_pid: Int?
    let last_runs: [String: Run]
    let login_item: Bool?
    let activity: Activity?
    let latest_pass: Pass?
    let next_digest: String?

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

extension Color {
    init(hex: Int) {
        self.init(red: Double(hex >> 16 & 0xff) / 255, green: Double(hex >> 8 & 0xff) / 255, blue: Double(hex & 0xff) / 255)
    }
}

/// Checks per hour for the last 24 hours (the Mac and Lambda stacked), the day's totals, and health.
struct ActivityView: View {
    let activity: RunnerState.Activity
    let latest: RunnerState.Pass?
    let nextDigest: Date?
    let alertsOff: Bool
    let ago: (Date?) -> String
    @Environment(\.colorScheme) private var scheme
    @State private var hovered: Date?

    private static let runners = ["Mac", "Lambda"]

    /// Categorical slots 1 and 2 of the chart palette, stepped for light or dark.
    private func color(_ runner: String) -> Color {
        switch (runner, scheme) {
        case ("Mac", .dark): return Color(hex: 0x3987e5)
        case ("Mac", _): return Color(hex: 0x2a78d6)
        case (_, .dark): return Color(hex: 0xd95926)
        default: return Color(hex: 0xeb6834)
        }
    }

    private func sameHour(_ a: Date, _ b: Date) -> Bool {
        Calendar.current.isDate(a, equalTo: b, toGranularity: .hour)
    }

    private var hoveredHour: RunnerState.Activity.Hour? {
        hovered.flatMap { h in activity.hours.first { sameHour($0.date, h) } }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Text("Activity").font(.system(size: 12, weight: .semibold))
                Spacer()
                ForEach(Self.runners, id: \.self) { runner in
                    HStack(spacing: 4) {
                        RoundedRectangle(cornerRadius: 2).fill(color(runner)).frame(width: 8, height: 8)
                        Text(runner).font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
            }
            chart.frame(height: 70)
                .overlay {
                    if activity.checked == 0 {
                        Text("No checks in the last 24 hours").font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
            Text(caption).font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()

            HStack(alignment: .top, spacing: 0) {
                stat(activity.checked, "checks")
                stat(activity.new_jobs, "new jobs")
                stat(activity.matches, "matches")
                stat(activity.alerts, "alerts sent")
            }
            .padding(.vertical, 2)

            if let latest {
                statusLine(lastPass(latest), symbol: "clock", tint: .secondary)
                if let failing = latest.failing {
                    statusLine(failing == 0 ? "Every site's last check worked" : "\(failing) site\(failing == 1 ? "" : "s") failing (see role-radar status)",
                               symbol: failing == 0 ? "checkmark.circle.fill" : "exclamationmark.triangle.fill",
                               tint: failing == 0 ? .green : .orange)
                }
                if let pending = latest.pending, pending > 0 {
                    statusLine(waiting(pending), symbol: "tray.full", tint: alertsOff ? .orange : .secondary)
                }
            }
        }
    }

    private var chart: some View {
        Chart {
            ForEach(activity.hours) { hour in
                ForEach(Self.runners, id: \.self) { runner in
                    BarMark(x: .value("Hour", hour.date, unit: .hour),
                            y: .value("Checks", runner == "Mac" ? hour.mac : hour.lambda), width: .ratio(0.72))
                        .foregroundStyle(by: .value("Runner", runner))
                        .opacity(hovered == nil || sameHour(hour.date, hovered!) ? 1 : 0.35)
                }
            }
        }
        .chartForegroundStyleScale(domain: Self.runners, range: Self.runners.map(color))
        .chartLegend(.hidden)
        .chartXAxis {
            AxisMarks(values: .stride(by: .hour, count: 6)) { _ in
                AxisTick(stroke: StrokeStyle(lineWidth: 1)).foregroundStyle(Color.secondary.opacity(0.4))
                AxisValueLabel(format: .dateTime.hour()).font(.system(size: 10)).foregroundStyle(Color.secondary)
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading, values: .automatic(desiredCount: 2)) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 1)).foregroundStyle(Color.secondary.opacity(0.18))
                AxisValueLabel().font(.system(size: 10)).foregroundStyle(Color.secondary)
            }
        }
        .chartOverlay { proxy in
            GeometryReader { geo in
                Rectangle().fill(.clear).contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let point):
                            guard let plot = proxy.plotFrame else { return }
                            hovered = proxy.value(atX: point.x - geo[plot].origin.x, as: Date.self)
                        case .ended:
                            hovered = nil
                        }
                    }
            }
        }
        .accessibilityLabel("Checks per hour over the last 24 hours, by the Mac and by Lambda")
    }

    private var caption: String {
        guard let hour = hoveredHour else { return "Checks per hour · hover a bar for details" }
        return "\(hour.date.formatted(.dateTime.hour())): Mac \(hour.mac.formatted()) · Lambda \(hour.lambda.formatted()) checks"
    }

    private func stat(_ value: Int, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(value.formatted(.number.notation(.compactName))).font(.system(size: 15, weight: .semibold)).monospacedDigit()
            Text(label).font(.system(size: 10)).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func statusLine(_ text: String, symbol: String, tint: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: symbol).font(.system(size: 10)).foregroundStyle(tint).frame(width: 12)
            Text(text).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func lastPass(_ pass: RunnerState.Pass) -> String {
        let who = pass.runner == "lambda" ? "Lambda" : "the Mac"
        let finished = pass.finished_at.flatMap { ISO8601DateFormatter().date(from: $0) }
        var text = "Last pass \(ago(finished)) by \(who): \((pass.checked ?? 0).formatted()) checked"
        if let failed = pass.failed, failed > 0 { text += ", \(failed) failed" }
        if let seconds = pass.seconds, seconds >= 1 {
            text += " in " + Duration.seconds(seconds.rounded()).formatted(.units(allowed: [.minutes, .seconds], width: .narrow))
        }
        return text
    }

    private func waiting(_ pending: Int) -> String {
        let jobs = "\(pending) match\(pending == 1 ? "" : "es")"
        if alertsOff { return "\(jobs) saved; they're sent when an alert is switched back on" }
        let when = nextDigest.map { " (next digest \($0.formatted(date: .omitted, time: .shortened)))" } ?? ""
        return "\(jobs) waiting to be sent\(when)"
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

            if let activity = model.state?.activity {
                ActivityView(activity: activity, latest: model.state?.latest_pass,
                             nextDigest: model.state?.next_digest.flatMap { ISO8601DateFormatter().date(from: $0) },
                             alertsOff: alertsOff, ago: ago)
                    .padding(10)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
            }

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
