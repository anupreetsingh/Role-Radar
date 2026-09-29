// Role Radar menu bar app: on/off switches for the Mac and Lambda runners,
// and for the Discord and email alerts, and a Live Tracking window.
//
// Runners. Both on: the Mac checks while this app is open; Lambda covers when
// it isn't. One on: only that runner checks. Both off: nothing checks.
// The Mac's checker (`role-radar start`, run by launchd) lives with this app:
// the app starts it, restarts it within a minute if it stops, and stops it on
// quit. Quitting and reopening the app restarts it on the current code.
// Alerts. Each goes out only to the channels switched on. Both off: sites are
// still checked and new matches saved, then sent once one is back on.
// Live Tracking. Matches waiting to be sent (newest first), skipped and sent,
// read every 5 seconds while the window is open; the round in progress; and
// the last 24 hours' activity. A skipped match is never sent: the next alert
// records it instead, and until then it can be unskipped. Send Now sends the
// waiting matches without waiting for the next alert time.
// Every read and write goes through the role-radar CLI (`switch --json`,
// `matches --json`), so the rules live in one place. Build with scripts/build_menubar.sh.

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
    let waiting: Int?  // matches waiting to be sent, not counting skipped ones
    let round: RoundInfo?

    /// The latest pass by a runner whose name starts with `prefix` ("laptop" or "lambda").
    func lastPass(_ prefix: String) -> Date? {
        let iso = ISO8601DateFormatter()
        return last_runs.filter { $0.key.hasPrefix(prefix) }
            .compactMap { $0.value.finished_at.flatMap(iso.date(from:)) }
            .max()
    }
}

/// How far the latest round of checks has got, by whichever runner ran it.
struct RoundInfo: Decodable {
    let runner: String
    let started_at: String?
    let finished_at: String?
    let total: Int?
    let done: Int?
    let stale: Bool  // stopped reporting before it finished: the runner quit or lost the lease

    private var who: String { runner == "lambda" ? "Lambda" : "The Mac" }
    private var count: String { "\((done ?? 0).formatted()) of \((total ?? 0).formatted())" }

    /// Between 0 and 1 while the round is going.
    var fraction: Double? {
        guard finished_at == nil, !stale, let total, total > 0 else { return nil }
        return min(1, Double(done ?? 0) / Double(total))
    }

    var summary: String {
        if let end = When.date(finished_at) {
            var text = "Last round finished \(When.short(end))"
            if let start = When.date(started_at), end.timeIntervalSince(start) >= 60 {
                text += ", took " + Duration.seconds(end.timeIntervalSince(start).rounded())
                    .formatted(.units(allowed: [.hours, .minutes], width: .abbreviated))
            }
            return text + " · \((total ?? 0).formatted()) checks"
        }
        if stale { return "\(who)'s last round stopped at \(count)" }
        let started = When.date(started_at).map { " · started \(When.short($0))" } ?? ""
        return "\(who) is checking: \(count)\(started)"
    }
}

/// Live Tracking's lists (`role-radar matches --json`), newest first.
struct LiveState: Decodable {
    struct Match: Decodable, Identifiable {
        let company: String
        let uid: String
        let title: String
        let location: String?
        let url: String
        let first_seen: String
        var skipped_at: String?
        let final: Bool  // the skip was applied: it can't be undone
        var id: String { company + "#" + uid }
    }

    /// One alert that went out: when, by which runner, and its jobs, newest first as the alert listed them.
    struct Alert: Decodable, Identifiable {
        struct Job: Decodable {
            let company: String?
            let title: String?
            let location: String?
            let url: String?
        }

        let sent_at: String
        let by: String?
        let jobs: [Job]
        var id: String { sent_at }
    }

    var waiting: [Match]
    var skipped: [Match]
    let sent: [Alert]
    let round: RoundInfo?
    let next_digest: String?
    let send_requested: Bool
    let alerts_off: Bool

    /// Move a match between Waiting and Skipped straight away, before the CLI confirms it.
    mutating func setSkipped(_ id: String, _ on: Bool) {
        if on, let i = waiting.firstIndex(where: { $0.id == id }) {
            var match = waiting.remove(at: i)
            match.skipped_at = When.iso.string(from: Date())
            skipped.insert(match, at: 0)
        } else if !on, let i = skipped.firstIndex(where: { $0.id == id && !$0.final }) {
            var match = skipped.remove(at: i)
            match.skipped_at = nil
            waiting.append(match)
            waiting.sort { $0.first_seen > $1.first_seen }
        }
    }
}

/// Times as the app shows them.
enum When {
    static let iso = ISO8601DateFormatter()
    private static let relative: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f
    }()

    static func date(_ text: String?) -> Date? { text.flatMap(iso.date(from:)) }

    /// "Sep 29, 3:50 PM", always with the day.
    static func full(_ date: Date) -> String {
        date.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }

    /// "2:32 PM" today, "Sep 28, 2:32 PM" before.
    static func short(_ date: Date) -> String {
        Calendar.current.isDateInToday(date)
            ? date.formatted(date: .omitted, time: .shortened)
            : date.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }

    static func ago(_ date: Date?) -> String {
        date.map { relative.localizedString(for: $0, relativeTo: Date()) } ?? "never"
    }

    /// "2:32 PM (12 min. ago)"
    static func stamp(_ text: String?) -> String {
        guard let date = date(text) else { return "" }
        return "\(short(date)) (\(ago(date)))"
    }
}

@MainActor
final class Model: ObservableObject {
    @Published var state: RunnerState?
    @Published var busy: Set<String> = []
    @Published var error: String?
    @Published var live: LiveState?
    @Published var liveBusy: Set<String> = []  // match ids, "all" (Skip All) or "send" (Send Now) in flight
    @Published var liveError: String?
    private var liveActions = 0  // a refresh that started before the latest skip or send is out of date

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

    /// Re-read Live Tracking's lists.
    func refreshLive() async {
        let seen = liveActions
        await runLive(["matches", "--json"], unless: { self.liveActions != seen })
    }

    func skip(_ match: LiveState.Match, _ on: Bool) async {
        liveActions += 1
        liveBusy.insert(match.id)
        live?.setSkipped(match.id, on)
        await runLive(["matches", on ? "skip" : "unskip", match.company, match.uid, "--json"])
        liveBusy.remove(match.id)
    }

    func skipAll() async {
        liveActions += 1
        liveBusy.insert("all")
        for match in live?.waiting ?? [] { live?.setSkipped(match.id, true) }
        await runLive(["matches", "skip", "--all", "--json"])
        liveBusy.remove("all")
    }

    func sendNow() async {
        liveActions += 1
        liveBusy.insert("send")
        await runLive(["matches", "send", "--json"])
        liveBusy.remove("send")
    }

    private func runLive(_ args: [String], unless outdated: () -> Bool = { false }) async {
        let config = projectDir + "/config/companies.yaml"
        let result = await Self.cli(python: python, dir: projectDir, args: ["-m", "role_radar"] + args + ["--config", config])
        if outdated() { return }
        switch result {
        case .success(let data):
            do {
                live = try JSONDecoder().decode(LiveState.self, from: data)
                liveError = nil
            } catch {
                liveError = "Unexpected reply from role-radar: \(error.localizedDescription)"
            }
        case .failure(let message):
            liveError = message
        }
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

/// Checks per hour for the last 24 hours (the Mac and Lambda stacked), and the day's totals.
struct ActivityView: View {
    let activity: RunnerState.Activity
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
            chart.frame(height: 90)
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
}

struct StatusLine: View {
    let text: String
    let symbol: String
    var tint: Color = .secondary

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: symbol).font(.system(size: 10)).foregroundStyle(tint).frame(width: 12)
            Text(text).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// The lines both the menu and the Live Tracking window show: the round, matches waiting, failing sites.
struct Health {
    let state: RunnerState?
    let live: LiveState?

    private var alertsOff: Bool { live?.alerts_off ?? (state.map { !$0.switches.discord && !$0.switches.email } ?? false) }

    var round: StatusLine {
        StatusLine(text: (live?.round ?? state?.round)?.summary ?? "No rounds recorded yet", symbol: "arrow.triangle.2.circlepath")
    }

    var waiting: StatusLine {
        let count = live?.waiting.count ?? state?.waiting ?? 0
        let jobs = count == 0 ? "No matches waiting" : "\(count) match\(count == 1 ? "" : "es") waiting"
        if alertsOff {
            return StatusLine(text: count == 0 ? "\(jobs) · alerts are off" : "\(jobs) · sent when an alert is switched back on",
                              symbol: "tray.full", tint: .orange)
        }
        if live?.send_requested == true { return StatusLine(text: "\(jobs) · sending now", symbol: "paperplane") }
        let next = When.date(live?.next_digest ?? state?.next_digest).map { " · next alert \(When.short($0))" } ?? ""
        return StatusLine(text: count == 0 ? jobs : jobs + next, symbol: "tray.full")
    }

    var failing: StatusLine? {
        guard let failing = state?.latest_pass?.failing else { return nil }
        return failing == 0
            ? StatusLine(text: "Every site's last check worked", symbol: "checkmark.circle.fill", tint: .green)
            : StatusLine(text: "\(failing) site\(failing == 1 ? "" : "s") failing (see role-radar status)",
                         symbol: "exclamationmark.triangle.fill", tint: .orange)
    }
}

struct Panel: View {
    @ObservedObject var model: Model
    @Environment(\.openWindow) private var openWindow
    @State private var openAtLogin = SMAppService.mainApp.status == .enabled

    private func ago(_ date: Date?) -> String { When.ago(date) }

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

            if model.state != nil {
                let health = Health(state: model.state, live: nil)
                VStack(alignment: .leading, spacing: 8) {
                    health.round
                    health.waiting
                    health.failing
                    Button {
                        openWindow(id: LiveWindow.id)
                        NSApp.activate()
                    } label: {
                        Label("Live Tracking", systemImage: "list.bullet.rectangle.portrait")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .padding(.top, 2)
                    .help("Matches waiting to be sent, skipped and sent, the round in progress, and activity")
                }
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

/// One match in Live Tracking: title, company and place, when. Clicking it opens the posting;
/// its button skips it (or undoes that).
struct MatchRow: View {
    let title: String
    let company: String?
    let location: String?
    let url: String?
    let when: String
    var dimmed = false
    var action: (title: String, help: String, run: () -> Void)? = nil
    var busy = false
    @State private var hovering = false

    private var link: URL? { url.flatMap(URL.init(string:)) }

    private func open() {
        if let link { NSWorkspace.shared.open(link) }
    }

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Button(action: open) {
                HStack(alignment: .center, spacing: 8) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(title).font(.system(size: 13, weight: .medium)).lineLimit(2)
                            .foregroundStyle(hovering ? Color.accentColor : Color.primary)
                        Text([company, location].compactMap { $0 }.joined(separator: " · "))
                            .font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                        if !when.isEmpty {
                            Text(when).font(.system(size: 11)).foregroundStyle(.tertiary).monospacedDigit()
                        }
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "arrow.up.right")
                        .font(.system(size: 11, weight: .semibold)).foregroundStyle(Color.accentColor)
                        .opacity(hovering ? 1 : 0)
                }
                .padding(.vertical, 5).padding(.horizontal, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(hovering ? 0.07 : 0)))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(link == nil)
            .onHover { hovering = $0 && link != nil }
            .modifier(LinkCursor(active: link != nil))
            .help(link == nil ? "" : "Open the posting in your browser")
            .padding(.horizontal, -6)

            if busy {
                ProgressView().controlSize(.small).frame(width: 58)
            } else if let action {
                Button(action.title, action: action.run).controlSize(.small).frame(minWidth: 58).help(action.help)
            }
        }
        .opacity(dimmed ? 0.55 : 1)
        .contextMenu {
            if link != nil {
                Button("Open Posting", action: open)
                Button("Copy Link") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(url ?? "", forType: .string)
                }
            }
        }
    }
}

/// The pointing hand over something that opens a link.
struct LinkCursor: ViewModifier {
    let active: Bool

    func body(content: Content) -> some View {
        if #available(macOS 15, *) {
            content.pointerStyle(active ? .link : nil)
        } else {
            content.onHover { inside in
                guard active else { return }
                if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() }
            }
        }
    }
}

/// The bigger window: the round in progress, alerts, activity, and the matches waiting, skipped and sent.
struct LiveWindow: View {
    static let id = "live"
    @ObservedObject var model: Model
    @State private var opened: Set<String> = []  // sent alerts unfolded: each starts folded

    private var live: LiveState? { model.live }

    var body: some View {
        HSplitView {
            ScrollView { sidebar.padding(16) }
                .frame(minWidth: 300, idealWidth: 330, maxWidth: 380)
            lists.frame(minWidth: 460)
        }
        .frame(minWidth: 820, minHeight: 520)
        .task {
            while !Task.isCancelled {
                await model.refreshLive()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    // -- sidebar: what's happening now ------------------------------------------

    private var sidebar: some View {
        let health = Health(state: model.state, live: live)
        return VStack(alignment: .leading, spacing: 14) {
            card {
                Text("Now").font(.system(size: 12, weight: .semibold))
                health.round
                if let fraction = live?.round?.fraction {
                    ProgressView(value: fraction).controlSize(.small)
                }
                health.failing
            }
            card {
                Text("Alerts").font(.system(size: 12, weight: .semibold))
                health.waiting
                Button {
                    Task { await model.sendNow() }
                } label: {
                    Label(live?.send_requested == true ? "Sending…" : "Send Now", systemImage: "paperplane")
                        .frame(maxWidth: .infinity)
                }
                .disabled(!canSend)
                .help(sendHelp)
                Text(sendHelp).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let activity = model.state?.activity {
                card { ActivityView(activity: activity) }
            }
            if let error = model.liveError ?? model.error {
                Text(error).font(.system(size: 11)).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var canSend: Bool {
        guard let live else { return false }
        return !live.alerts_off && !live.waiting.isEmpty && !live.send_requested && !model.liveBusy.contains("send")
    }

    private var sendHelp: String {
        guard let live else { return "" }
        if live.alerts_off { return "Switch Discord or email on to send the waiting matches." }
        if live.send_requested {
            return model.state?.checking == "lambda" ? "Lambda sends them at its next run (within 5 minutes)."
                                                     : "The Mac sends them within a minute."
        }
        return "Sends the waiting matches now, not at the next alert time. Skipped ones are never sent."
    }

    private func card<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8, content: content)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
    }

    // -- the lists ----------------------------------------------------------------

    @ViewBuilder private var lists: some View {
        if let live {
            List {
                if live.alerts_off {
                    Label("Alerts are off: matches collect here, newest on top, and go out when you switch Discord or email back on.",
                          systemImage: "bell.slash")
                        .font(.system(size: 12)).foregroundStyle(.orange)
                        .padding(.vertical, 4)
                }
                Section {
                    if live.waiting.isEmpty {
                        Text("Nothing waiting. New matches appear here as soon as their company is checked.")
                            .font(.system(size: 12)).foregroundStyle(.secondary).padding(.vertical, 4)
                    }
                    ForEach(live.waiting) { match in
                        MatchRow(title: match.title, company: match.company, location: match.location, url: match.url,
                                 when: "Found " + When.stamp(match.first_seen),
                                 action: ("Skip", "Don't send this one", { Task { await model.skip(match, true) } }),
                                 busy: model.liveBusy.contains(match.id) || model.liveBusy.contains("all"))
                    }
                } header: {
                    header("Waiting to be sent", live.waiting.count) {
                        Button("Skip All") { Task { await model.skipAll() } }
                            .controlSize(.small)
                            .disabled(live.waiting.isEmpty || model.liveBusy.contains("all"))
                            .help("Don't send any of the matches waiting now")
                    }
                }
                Section {
                    if live.skipped.isEmpty {
                        Text("Skipped matches are never sent. You can unskip one until the next alert goes out.")
                            .font(.system(size: 12)).foregroundStyle(.secondary).padding(.vertical, 4)
                    }
                    ForEach(live.skipped) { match in
                        MatchRow(title: match.title, company: match.company, location: match.location, url: match.url,
                                 when: "Skipped " + When.stamp(match.skipped_at) + (match.final ? " · won't be sent" : ""),
                                 dimmed: true,
                                 action: match.final ? nil : ("Unskip", "Send this one with the next alert",
                                                              { Task { await model.skip(match, false) } }),
                                 busy: model.liveBusy.contains(match.id))
                    }
                } header: {
                    header("Skipped", live.skipped.count) { EmptyView() }
                }
                Section {
                    if live.sent.isEmpty {
                        Text("No alerts sent lately.").font(.system(size: 12)).foregroundStyle(.secondary).padding(.vertical, 4)
                    }
                    ForEach(live.sent) { alert in
                        DisclosureGroup(isExpanded: expanded(alert.id)) {
                            ForEach(Array(alert.jobs.enumerated()), id: \.offset) { _, job in
                                MatchRow(title: job.title ?? "", company: job.company, location: job.location, url: job.url, when: "")
                            }
                        } label: {
                            Button {
                                withAnimation(.easeInOut(duration: 0.15)) { expanded(alert.id).wrappedValue.toggle() }
                            } label: {
                                alertLabel(alert).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .help(opened.contains(alert.id) ? "Hide this alert's jobs" : "Show this alert's jobs")
                        }
                    }
                } header: {
                    header("Sent alerts", live.sent.count) { EmptyView() }
                }
            }
            .listStyle(.inset)
        } else if let error = model.liveError {
            ContentUnavailableView("Couldn't load the matches", systemImage: "exclamationmark.triangle", description: Text(error))
        } else {
            ProgressView("Loading matches…").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func expanded(_ id: String) -> Binding<Bool> {
        Binding(get: { opened.contains(id) },
                set: { open in if open { opened.insert(id) } else { opened.remove(id) } })
    }

    private func alertLabel(_ alert: LiveState.Alert) -> some View {
        let sent = When.date(alert.sent_at)
        let jobs = "\(alert.jobs.count) job\(alert.jobs.count == 1 ? "" : "s")"
        let by = alert.by == "lambda" ? "Lambda" : (alert.by == nil ? nil : "the Mac")
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "paperplane.fill").font(.system(size: 11)).foregroundStyle(.secondary)
            Text(sent.map(When.full) ?? alert.sent_at).font(.system(size: 13, weight: .semibold)).monospacedDigit()
            Text([jobs, by.map { "sent by \($0)" }, sent.map { When.ago($0) }].compactMap { $0 }.joined(separator: " · "))
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }
        .padding(.vertical, 3)
    }

    private func header<Trailing: View>(_ title: String, _ count: Int, @ViewBuilder trailing: () -> Trailing) -> some View {
        HStack {
            Text(title).font(.system(size: 12, weight: .semibold))
            Text(count.formatted()).font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
            Spacer()
            trailing()
        }
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

        Window("Live Tracking", id: LiveWindow.id) {
            LiveWindow(model: model)
        }
        .defaultSize(width: 1000, height: 680)
    }
}
#endif
