// Role Radar menu bar app: on/off switches for the Mac and Lambda runners,
// and for the Discord and email alerts, and a Live Tracking window.
//
// Runners. Both on: the Mac checks while this app is open; Lambda covers when
// it isn't. One on: only that runner checks. Both off: nothing checks. Without
// AWS (runtime.storage sqlite) there's no Lambda: the Mac checks while it's open.
// The Mac's checker (`role-radar start`, run by launchd) lives with this app:
// the app starts it, restarts it within a minute if it stops, and stops it on
// quit. Quitting and reopening the app restarts it on the current code.
// Alerts are optional. Each goes out only to the channels switched on (a channel
// that isn't set up stays off). Both off: sites are still checked, and new
// matches collect in Live Tracking.
// Live Tracking. The stack of new jobs (newest first), those cleared from it, and
// the alerts sent, read every 5 seconds while the window is open; the round in
// progress; and the last 24 hours' activity. Ticked jobs, or all of them, can be
// cleared (never sent; undoable until the next digest records it, within minutes)
// or sent now, alerts on or off.
// Every read and write goes through the role-radar CLI (`switch --json`,
// `matches --json`), so the rules live in one place. Build with scripts/build_menubar.sh
// (it runs the project's code), or package it with scripts/package_app.sh: then the app
// carries its own Python and keeps its files in ~/Library/Application Support/Role Radar,
// and a Setup window (`role-radar setup`) asks for a profession, job titles and places, and a Gmail account. It sits
// in the Dock while one of its windows is open, and in the menu bar always. Built with Sparkle
// (package_app.sh), it also updates itself: see Updates.

import AppKit
import Charts
import Combine
import Observation
import ServiceManagement
import SwiftUI
#if canImport(Sparkle)
import Sparkle
#endif

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
    let storage: String?  // "dynamodb" with AWS (the Mac and Lambda), "sqlite" on this Mac only

    var hasLambda: Bool { !Place.packaged && (storage ?? "dynamodb") == "dynamodb" }

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
    struct Match: Decodable, Identifiable, Equatable {
        let company: String
        let uid: String
        let title: String
        let location: String?
        let url: String
        let first_seen: String
        var skipped_at: String?
        var send_at: String?  // sent from Live Tracking: on its way out
        var id: String { company + "#" + uid }
    }

    /// One alert that went out: when, by which runner, and its jobs, newest first as the alert listed them.
    struct Alert: Decodable, Identifiable, Equatable {
        struct Job: Decodable, Equatable {
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

    /// Move matches between Waiting and Skipped straight away, before the CLI confirms it: in one pass,
    /// so marking hundreds as seen redraws the list once, not once a match.
    mutating func setSkipped(_ ids: Set<String>, _ on: Bool) {
        if on {
            let now = When.iso.string(from: Date())
            var moved: [Match] = []
            waiting.removeAll { match in
                guard ids.contains(match.id) else { return false }
                var seen = match
                seen.skipped_at = now
                moved.append(seen)
                return true
            }
            skipped.insert(contentsOf: moved, at: 0)
        } else {
            var back: [Match] = []
            skipped.removeAll { match in
                guard ids.contains(match.id) else { return false }
                var new = match
                new.skipped_at = nil
                back.append(new)
                return true
            }
            waiting.append(contentsOf: back)
            waiting.sort { $0.first_seen > $1.first_seen }
        }
    }

    /// Show matches as on their way out straight away, before the CLI confirms it.
    mutating func setSending(_ ids: Set<String>) {
        let now = When.iso.string(from: Date())
        for i in waiting.indices where ids.contains(waiting[i].id) { waiting[i].send_at = now }
    }
}

/// Times as the app shows them.
/// How big the app's text is, as a multiple of each font's own size, for every window and the menu
/// bar panel. It starts at `standard` whenever the app opens; A−/A+ in the windows' toolbars and
/// ⌘= / ⌘− / ⌘0 change it until the app quits.
enum TextSize {
    static let key = "textScale"
    static let standard = 1.15
    static let range = 0.85...1.75

    static var scale: Double { UserDefaults.standard.object(forKey: key) as? Double ?? standard }

    static func change(by step: Double) {
        let next = (scale + step).clamped(to: range)
        UserDefaults.standard.set((next * 100).rounded() / 100, forKey: key)
    }

    static func reset() { UserDefaults.standard.removeObject(forKey: key) }

    /// Buttons and switches a size up as the text grows (macOS draws each control size's text at a fixed size).
    static func controls(_ scale: Double) -> ControlSize { scale >= 1.6 ? .large : scale >= 1.3 ? .regular : .small }
}

extension Comparable {
    func clamped(to limits: ClosedRange<Self>) -> Self { min(max(self, limits.lowerBound), limits.upperBound) }
}

/// A system font at `size` points times the text size setting.
struct ScaledFont: ViewModifier {
    @AppStorage(TextSize.key) private var scale = TextSize.standard
    let size: CGFloat
    var weight: Font.Weight = .regular
    var design: Font.Design = .default

    func body(content: Content) -> some View {
        content.font(.system(size: size * scale, weight: weight, design: design))
    }
}

extension View {
    func scaledFont(_ size: CGFloat, weight: Font.Weight = .regular, design: Font.Design = .default) -> some View {
        modifier(ScaledFont(size: size, weight: weight, design: design))
    }
}

/// Smaller and bigger text, for a window's toolbar.
struct TextSizeButtons: View {
    @AppStorage(TextSize.key) private var scale = TextSize.standard

    var body: some View {
        ControlGroup {
            Button { TextSize.change(by: -0.1) } label: { Image(systemName: "textformat.size.smaller") }
                .help("Smaller text (⌘−)").disabled(scale <= TextSize.range.lowerBound)
            Button { TextSize.change(by: 0.1) } label: { Image(systemName: "textformat.size.larger") }
                .help("Bigger text (⌘=)").disabled(scale >= TextSize.range.upperBound)
        }
    }
}

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

/// Where the app finds Python, its files and its checker: the project it was built from
/// (build_menubar.sh), or in the packaged app (package_app.sh), everything inside it.
enum Place {
    static let info = Bundle.main.infoDictionary ?? [:]
    static let packaged = info["RRPackaged"] as? Bool ?? false
    static let version = info["CFBundleShortVersionString"] as? String ?? ""
    static let support = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Role Radar").path
    static let agent = "com.roleradar.app.checker"  // the packaged app's own launchd agent
    static let python: String = packaged
        ? (Bundle.main.resourcePath ?? "") + "/python/bin/python3"
        : info["RRPython"] as? String ?? ProcessInfo.processInfo.environment["RR_PYTHON"] ?? "python3"
    static let workDir: String = packaged
        ? support
        : info["RRProjectDir"] as? String ?? ProcessInfo.processInfo.environment["RR_PROJECT_DIR"]
            ?? FileManager.default.currentDirectoryPath
    static let config = packaged ? support + "/companies.yaml" : workDir + "/config/companies.yaml"
    /// The packaged app's state, lock, checker and Keychain items are its own, apart from a checker run from the code.
    static let env: [String: String] = packaged
        ? ["ROLE_RADAR_HOME": support, "ROLE_RADAR_AGENT": agent, "ROLE_RADAR_KEYCHAIN": "com.roleradar.app"] : [:]
    static let log = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/" + (packaged ? agent + ".log" : "role-radar.log"))
    /// Opened straight from Downloads, macOS runs a downloaded app from a hidden read-only copy
    /// ("App Translocation"): the checker it sets up would break once that copy goes away.
    static let translocated = packaged && Bundle.main.bundlePath.contains("/AppTranslocation/")
}

/// The packaged app is in the Dock while one of its windows is open, so it opens and closes like any
/// app: opening it (or clicking its Dock icon) shows Settings, and closing the last window leaves just
/// the menu bar icon, still checking. Opened at login, it starts in the menu bar only.
enum Dock {
    static let openWindow = Notification.Name("RoleRadarOpenWindow")
    static var launchedAtLogin = false
    private static var windows = 0

    static func windowOpened() {
        guard Place.packaged else { return }
        windows += 1
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    static func windowClosed() {
        guard Place.packaged else { return }
        windows = max(0, windows - 1)
        if windows == 0 { NSApp.setActivationPolicy(.accessory) }
    }

    /// In the Dock and in front, for a window that isn't one of ours (Sparkle's), then back out
    /// once it's gone if none of ours is open.
    static func show() {
        guard Place.packaged else { return }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    static func hideIfNoWindows() {
        guard Place.packaged, windows == 0 else { return }
        NSApp.setActivationPolicy(.accessory)
    }
}

/// Updates for the downloadable app, with Sparkle. Its Info.plist names the feed (the latest GitHub
/// release's appcast.xml) and the public key updates must be signed with (scripts/publish_update.sh
/// signs them); it looks for a newer version every few hours, and "Check for Updates…" looks now.
/// Found on a schedule, an update shows as Sparkle's own window, the app brought forward for it.
/// A build without Sparkle (one run from the code) or without a feed (the dev build) has no updater.
@MainActor
final class Updates: NSObject, ObservableObject {
    static let shared = Updates()
    @Published private(set) var available = false
    @Published private(set) var canCheck = false
#if canImport(Sparkle)
    private var controller: SPUStandardUpdaterController?
#endif

    func start() {
#if canImport(Sparkle)
        guard controller == nil, Place.packaged, Place.info["SUFeedURL"] != nil else { return }
        let controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: nil, userDriverDelegate: self)
        controller.updater.publisher(for: \.canCheckForUpdates).assign(to: &$canCheck)
        self.controller = controller
        available = true
#endif
    }

    func check() {
#if canImport(Sparkle)
        Dock.show()
        controller?.checkForUpdates(nil)
#endif
    }
}

#if canImport(Sparkle)
extension Updates: SPUStandardUserDriverDelegate {
    // A menu bar app: an update found on a schedule is shown gently, the app brought forward for it.
    nonisolated var supportsGentleScheduledUpdateReminders: Bool { true }

    nonisolated func standardUserDriverWillHandleShowingUpdate(_ handleShowingUpdate: Bool, forUpdate update: SUAppcastItem,
                                                               state: SPUUserUpdateState) {
        guard handleShowingUpdate else { return }
        MainActor.assumeIsolated { Dock.show() }
    }

    nonisolated func standardUserDriverWillFinishUpdateSession() {
        MainActor.assumeIsolated { Dock.hideIfNoWindows() }
    }
}
#endif

/// "Check for Updates…" in the app menu, when the app can update itself.
struct CheckForUpdates: View {
    @ObservedObject var updates = Updates.shared

    var body: some View {
        if updates.available {
            Button("Check for Updates…") { updates.check() }.disabled(!updates.canCheck)
        }
    }
}

/// What `role-radar setup show` reports.
struct SetupState: Decodable {
    struct Profession: Decodable, Identifiable {
        struct Group: Decodable { let name: String; let titles: [String] }
        let id: String
        let name: String
        let about: String
        let groups: [Group]  // job titles that alert
        let skip_groups: [Group]?  // words that rule a title out
    }
    struct Country: Decodable { let code: String; let name: String }
    let profession: String?
    let professions: [Profession]?
    let roles: [String]
    let exclude: [String]
    let locations: [String]
    let countries: [String]?
    let country_options: [Country]?
    let cities: [String]?
    let max_experience_years: Int?
    let education: String?  // none, bachelors, masters or phd
    let companies: Int  // how many companies checks read: the profession's, in their countries, and their own
    let companies_for: [String: Int]?  // the same for every combination of countries, keyed "US+IN"
    let companies_by_country: [String: Int]?
    let companies_untagged: Int?  // companies whose job locations name no country: tracked for every country
    let email: String?
    let also: [String]  // who else gets the alerts, besides `email`
    let email_ready: Bool
    let discord_ready: Bool?  // a Discord webhook is saved
    let ready: Bool
}

/// `role-radar setup find`: the companies whose name or job site has every word searched for.
struct CompanySearch: Decodable {
    struct Company: Decodable, Identifiable {
        let name: String
        let url: String
        let site: String?  // the reader, e.g. "workday"
        let countries: [String]
        let own: Bool  // added by them, not from the profession's list
        let readable: Bool  // false: on a job site Role Radar can't read yet
        let off: Bool  // they turned it off
        let tracked: Bool
        let why: String?  // why it isn't tracked
        var id: String { name }
    }
    let total: Int
    let results: [Company]
    let untracked: Int  // how many they've turned off
}

/// `role-radar setup add`: a company they asked for, tracked or not. Unless it was listed already, it's
/// also suggested for everyone's list.
struct AddResult: Decodable {
    let status: String  // "added", "listed" (already there) or "failed" (can't be tracked)
    let name: String
    let jobs: Int?
    let url: String?  // failed: listed already, on a job site Role Radar can't read yet
    let why: String?  // listed, but not tracked
    let turned_on: Bool?
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
    private var liveData: Data?  // the lists as last read: the same again changes nothing, so nothing is drawn again
    @Published var setup: SetupState?  // the packaged app's setup; nil when built from the code
    @Published var setupError: String?  // why Setup couldn't read or save its files: always shown
    @Published var wantsWindow = false  // opens the window (the menu bar label watches it)
    @Published var showingSetup = false  // the window shows Setup's pages rather than Live Tracking

    init() {
        Task { [weak self] in
            await self?.prepare()
            while let self {
                await self.refresh()
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }

    /// The packaged app's first steps: trust its own files, create its files, and open Setup until
    /// it's done (or whenever someone opens the app, rather than it opening at login).
    private func prepare() async {
        guard Place.packaged else { return }
        // Downloaded apps carry macOS's quarantine flag, which would stop launchd running the bundled
        // Python. Once the app has been opened (Open Anyway), it clears the flag on itself.
        Self.runQuietly("/usr/bin/xattr", ["-dr", "com.apple.quarantine", Bundle.main.bundlePath])
        if case .failure(let message) = await setupCommand(["init"]) { setupError = message }
        await loadSetup()
        if !(setup?.ready ?? false) || !Dock.launchedAtLogin {
            showingSetup = !(setup?.ready ?? false)  // Setup until it's done, then Live Tracking
            wantsWindow = true
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

    /// The packaged app starts checking only once Setup is done (and it's in Applications); one built from the code always may.
    var canStart: Bool { !Place.packaged || (!Place.translocated && (setup?.ready ?? false)) }

    /// Re-read the state, starting the Mac's checker first if it's switched on and not running.
    func refresh() async {
        await run(["switch", "--json"] + (canStart ? ["--start"] : []))
        // A channel that isn't set up can't send: keep its switch off, so every view says so.
        for name in ["discord", "email"] where state?.switches[name] == true && !channelReady(name) {
            await run(["switch", name, "off", "--json"])
        }
    }

    /// Whether an alert channel is set up: in the packaged app, saved in Setup. One built from the code
    /// keeps its settings elsewhere (the Keychain or AWS), so its switches are always free.
    func channelReady(_ name: String) -> Bool {
        guard Place.packaged, let setup else { return true }
        return name == "email" ? setup.email_ready : setup.discord_ready ?? false
    }

    /// Ask the Mac's checker to finish the companies in flight and quit. Doesn't wait: the
    /// `role-radar stop` it launches outlives this app, and Lambda takes over once it's done.
    nonisolated static func stopChecker() {
        runQuietly(Place.python, ["-m", "role_radar", "stop"], wait: false)
    }

    nonisolated static func runQuietly(_ program: String, _ args: [String], wait: Bool = true) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: program)
        process.arguments = args
        process.currentDirectoryURL = URL(fileURLWithPath: FileManager.default.fileExists(atPath: Place.workDir) ? Place.workDir : "/")
        process.environment = ProcessInfo.processInfo.environment.merging(Place.env) { $1 }
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try? process.run()
        if wait { process.waitUntilExit() }
    }

    // -- setup (the packaged app) --------------------------------------------------

    func loadSetup() async {
        switch await setupCommand(["show"]) {
        case .success(let data):
            do {
                setup = try JSONDecoder().decode(SetupState.self, from: data)
                setupError = nil
            } catch {
                setupError = "Unexpected reply from role-radar: \(error.localizedDescription)"
            }
        case .failure(let message):
            setupError = message
        }
    }

    /// Whether Lambda can check too: only with AWS, and never in the downloadable app.
    var hasLambda: Bool { !Place.packaged && (state?.hasLambda ?? true) }

    /// Try again after a failure: the state, and Setup's in the downloadable app.
    func retry() async {
        await refresh()
        if Place.packaged { await loadSetup() }
    }

    /// Run `role-radar setup ...`, updating `setup` from what it reports. Returns an error message, or nil.
    func setupStep(_ args: [String], stdin: String? = nil) async -> String? {
        switch await setupCommand(args, stdin: stdin) {
        case .success(let data):
            if let fresh = try? JSONDecoder().decode(SetupState.self, from: data) { setup = fresh }
            return nil
        case .failure(let message):
            return message
        }
    }

    func setupCommand(_ args: [String], stdin: String? = nil) async -> CLIResult {
        await Self.cli(args: ["-m", "role_radar", "setup"] + args + ["--config", Place.config], stdin: stdin)
    }

    /// Setup is done: start checking now and at every login.
    func startChecking() async {
        try? SMAppService.mainApp.register()
        await set("laptop", on: true)
        wantsWindow = false
    }

    /// Flip a switch: "laptop" or "lambda" (runners), "discord" or "email" (alerts).
    func set(_ name: String, on: Bool) async {
        busy.insert(name)
        state?.switches[name] = on
        // --start: switching the Mac on also starts its checker if it isn't running (not during Setup).
        await run(["switch", name, on ? "on" : "off", "--json"] + (canStart ? ["--start"] : []))
        busy.remove(name)
    }

    /// Re-read Live Tracking's lists.
    func refreshLive() async {
        let seen = liveActions
        await runLive(["matches", "--json"], unless: { self.liveActions != seen })
    }

    func skip(_ match: LiveState.Match, _ on: Bool) async {
        liveActions += 1
        liveData = nil  // shown changed already: take whatever comes back
        liveBusy.insert(match.id)
        live?.setSkipped([match.id], on)
        await runLive(["matches", on ? "skip" : "unskip", match.company, match.uid, "--json"])
        liveBusy.remove(match.id)
    }

    /// Mark matches as seen (nil: all of them): they leave the stack, and are never sent.
    func markSeen(_ matches: [LiveState.Match]?) async {
        liveActions += 1
        liveData = nil  // shown changed already: take whatever comes back
        liveBusy.insert("seen")
        live?.setSkipped(Set((matches ?? live?.waiting ?? []).map(\.id)), true)
        await runLive(["matches", "skip", "--json"] + Self.picks(matches), stdin: Self.pickList(matches))
        liveBusy.remove("seen")
    }

    /// Put seen matches back among the new ones, while the next digest hasn't recorded them yet.
    func markNew(_ matches: [LiveState.Match]) async {
        liveActions += 1
        liveData = nil
        liveBusy.insert("new")
        live?.setSkipped(Set(matches.map(\.id)), false)
        await runLive(["matches", "unskip", "--json"] + Self.picks(matches), stdin: Self.pickList(matches))
        liveBusy.remove("new")
    }

    /// Send matches now (nil: all of them), alerts on or off; once sent, they leave the stack.
    func send(_ matches: [LiveState.Match]?) async {
        liveActions += 1
        liveData = nil  // shown changed already: take whatever comes back
        liveBusy.insert("send")
        live?.setSending(Set((matches ?? live?.waiting ?? []).map(\.id)))
        await runLive(["matches", "send", "--json"] + Self.picks(matches), stdin: Self.pickList(matches))
        liveBusy.remove("send")
    }

    /// All of them (`--all`), or the picked ones as JSON on stdin: a process takes at most 4,096 arguments,
    /// so one `--pick COMPANY UID` per match ended the app past about 1,360 of them.
    private static func picks(_ matches: [LiveState.Match]?) -> [String] {
        matches == nil ? ["--all"] : ["--stdin"]
    }

    private static func pickList(_ matches: [LiveState.Match]?) -> String? {
        guard let matches else { return nil }
        let pairs = matches.map { [$0.company, $0.uid] }
        return (try? JSONSerialization.data(withJSONObject: pairs)).map { String(decoding: $0, as: UTF8.self) } ?? "[]"
    }

    /// Whether alerts can be sent at all: email or Discord is set up.
    var canAlert: Bool { channelReady("email") || channelReady("discord") }

    private func runLive(_ args: [String], stdin: String? = nil, unless outdated: () -> Bool = { false }) async {
        let result = await Self.cli(args: ["-m", "role_radar"] + args + ["--config", Place.config], stdin: stdin)
        if outdated() { return }
        switch result {
        case .success(let data):
            if data == liveData {
                liveError = nil
                return
            }
            do {
                live = try JSONDecoder().decode(LiveState.self, from: data)
                liveData = data
                liveError = nil
            } catch {
                liveError = "Unexpected reply from role-radar: \(error.localizedDescription)"
            }
        case .failure(let message):
            liveError = message
        }
    }

    private func run(_ args: [String]) async {
        let result = await Self.cli(args: ["-m", "role_radar"] + args + ["--config", Place.config])
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
        if case .success(let data) = await Self.cli(args: ["-m", "role_radar", "switch", "--json", "--config", Place.config]),
            let fresh = try? JSONDecoder().decode(RunnerState.self, from: data) {
            state = fresh
        }
    }

    enum CLIResult { case success(Data), failure(String) }

    nonisolated static func cli(args: [String], stdin: String? = nil) async -> CLIResult {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: Place.python)
                process.arguments = args
                try? FileManager.default.createDirectory(atPath: Place.workDir, withIntermediateDirectories: true)
                process.currentDirectoryURL = URL(fileURLWithPath: Place.workDir)
                var env = ProcessInfo.processInfo.environment.merging(Place.env) { $1 }
                env["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
                process.environment = env
                let out = Pipe(), err = Pipe(), input = Pipe()
                process.standardOutput = out
                process.standardError = err
                process.standardInput = stdin == nil ? FileHandle.nullDevice : input
                do {
                    try process.run()
                } catch {
                    continuation.resume(returning: .failure("Couldn't run \(Place.python): \(error.localizedDescription)"))
                    return
                }
                if let stdin {
                    input.fileHandleForWriting.write(Data(stdin.utf8))
                    try? input.fileHandleForWriting.close()
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

/// Something that went wrong, said plainly, with what to do about it: never a silent blank.
struct Trouble: View {
    let message: String
    let retry: () async -> Void
    @State private var trying = false

    /// macOS refused access to a folder: for an app built from the code, the Downloads folder it runs from.
    static func isPermission(_ message: String) -> Bool {
        ["Operation not permitted", "PermissionError", "Permission denied"].contains { message.contains($0) }
    }

    private static let privacy = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders")!

    var body: some View {
        let permission = Self.isPermission(message)
        VStack(alignment: .leading, spacing: 8) {
            Label(permission ? "macOS stopped Role Radar from reading its files" : "Role Radar hit a problem",
                  systemImage: "exclamationmark.triangle.fill")
                .scaledFont(12, weight: .semibold).foregroundStyle(.orange)
            if permission {
                Text("Open System Settings → Privacy & Security → Files and Folders, and turn on the folders listed "
                     + "under Role Radar (for a copy built from the code, its Downloads Folder). Then try again.")
                    .scaledFont(11).fixedSize(horizontal: false, vertical: true)
            }
            Text(message).scaledFont(11, design: .monospaced).foregroundStyle(.secondary)
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                if permission {
                    Button("Open Privacy Settings") { NSWorkspace.shared.open(Self.privacy) }
                }
                Button(trying ? "Trying…" : "Try Again") {
                    Task { trying = true; await retry(); trying = false }
                }
                .disabled(trying)
            }
            .controlSize(.small)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.orange.opacity(0.12)))
    }
}

struct SwitchRow: View {
    @AppStorage(TextSize.key) private var scale = TextSize.standard
    let title: String
    let symbol: String
    let detail: String
    let isOn: Bool
    let active: Bool
    let busy: Bool
    var action: (title: String, run: () -> Void)? = nil
    var locked = false  // can't be switched (a channel that isn't set up)
    let toggle: (Bool) -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .scaledFont(13, weight: .semibold)
                .foregroundStyle(isOn ? Color.white : Color.secondary)
                .frame(width: 24 * scale, height: 24 * scale)
                .background(Circle().fill(isOn ? (active ? Color.green : Color.accentColor) : Color.secondary.opacity(0.18)))
            VStack(alignment: .leading, spacing: 1) {
                Text(title).scaledFont(13, weight: .medium)
                Text(detail).scaledFont(11).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            if busy {
                ProgressView().controlSize(.small)
            } else if let action {
                Button(action.title, action: action.run).scaledFont(11).controlSize(TextSize.controls(scale))
            }
            Toggle(title, isOn: Binding(get: { isOn }, set: toggle))
                .toggleStyle(.switch)
                .labelsHidden()
                .controlSize(TextSize.controls(scale))
                .disabled(busy || locked)
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
    @AppStorage(TextSize.key) private var textScale = TextSize.standard  // for the chart's axis labels
    let activity: RunnerState.Activity
    var lambda = true  // Lambda checks too: its series and legend
    @Environment(\.colorScheme) private var scheme
    @State private var hovered: Date?

    private var runners: [String] { lambda ? ["Mac", "Lambda"] : ["Mac"] }

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
                Text("Activity").scaledFont(12, weight: .semibold)
                Spacer()
                ForEach(runners, id: \.self) { runner in
                    HStack(spacing: 4) {
                        RoundedRectangle(cornerRadius: 2).fill(color(runner)).frame(width: 8, height: 8)
                        Text(runner).scaledFont(11).foregroundStyle(.secondary)
                    }
                }
            }
            chart.frame(height: 90)
                .overlay {
                    if activity.checked == 0 {
                        Text("No checks in the last 24 hours").scaledFont(11).foregroundStyle(.secondary)
                    }
                }
            Text(caption).scaledFont(11).foregroundStyle(.secondary).monospacedDigit()

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
                ForEach(runners, id: \.self) { runner in
                    BarMark(x: .value("Hour", hour.date, unit: .hour),
                            y: .value("Checks", runner == "Mac" ? hour.mac : hour.lambda), width: .ratio(0.72))
                        .foregroundStyle(by: .value("Runner", runner))
                        .opacity(hovered == nil || sameHour(hour.date, hovered!) ? 1 : 0.35)
                }
            }
        }
        .chartForegroundStyleScale(domain: runners, range: runners.map(color))
        .chartLegend(.hidden)
        .chartXAxis {
            AxisMarks(values: .stride(by: .hour, count: 6)) { _ in
                AxisTick(stroke: StrokeStyle(lineWidth: 1)).foregroundStyle(Color.secondary.opacity(0.4))
                AxisValueLabel(format: .dateTime.hour()).font(.system(size: 10 * textScale)).foregroundStyle(Color.secondary)
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading, values: .automatic(desiredCount: 2)) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 1)).foregroundStyle(Color.secondary.opacity(0.18))
                AxisValueLabel().font(.system(size: 10 * textScale)).foregroundStyle(Color.secondary)
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
            Text(value.formatted(.number.notation(.compactName))).scaledFont(15, weight: .semibold).monospacedDigit()
            Text(label).scaledFont(10).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct StatusLine: View {
    @AppStorage(TextSize.key) private var scale = TextSize.standard
    let text: String
    let symbol: String
    var tint: Color = .secondary

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: symbol).scaledFont(10).foregroundStyle(tint).frame(width: 14 * scale)
            Text(text).scaledFont(11).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
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
        let jobs = count == 0 ? "No new jobs" : "\(count) new job\(count == 1 ? "" : "s")"
        if alertsOff {
            return StatusLine(text: jobs + " in Live Tracking · alerts are off", symbol: "tray.full")
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
    @AppStorage(TextSize.key) private var scale = TextSize.standard
    @ObservedObject var model: Model
    @ObservedObject private var updates = Updates.shared
    @Environment(\.openWindow) private var openWindow
    @State private var openAtLogin = SMAppService.mainApp.status == .enabled

    private func ago(_ date: Date?) -> String { When.ago(date) }

    private var headline: (String, Color) {
        guard let s = model.state else { return model.error == nil ? ("Loading…", .secondary) : ("Can't read the status", .orange) }
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

    private var hasLambda: Bool { model.hasLambda }

    private func alertDetail(_ name: String) -> String {
        guard let s = model.state else { return "" }
        return s.switches[name] ? "New jobs are sent here" : "Off"
    }

    private func alertRow(_ title: String, _ name: String, symbol: String) -> SwitchRow {
        let ready = model.channelReady(name)
        let on = ready && model.state?.switches[name] ?? false
        return SwitchRow(title: title, symbol: symbol, detail: ready ? alertDetail(name) : "Not set up · add it in Settings…",
                         isOn: on, active: on, busy: model.busy.contains(name), locked: !ready && !on) { on in
            Task { await model.set(name, on: on) }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Role Radar").scaledFont(13, weight: .semibold)
                Spacer()
                Button {
                    Task { await model.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise").scaledFont(12)
                }
                .buttonStyle(.borderless)
                .help("Refresh")
            }
            Label(headline.0, systemImage: "circle.fill")
                .scaledFont(12)
                .foregroundStyle(headline.1)
                .labelStyle(DotLabel())

            VStack(spacing: 10) {
                SwitchRow(title: "Mac", symbol: "laptopcomputer", detail: macDetail,
                          isOn: model.state?.switches.laptop ?? false, active: model.state?.checking == "laptop",
                          busy: model.busy.contains("laptop"),
                          action: macNeedsStart ? ("Start", { Task { await model.set("laptop", on: true) } }) : nil
                ) { on in Task { await model.set("laptop", on: on) } }
                if hasLambda {
                    SwitchRow(title: "Lambda", symbol: "cloud", detail: lambdaDetail,
                              isOn: model.state?.switches.lambda ?? false, active: model.state?.checking == "lambda",
                              busy: model.busy.contains("lambda")) { on in Task { await model.set("lambda", on: on) } }
                }
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
            .disabled(model.state == nil)

            Text(hasLambda ? "Both on: the Mac checks while this app is open, Lambda covers when it isn't."
                           : "The Mac checks while this app is open. Everything stays on this Mac.")
                .scaledFont(11).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            Text("Alerts").scaledFont(12, weight: .semibold)
            VStack(spacing: 10) {
                alertRow("Discord", "discord", symbol: "bubble.left.and.bubble.right")
                alertRow("Email", "email", symbol: "envelope")
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
            .disabled(model.state == nil)

            Text(alertsOff
                 ? "Alerts are optional. Off, new jobs collect in Live Tracking, newest on top."
                 : "New jobs go to the alerts switched on, every 10 minutes.")
                .scaledFont(11).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if Place.packaged && !(model.setup?.ready ?? true) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Finish setting up: pick your profession, countries and the roles you want.")
                        .scaledFont(11).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    Button {
                        model.showingSetup = true
                        openWindow(id: MainWindow.id)
                        NSApp.activate()
                    } label: {
                        Label("Set Up Role Radar", systemImage: "wand.and.stars").scaledFont(13).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
            } else if model.state != nil {
                let health = Health(state: model.state, live: nil)
                VStack(alignment: .leading, spacing: 8) {
                    health.round
                    health.waiting
                    health.failing
                    Button {
                        model.showingSetup = false
                        openWindow(id: MainWindow.id)
                        NSApp.activate()
                    } label: {
                        Label("Live Tracking", systemImage: "list.bullet.rectangle.portrait")
                            .scaledFont(13).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .padding(.top, 2)
                    .help("Matches waiting to be sent, skipped and sent, the round in progress, and activity")
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
            }

            if let error = model.error ?? model.setupError {
                Trouble(message: error) { await model.retry() }
            }

            Divider()
            HStack {
                Toggle("Open at Login", isOn: Binding(get: { openAtLogin }, set: setOpenAtLogin))
                    .toggleStyle(.checkbox)
                    .fixedSize()
                Spacer(minLength: 8)
                if Place.packaged {
                    Button("Edit Setup…") {
                        model.showingSetup = true
                        openWindow(id: MainWindow.id)
                        NSApp.activate()
                    }
                    .help("Your profession, countries, roles, qualifications and alerts")
                }
                Button("Log") { NSWorkspace.shared.open(Place.log) }
                Button("Quit") { NSApp.terminate(nil) }
                    .help(hasLambda ? "Also stops the Mac's checker; Lambda takes over" : "Also stops the Mac's checker")
            }
            .scaledFont(12)
            .controlSize(TextSize.controls(scale))
            if updates.available {
                HStack {
                    Text("Version \(Place.version)").scaledFont(11).foregroundStyle(.secondary)
                    Spacer()
                    Button("Check for Updates…") { updates.check() }
                        .buttonStyle(.link).scaledFont(11)
                        .disabled(!updates.canCheck)
                }
            }
        }
        .padding(14)
        .frame(width: max(330, 290 * scale))  // wider as the text grows, so lines don't wrap into a column
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
/// A select box that's easy to hit, as in Gmail: a bigger box, a round highlight under the pointer.
/// `mixed`: some of what it stands for are selected (the box above a list).
struct SelectBox: View {
    @AppStorage(TextSize.key) private var scale = TextSize.standard
    @Binding var on: Bool
    var mixed = false
    @State private var hovering = false

    static func width(_ scale: Double) -> CGFloat { 30 * scale }

    var body: some View {
        Button { on.toggle() } label: {
            Image(systemName: mixed ? "minus.square.fill" : on ? "checkmark.square.fill" : "square")
                .font(.system(size: 16 * scale))
                .foregroundStyle(on || mixed ? Color.accentColor : Color.secondary)
                .frame(width: Self.width(scale), height: Self.width(scale))
                .background(Circle().fill(Color.primary.opacity(hovering ? 0.09 : 0)))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(.easeOut(duration: 0.1), value: hovering)
        .accessibilityLabel("Select")
        .accessibilityValue(on ? "selected" : "not selected")
    }
}

struct MatchRow: View {
    @AppStorage(TextSize.key) private var scale = TextSize.standard
    let title: String
    let company: String?
    let location: String?
    let url: String?
    let when: String
    var since: (label: String, at: String?)? = nil  // "Found 2:32 PM (12 min. ago)", its own part kept current
    var dimmed = false
    var action: (title: String, help: String, run: () -> Void)? = nil
    var busy = false
    var picked: Binding<Bool>? = nil  // a tick box, for acting on several at once
    var symbol: String? = nil  // shown where the tick box would be, e.g. while it's being sent
    var note: String? = nil  // e.g. "Sending…"
    var menu: [(title: String, run: () -> Void)] = []  // more for its right-click menu
    @State private var hovering = false

    private var link: URL? { url.flatMap(URL.init(string:)) }

    private func open() {
        if let link { NSWorkspace.shared.open(link) }
    }

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            if let picked {
                SelectBox(on: picked).padding(.leading, -6)
            } else if let symbol {
                Image(systemName: symbol).scaledFont(12).foregroundStyle(.secondary)
                    .frame(width: SelectBox.width(scale)).padding(.leading, -6)
            }
            Button(action: open) {
                HStack(alignment: .center, spacing: 8) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(title).scaledFont(13, weight: .medium).lineLimit(2)
                            .foregroundStyle(hovering ? Color.accentColor : Color.primary)
                        Text([company, location].compactMap { $0 }.joined(separator: " · "))
                            .scaledFont(11).foregroundStyle(.secondary).lineLimit(1)
                        if let since {
                            // Only this text is drawn again, each minute: not the row, so the cursor stays right.
                            TimelineView(.everyMinute) { _ in
                                Text(since.label + " " + When.stamp(since.at))
                                    .scaledFont(11).foregroundStyle(.tertiary).monospacedDigit()
                            }
                        } else if !when.isEmpty {
                            Text(when).scaledFont(11).foregroundStyle(.tertiary).monospacedDigit()
                        }
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "arrow.up.right")
                        .scaledFont(11, weight: .semibold).foregroundStyle(Color.accentColor)
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
            } else if let note {
                Text(note).scaledFont(11).foregroundStyle(.secondary)
            } else if let action {
                Button(action.title, action: action.run).controlSize(.small).frame(minWidth: 58).help(action.help)
            }
        }
        .opacity(dimmed ? 0.55 : 1)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.accentColor.opacity(picked?.wrappedValue == true ? 0.12 : 0))
            .padding(.horizontal, -8).padding(.vertical, -2))
        .contextMenu {
            if link != nil {
                Button("Open Posting", action: open)
                Button("Copy Link") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(url ?? "", forType: .string)
                }
            }
            if !menu.isEmpty {
                Divider()
                ForEach(Array(menu.enumerated()), id: \.offset) { _, item in Button(item.title, action: item.run) }
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
                (inside ? NSCursor.pointingHand : NSCursor.arrow).set()
            }
        }
    }
}

/// The plain arrow over a bar of controls, so a pointing hand from a row it borders can't linger there.
struct ArrowCursor: ViewModifier {
    func body(content: Content) -> some View {
        if #available(macOS 15, *) {
            content.pointerStyle(.default)
        } else {
            content.onHover { inside in if inside { NSCursor.arrow.set() } }
        }
    }
}

/// The app's one window: Setup's pages until it's done (or when asked for again), otherwise Live
/// Tracking, switching in place rather than opening another window.
struct MainWindow: View {
    static let id = "main"
    @ObservedObject var model: Model

    private var setupShown: Bool { Place.packaged && model.showingSetup }

    var body: some View {
        Group {
            if setupShown { SetupView(model: model) } else { LiveWindow(model: model) }
        }
        .navigationTitle(setupShown ? "Role Radar Setup" : "Live Tracking")
        .toolbar {
            if Place.packaged && !setupShown {
                ToolbarItem(placement: .navigation) {
                    Button { model.showingSetup = true } label: {
                        Label("Edit Setup", systemImage: "slider.horizontal.3").labelStyle(.titleAndIcon)
                    }
                    .help("Change your profession, countries, roles, qualifications or alerts")
                }
            } else if setupShown && model.setup?.ready ?? false {
                ToolbarItem(placement: .navigation) {
                    Button { model.showingSetup = false } label: {
                        Label("Live Tracking", systemImage: "list.bullet.rectangle.portrait").labelStyle(.titleAndIcon)
                    }
                    .help("Back to the new jobs; what you changed here is saved")
                }
            }
            ToolbarItem { TextSizeButtons() }
        }
    }
}

/// The bigger window: the round in progress, alerts, activity, and the stack of new jobs (newest on top),
/// those cleared from it, and the alerts sent.
struct LiveWindow: View {
    static let id = "live"
    @ObservedObject var model: Model

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
                Text("Now").scaledFont(12, weight: .semibold)
                health.round
                if let fraction = live?.round?.fraction {
                    ProgressView(value: fraction).controlSize(.small)
                }
                health.failing
            }
            card {
                Text("Alerts").scaledFont(12, weight: .semibold)
                health.waiting
                Text(alertsHelp).scaledFont(11).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let activity = model.state?.activity {
                card { ActivityView(activity: activity, lambda: model.hasLambda) }
            }
            if let error = model.liveError ?? model.error {
                Trouble(message: error) { await model.retry(); await model.refreshLive() }
            }
        }
    }

    private var alertsHelp: String {
        guard let live else { return "" }
        if live.send_requested {
            return model.state?.checking == "lambda" ? "Sending: Lambda sends them at its next run (within 5 minutes)."
                                                     : "Sending: the Mac sends them within a minute."
        }
        if !model.canAlert { return "No alerts set up: new jobs collect here. To send them, set up email or Discord in Edit Setup." }
        if live.alerts_off { return "Alerts are off: new jobs collect here until you send them or mark them as seen." }
        return "New jobs go out every 10 minutes. Send some sooner, or mark the ones you don't want as seen."
    }

    private func card<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8, content: content)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
    }

    // -- the lists ----------------------------------------------------------------

    /// A view of its own, drawn again only when its jobs change: the round's progress changes every
    /// few seconds, and drawing the rows again under a pointer that isn't moving leaves macOS showing
    /// the wrong cursor (an arrow on a row, a hand on a button) until it moves.
    @ViewBuilder private var lists: some View {
        if let live {
            JobList(model: model, waiting: live.waiting, skipped: live.skipped, sent: live.sent,
                    alertsOff: live.alerts_off, canAlert: model.canAlert, busy: model.liveBusy)
                .equatable()
        } else if let error = model.liveError {
            ContentUnavailableView("Couldn't load the matches", systemImage: "exclamationmark.triangle", description: Text(error))
        } else {
            ProgressView("Loading matches…").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

/// The jobs ticked in one of Live Tracking's lists. Each tick box reads it itself, so ticking one
/// redraws the boxes, not the rows around them.
@Observable final class Picks {
    var ids: Set<String> = []

    /// A job's tick box.
    func box(_ id: String) -> Binding<Bool> {
        Binding(get: { self.ids.contains(id) },
                set: { on in if on { self.ids.insert(id) } else { self.ids.remove(id) } })
    }
}

/// A new job's row. Equatable on what it shows, so ticking one job doesn't draw every row again
/// (with a couple of thousand new jobs, every tick used to redraw every row): its tick box follows
/// `picks` on its own.
struct NewJobRow: View, Equatable {
    let match: LiveState.Match
    let picks: Picks
    let busy: Bool
    let canAlert: Bool
    let menu: [(title: String, run: () -> Void)]

    nonisolated static func == (a: NewJobRow, b: NewJobRow) -> Bool {
        a.match == b.match && a.busy == b.busy && a.canAlert == b.canAlert  // `picks` is the list's, for good
    }

    var body: some View {
        let sending = match.send_at != nil
        MatchRow(title: match.title, company: match.company, location: match.location, url: match.url,
                 when: "", since: ("Found", match.first_seen), busy: busy,
                 picked: sending ? nil : picks.box(match.id), symbol: sending ? "paperplane" : nil,
                 note: sending ? "Sending…" : nil, menu: sending ? [] : menu)
    }
}

/// A seen job's row: ticked to go back with others, or put back on its own.
struct SeenJobRow: View, Equatable {
    let match: LiveState.Match
    let picks: Picks
    let busy: Bool
    let markNew: () -> Void

    nonisolated static func == (a: SeenJobRow, b: SeenJobRow) -> Bool {
        a.match == b.match && a.busy == b.busy
    }

    var body: some View {
        MatchRow(title: match.title, company: match.company, location: match.location, url: match.url,
                 when: "", since: ("Seen", match.skipped_at), dimmed: true,
                 action: ("Mark as New", "Put it back in New jobs", markNew), busy: busy, picked: picks.box(match.id))
    }
}

/// Live Tracking's jobs, with the bar that acts on them: New jobs (the stack), Seen (folded), and the
/// alerts sent. Equatable on what it shows, so it's drawn again only when that changes.
struct JobList: View, Equatable {
    let model: Model  // for its actions; not watched
    let waiting: [LiveState.Match]
    let skipped: [LiveState.Match]
    let sent: [LiveState.Alert]
    let alertsOff: Bool
    let canAlert: Bool
    let busy: Set<String>
    @State private var opened: Set<String> = []  // sent alerts unfolded: each starts folded
    @State private var picks = Picks()  // new jobs ticked, to mark as seen or send together
    @State private var showSeen = false  // the Seen section is folded until opened
    @State private var seenPicks = Picks()  // seen jobs ticked, to mark as new together
    @State private var seenShown = 100  // the Seen section lists this many, newest first

    nonisolated static func == (a: JobList, b: JobList) -> Bool {
        a.waiting == b.waiting && a.skipped == b.skipped && a.sent == b.sent && a.alertsOff == b.alertsOff
            && a.canAlert == b.canAlert && a.busy == b.busy
    }

    var body: some View {
        VStack(spacing: 0) {
            // Fixed above the list, as in Gmail: always in reach, and rows never scroll under it.
            stackHeader
                .padding(.leading, 16).padding(.trailing, 20).padding(.vertical, 10)  // its box in line with the rows
                .contentShape(Rectangle())
                .modifier(ArrowCursor())
            Divider()
            list
        }
    }

    private var list: some View {
        List {
            if alertsOff {
                Label(canAlert ? "Alerts are off: new jobs collect here, newest on top. Mark the ones "
                                       + "you've seen, or send some as alerts."
                                     : "New jobs collect here, newest on top. Mark the ones you've seen.",
                      systemImage: "tray.full")
                    .scaledFont(12).foregroundStyle(.secondary)
                    .padding(.vertical, 4)
            }
            Section {
                if waiting.isEmpty {
                    Text("No new jobs. They appear here as soon as their company is checked.")
                        .scaledFont(12).foregroundStyle(.secondary).padding(.vertical, 4)
                }
                ForEach(waiting) { match in
                    NewJobRow(match: match, picks: picks, busy: busy.contains(match.id), canAlert: canAlert,
                              menu: rowMenu(match))
                        .equatable()
                }
            }
            Section {
                if showSeen {
                    if skipped.isEmpty {
                        Text("Jobs you mark as seen go here for a week, and are never sent. Mark one as new to "
                             + "put it back.")
                            .scaledFont(12).foregroundStyle(.secondary).padding(.vertical, 4)
                    }
                    ForEach(skipped.prefix(seenShown)) { match in
                        SeenJobRow(match: match, picks: seenPicks, busy: busy.contains(match.id),
                                   markNew: { Task { await model.skip(match, false) } })
                            .equatable()
                            .listRowBackground(Color.primary.opacity(0.04))
                    }
                    if skipped.count > seenShown {
                        Button("Show \(min(100, skipped.count - seenShown)) More") { seenShown += 100 }
                            .controlSize(.small).padding(.vertical, 4)
                    }
                }
            } header: {
                seenHeader
            }
            Section {
                if sent.isEmpty {
                    Text("No alerts sent lately.").scaledFont(12).foregroundStyle(.secondary).padding(.vertical, 4)
                }
                ForEach(sent) { alert in
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
                header("Sent alerts", sent.count) { EmptyView() }
            }
        }
        .listStyle(.inset)
    }

    /// Seen jobs' header: unfold them; once unfolded, a box to select those shown, and Mark as New for
    /// the selected ones.
    private var seenHeader: some View {
        let shown = skipped.prefix(seenShown)
        let chosen = shown.filter { seenPicks.ids.contains($0.id) }
        let all = Binding(get: { !shown.isEmpty && chosen.count == shown.count },
                          set: { on in seenPicks.ids = on ? Set(shown.map(\.id)) : [] })
        return HStack(spacing: 6) {
            if showSeen && !shown.isEmpty {
                SelectBox(on: all, mixed: !chosen.isEmpty && chosen.count < shown.count)
                    .padding(.leading, -6)
                    .help(chosen.isEmpty ? "Select all" : "Deselect all")
            }
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { showSeen.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right").scaledFont(10, weight: .semibold).foregroundStyle(.secondary)
                        .rotationEffect(.degrees(showSeen ? 90 : 0))
                    Text("Seen").scaledFont(12, weight: .semibold)
                    Text(showSeen && !chosen.isEmpty ? "\(chosen.count) selected" : skipped.count.formatted())
                        .scaledFont(11).foregroundStyle(.secondary).monospacedDigit()
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(showSeen ? "Hide the jobs marked as seen" : "Show the jobs marked as seen")
            if showSeen && !chosen.isEmpty {
                Button("Mark as New") {
                    seenPicks.ids = []
                    Task { await model.markNew(chosen) }
                }
                .controlSize(.small)
                .disabled(busy.contains("new"))
                .help("Put them back in New jobs")
            }
        }
    }

    /// New jobs' header: a box to select them all, and what to do with the selected ones (or all of them).
    private var stackHeader: some View {
        let open = waiting.filter { $0.send_at == nil }
        let chosen = open.filter { picks.ids.contains($0.id) }
        let targets = chosen.isEmpty ? nil : chosen
        let busy = busy.contains("seen") || busy.contains("send")
        let all = Binding(get: { !open.isEmpty && chosen.count == open.count },
                          set: { on in picks.ids = on ? Set(open.map(\.id)) : [] })
        return HStack(spacing: 8) {
            if !open.isEmpty {
                SelectBox(on: all, mixed: !chosen.isEmpty && chosen.count < open.count)
                    .padding(.leading, -6)  // in line with the jobs' boxes
                    .help(chosen.isEmpty ? "Select all" : "Deselect all")
            }
            Text("New jobs").scaledFont(12, weight: .semibold)
            Text(chosen.isEmpty ? waiting.count.formatted() : "\(chosen.count) selected")
                .scaledFont(11).foregroundStyle(.secondary).monospacedDigit()
            Spacer()
            Group {
                Button(chosen.isEmpty ? "Mark All as Seen" : "Mark as Seen") {
                    picks.ids = []
                    Task { await model.markSeen(targets) }
                }
                .disabled(open.isEmpty || busy)
                .help("Take them off the stack. Jobs marked as seen are never sent.")
                Button(chosen.isEmpty ? "Send All as Alert" : "Send as Alert") {
                    picks.ids = []
                    Task { await model.send(targets) }
                }
                .disabled(open.isEmpty || busy || !canAlert)
                .help(canAlert ? "Send them by email or Discord now; once sent, they leave the stack."
                                     : "Set up email or Discord in Edit Setup to send jobs.")
            }
            .controlSize(.small)
        }
    }

    private func rowMenu(_ match: LiveState.Match) -> [(title: String, run: () -> Void)] {
        var items: [(title: String, run: () -> Void)] = [("Mark as Seen", { Task { await model.markSeen([match]) } })]
        if canAlert { items.append(("Send as Alert", { Task { await model.send([match]) } })) }
        return items
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
            Image(systemName: "paperplane.fill").scaledFont(11).foregroundStyle(.secondary)
            Text(sent.map(When.full) ?? alert.sent_at).scaledFont(13, weight: .semibold).monospacedDigit()
            TimelineView(.everyMinute) { _ in
                Text([jobs, by.map { "sent by \($0)" }, sent.map { When.ago($0) }].compactMap { $0 }.joined(separator: " · "))
                    .scaledFont(11).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 3)
    }

    private func header<Trailing: View>(_ title: String, _ count: Int, @ViewBuilder trailing: () -> Trailing) -> some View {
        HStack {
            Text(title).scaledFont(12, weight: .semibold)
            Text(count.formatted()).scaledFont(11).foregroundStyle(.secondary).monospacedDigit()
            Spacer()
            trailing()
        }
    }
}

struct DotLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 6) {
            configuration.icon.scaledFont(7)
            configuration.title.foregroundStyle(.primary)
        }
    }
}

/// The packaged app's Setup, in three pages: the person's profession (which brings its company list
/// and job titles), the titles, places and experience they want, and the Gmail account alerts use.
struct SetupView: View {
    static let id = "setup"
    @ObservedObject var model: Model

    @State private var page = 0  // 0 profession, 1 titles and places, 2 email alerts
    @State private var picked: Set<String> = []  // ticked titles
    @State private var ownTitles: [String] = []  // titles they added, beyond the profession's
    @State private var newTitle = ""
    @State private var adding = false  // the "add a title" field is showing
    @FocusState private var titleFocused: Bool
    @State private var skipped: Set<String> = []  // ticked non-target words
    @State private var ownSkips: [String] = []  // non-target words they added
    @State private var newSkip = ""
    @State private var addingSkip = false
    @FocusState private var skipFocused: Bool
    @State private var countries: Set<String> = []
    @State private var cities = ""
    @State private var education = "bachelors"
    @State private var checkYears = true
    @State private var skipFrom = 3  // jobs asking for this many years or more are skipped
    @State private var address = ""
    @State private var password = ""
    @State private var also = ""
    @State private var webhook = ""
    @State private var busy: String?  // the action working right now
    @State private var choosing: String?  // the profession being saved, shown as picked meanwhile
    @State private var notes: [String: (text: String, ok: Bool)] = [:]  // each action's last result
    @State private var filled = false
    @State private var saved = ""  // the profile pages' answers as last saved, to tell when they've changed
    // The Companies page: search (empty: the ones they turned off), add their own, ask for one.
    @State private var query = ""
    @State private var found: CompanySearch?
    @State private var shown = 50  // how many results to list
    @State private var searching: Task<Void, Never>?
    @State private var turning: Set<String> = []  // companies being turned on or off
    @State private var newCompany = ""
    @State private var newCareers = ""

    private var setup: SetupState? { model.setup }
    private var profession: SetupState.Profession? { setup?.professions?.first { $0.id == setup?.profession } }
    private static let pages = ["Profession", "Countries", "Companies", "Roles", "Qualifications", "Alerts"]
    private static let degrees = [("none", "No degree yet"), ("bachelors", "Bachelor's"), ("masters", "Master's"), ("phd", "PhD")]
    private static let last = pages.count - 1
    private static let symbols = ["tech": "laptopcomputer", "accounting": "chart.bar.doc.horizontal", "healthcare": "stethoscope"]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 12) {
                if Place.translocated {
                    Label("Move Role Radar to your Applications folder first (drag it from Downloads onto Applications "
                          + "in Finder), then open it from there.", systemImage: "exclamationmark.triangle.fill")
                        .scaledFont(13, weight: .medium).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("Set Up Role Radar").scaledFont(20, weight: .semibold)
                pageMarkers
            }
            .padding([.horizontal, .top], 24).padding(.bottom, 14)
            Divider()
            ScrollViewReader { scroll in
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        Color.clear.frame(height: 0).id("top")
                        if let error = model.setupError {
                            Trouble(message: error) {
                                await model.loadSetup()
                                await load()
                            }
                            .padding([.horizontal, .top], 24)
                        }
                        Group {
                            if setup == nil {
                                // Never a blank page: loading, or (above) why it couldn't load.
                                if model.setupError == nil {
                                    ProgressView("Loading…").frame(maxWidth: .infinity).padding(.top, 60)
                                }
                            } else {
                                switch page {
                                case 0: professionPage
                                case 1: countriesPage
                                case 2: companiesPage
                                case 3: rolesPage
                                case 4: qualificationsPage
                                default: alertsPage
                                }
                            }
                        }
                        .padding(24)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)  // the page uses the window's width
                }
                // Each page opens at its top with no field focused: macOS would otherwise put the cursor
                // in a text field as the window becomes key, which on the titles page scrolls to the bottom.
                .onChange(of: page) { _, _ in toTop(scroll) }
                .onChange(of: filled) { _, _ in toTop(scroll, settle: [0, 0.3, 0.8]) }
            }
            Divider()
            footer.padding(.horizontal, 24).padding(.vertical, 14)
        }
        .frame(minWidth: 640, minHeight: 600)
        .task { await load() }
        .onDisappear {
            // Closing the window keeps what was changed, as leaving the page does.
            guard filled, fingerprint != saved else { return }
            let data = json(profileData())
            Task { _ = await model.setupStep(["profile"], stdin: data) }
        }
    }

    private func toTop(_ scroll: ScrollViewProxy, settle: [Double] = [0, 0.3]) {
        for delay in settle {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                NSApp.keyWindow?.makeFirstResponder(nil)
                scroll.scrollTo("top", anchor: .top)
            }
        }
    }

    private var pageMarkers: some View {
        HStack(spacing: 8) {
            ForEach(Array(Self.pages.enumerated()), id: \.offset) { index, name in
                let saved = !(setup?.countries ?? []).isEmpty
                let done = [setup?.profession != nil, saved, saved, saved && !(setup?.roles.isEmpty ?? true),
                            setup?.education != nil, alertsReady][index]
                Button { Task { await go(to: index) } } label: {
                    HStack(spacing: 5) {
                        Image(systemName: done ? "checkmark.circle.fill" : "\(index + 1).circle")
                            .foregroundStyle(done ? Color.green : index == page ? Color.accentColor : Color.secondary)
                        Text(name).fontWeight(index == page ? .semibold : .regular)
                            .foregroundStyle(index == page ? Color.primary : Color.secondary)
                    }
                    .scaledFont(12)
                }
                .buttonStyle(.plain)
                .focusable(false)  // Back and Next move between pages from the keyboard
                .disabled(busy != nil || (index > 0 && setup?.profession == nil))
                if index < Self.pages.count - 1 {
                    Image(systemName: "chevron.right").scaledFont(9).foregroundStyle(.tertiary)
                }
            }
        }
    }

    // -- page 1: profession -------------------------------------------------------------

    private var professionPage: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("What's your profession?").scaledFont(15, weight: .semibold)
                Text("Role Radar watches the job boards of companies that hire in your profession, and emails you "
                     + "new jobs that match within minutes of them being posted.")
                    .scaledFont(12).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            HStack(alignment: .top, spacing: 12) {
                ForEach(setup?.professions ?? []) { option in professionCard(option) }
            }
            .fixedSize(horizontal: false, vertical: true)  // the cards share the tallest one's height
            if let note = notes["profession"], !note.ok {
                Label(note.text, systemImage: "exclamationmark.triangle").scaledFont(11).foregroundStyle(.red)
            } else if let profession, choosing == nil {
                Label("\(profession.name): \(profession.groups.reduce(0) { $0 + $1.titles.count }) job titles to choose "
                      + "from, and its companies. Next, pick your countries and titles.", systemImage: "checkmark.circle.fill")
                    .scaledFont(12).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func professionCard(_ option: SetupState.Profession) -> some View {
        ProfessionCard(option: option, symbol: Self.symbols[option.id] ?? "briefcase",
                       chosen: (choosing ?? setup?.profession) == option.id,
                       saving: choosing == option.id, disabled: busy != nil || choosing != nil) {
            Task { await pick(option.id) }
        }
    }

    // -- page 2: countries ------------------------------------------------------------------

    private var pickedKey: String { (setup?.country_options ?? []).map(\.code).filter(countries.contains).joined(separator: "+") }
    private var trackedCount: Int? { countries.isEmpty ? nil : setup?.companies_for?[pickedKey] }
    private var countryNames: String {
        let names = (setup?.country_options ?? []).filter { countries.contains($0.code) }.map(\.name)
        return names.count <= 1 ? names.first ?? "" : names.dropLast().joined(separator: ", ") + " and " + names.last!
    }

    private var countriesPage: some View {
        VStack(alignment: .leading, spacing: 20) {
            section("Where do you want to work?", "Role Radar tracks the companies that post jobs in the countries you pick.") {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(setup?.country_options ?? [], id: \.code) { country in
                        Toggle(isOn: Binding(get: { countries.contains(country.code) },
                                             set: { on in if on { countries.insert(country.code) } else { countries.remove(country.code) } })) {
                            HStack(spacing: 8) {
                                Text(country.name).scaledFont(14)
                                if let n = setup?.companies_by_country?[country.code], n > 0 {
                                    Text("\(n) companies").scaledFont(12).foregroundStyle(.secondary)
                                }
                            }
                        }
                        .toggleStyle(.checkbox)
                    }
                }
                if let trackedCount {
                    Label("\(trackedCount) companies post jobs in \(countryNames).", systemImage: "building.2")
                        .scaledFont(13, weight: .medium).foregroundStyle(trackedCount == 0 ? Color.orange : Color.primary)
                }
            }
            section("Cities (optional)", "Alerts only for jobs in these cities, e.g. Bengaluru, Hyderabad. With none, jobs "
                    + "anywhere in your countries alert you. Jobs listed only as \"Remote\" always do.") {
                TextField("Cities, separated by commas", text: $cities).textFieldStyle(.roundedBorder).frame(maxWidth: 460)
            }
        }
    }

    // -- page 3: the companies tracked ------------------------------------------------------

    private var companiesPage: some View {
        let count = trackedCount ?? setup?.companies ?? 0
        return VStack(alignment: .leading, spacing: 18) {
            if count == 0 {
                section("No \(profession?.name ?? "") companies yet", "Role Radar doesn't have a list of \(profession?.name ?? "") "
                        + "employers yet. A later version adds them, and checking starts then.") { EmptyView() }
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("\(count) companies will be tracked").scaledFont(22, weight: .semibold)
                    Text("Role Radar checks every one of their job boards from this Mac while it's on: most every 20 "
                         + "minutes, Workday boards every 6 hours. You'll hear about new jobs that match your roles "
                         + "within minutes of them being posted.")
                        .scaledFont(13).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                section("By country", nil) {
                    ForEach((setup?.country_options ?? []).filter { countries.contains($0.code) }, id: \.code) { country in
                        HStack {
                            Text(country.name).scaledFont(13)
                            Spacer().frame(width: 16)
                            Text("\(setup?.companies_by_country?[country.code] ?? 0) companies post jobs here")
                                .scaledFont(13).foregroundStyle(.secondary)
                        }
                    }
                    if let untagged = setup?.companies_untagged, untagged > 0 {
                        Text("Also \(untagged) whose job listings don't name a country, so they're tracked for every country.")
                            .scaledFont(12).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
                findSection
            }
            addSection
        }
        .task { await search() }
        .onChange(of: query) { _, _ in
            searching?.cancel()
            shown = 50
            searching = Task {
                try? await Task.sleep(nanoseconds: 300_000_000)  // once they pause typing
                if !Task.isCancelled { await search() }
            }
        }
    }

    /// Search the list; untick one to stop tracking it. With nothing typed, the ones turned off.
    private var findSection: some View {
        section("Find a company", "Untick a company to stop tracking it; tick it again to bring it back.") {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Search companies", text: $query).textFieldStyle(.roundedBorder).frame(maxWidth: 320)
            }
            if let found {
                if query.isEmpty {
                    if !found.results.isEmpty {
                        Text("Turned off (\(found.total))").scaledFont(11, weight: .medium).foregroundStyle(.secondary)
                    }
                } else if found.total == 0 {
                    Text("No company on the list matches. Add it below, or ask for it.")
                        .scaledFont(12).foregroundStyle(.secondary)
                } else {
                    Text(found.total > found.results.count ? "\(found.results.count) of \(found.total)" : "\(found.total) found")
                        .scaledFont(11).foregroundStyle(.secondary)
                }
                ForEach(found.results) { company in companyRow(company) }
                if found.total > found.results.count {
                    Button("Show More") { shown += 50; Task { await search() } }.controlSize(.small)
                }
            }
            if let note = notes["find"] {
                Label(note.text, systemImage: "exclamationmark.triangle").scaledFont(11).foregroundStyle(.red)
            }
        }
    }

    private func companyRow(_ company: CompanySearch.Company) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Toggle("", isOn: Binding(get: { !company.off }, set: { on in Task { await setTracked(company, on) } }))
                .toggleStyle(.checkbox).labelsHidden()
                .disabled(!company.readable || turning.contains(company.name))
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(company.name).scaledFont(13)
                    if company.own { Text("Yours").scaledFont(10, weight: .medium).foregroundStyle(.secondary) }
                }
                Text(Self.about(company)).scaledFont(11).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// "Workday · US, CA", and why it isn't tracked unless they turned it off themselves.
    private static func about(_ company: CompanySearch.Company) -> String {
        var parts: [String] = []
        if let site = company.site, site != "generic" {
            parts.append(siteNames[site] ?? site.capitalized)
        }
        if !company.countries.isEmpty { parts.append(company.countries.joined(separator: ", ")) }
        if let why = company.why, !company.off { parts.append("not tracked: \(why)") }
        return parts.joined(separator: " · ")
    }

    private static let siteNames = ["oracle_hcm": "Oracle", "smartrecruiters": "SmartRecruiters", "icims": "iCIMS",
                                    "bamboohr": "BambooHR", "hrmdirect": "HRM Direct", "tiktok": "TikTok"]

    /// A company they want: tracked at once if Role Radar can read its job site, and either way suggested
    /// for everyone's list (the maintainer's suggestions box), unless it's listed already.
    private var addSection: some View {
        section("Add a company you want", "Not on the list? Give its name, and its careers page if you know it. "
                + "If Role Radar can read its job site, it's tracked right away; if not, we'll work on it.") {
            TextField("Company name", text: $newCompany).textFieldStyle(.roundedBorder).frame(maxWidth: 320)
            TextField("Careers page (optional), https://…", text: $newCareers).textFieldStyle(.roundedBorder)
                .frame(maxWidth: 420)
            actionRow("add", "Add", disabled: newCompany.trimmingCharacters(in: .whitespaces).isEmpty) { await addCompany() }
        }
    }

    // -- page 4: roles ------------------------------------------------------------------------

    private var rolesPage: some View {
        VStack(alignment: .leading, spacing: 28) {
            section("Target roles", "A job alerts you when its title contains one of the ticked titles. "
                    + "Untick any you don't want.") {
                HStack(spacing: 8) {
                    Button("Tick All") { picked = Set(offered + ownTitles) }.controlSize(.small)
                    Button("Untick All") { picked = [] }.controlSize(.small)
                    Text("\(picked.count) ticked").scaledFont(11).foregroundStyle(.secondary)
                }
                ForEach(profession?.groups ?? [], id: \.name) { group in
                    chips(group.name, group.titles, $picked)
                }
                if !ownTitles.isEmpty { chips("Your own", ownTitles, $picked) }
                adder("A title of your own", "Add a Title…", text: $newTitle, open: $adding, focus: $titleFocused) {
                    add(&newTitle, offered: offered, own: &ownTitles, to: &picked)
                }
            }
            section("Non-target roles", "A job whose title contains a ticked word never alerts you, even when it "
                    + "matches a target role. Untick any you want, such as Senior or Lead if you have the experience.") {
                ForEach(profession?.skip_groups ?? [], id: \.name) { group in
                    chips(group.name, group.titles, $skipped)
                }
                if !ownSkips.isEmpty { chips("Your own", ownSkips, $skipped) }
                adder("A word of your own", "Add a Word…", text: $newSkip, open: $addingSkip, focus: $skipFocused) {
                    add(&newSkip, offered: offeredSkips, own: &ownSkips, to: &skipped)
                }
            }
        }
    }

    // -- page 5: qualifications ------------------------------------------------------------------

    private var qualificationsPage: some View {
        VStack(alignment: .leading, spacing: 24) {
            section("Experience", "Role Radar reads each new match's description once, and skips jobs asking for "
                    + "more experience than you have.") {
                HStack(spacing: 10) {
                    Toggle("Skip jobs asking for", isOn: $checkYears).toggleStyle(.checkbox)
                    Stepper("\(skipFrom)+ years of experience", value: $skipFrom, in: 1...20)
                        .disabled(!checkYears)
                }
                .scaledFont(13)
                Text(checkYears ? "You'll still hear about jobs asking for \(Self.stillAlert(skipFrom)), and jobs "
                     + "that don't say." : "Jobs alert you whatever experience they ask for.")
                    .scaledFont(12).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            section("Education", "Your highest degree, or the one you'll have when you start. Jobs that need a "
                    + "higher degree are skipped.") {
                Picker("Education", selection: $education) {
                    ForEach(Self.degrees, id: \.0) { value, label in Text(label).tag(value) }
                }
                .pickerStyle(.radioGroup)
                .labelsHidden()
                .scaledFont(13)
                Text("A degree can also count in place of experience: if you skip 3+ years and have a Master's, "
                     + "a job asking for \"3 years, or 1 year with a Master's\" is still shown.")
                    .scaledFont(12).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// What someone skipping jobs that ask for `from`+ years still hears about.
    private static func stillAlert(_ from: Int) -> String {
        switch from {
        case 1: return "no experience"
        case 2: return "no experience or 1+ year"
        case 3: return "no experience, 1+ or 2+ years"
        default: return "anything from no experience up to \(from - 1)+ years"
        }
    }

    private var offered: [String] { profession?.groups.flatMap(\.titles) ?? [] }
    private var offeredSkips: [String] { profession?.skip_groups?.flatMap(\.titles) ?? [] }

    private func chips(_ name: String, _ titles: [String], _ ticked: Binding<Set<String>>) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(name).scaledFont(11, weight: .medium).foregroundStyle(.secondary)
            FlowLayout(spacing: 6) {
                ForEach(titles, id: \.self) { title in
                    Chip(text: title, on: ticked.wrappedValue.contains(title)) {
                        if ticked.wrappedValue.contains(title) {
                            ticked.wrappedValue.remove(title)
                        } else {
                            ticked.wrappedValue.insert(title)
                        }
                    }
                }
            }
        }
    }

    /// A button that opens a field for adding one of their own: a field only once asked for, so opening
    /// the page never focuses (and scrolls to) it.
    private func adder(_ placeholder: String, _ button: String, text: Binding<String>, open: Binding<Bool>,
                       focus: FocusState<Bool>.Binding, add: @escaping () -> Void) -> some View {
        Group {
            if open.wrappedValue {
                HStack(spacing: 8) {
                    TextField(placeholder, text: text).textFieldStyle(.roundedBorder).frame(maxWidth: 280)
                        .focused(focus)
                        .onSubmit(add)
                    Button("Add", action: add).disabled(text.wrappedValue.trimmingCharacters(in: .whitespaces).isEmpty)
                    Button("Done") { open.wrappedValue = false; text.wrappedValue = "" }
                }
            } else {
                Button(button) {
                    open.wrappedValue = true
                    DispatchQueue.main.async { focus.wrappedValue = true }
                }
                .controlSize(.small)
            }
        }
    }

    /// Tick what was typed: the box already offered under any capitalization, or a new one of their own.
    private func add(_ text: inout String, offered: [String], own: inout [String], to ticked: inout Set<String>) {
        let word = text.trimmingCharacters(in: .whitespaces)
        guard !word.isEmpty else { return }
        if let known = (offered + own).first(where: { $0.caseInsensitiveCompare(word) == .orderedSame }) {
            ticked.insert(known)
        } else {
            own.append(word)
            ticked.insert(word)
        }
        text = ""
    }

    // -- page 6: alerts -------------------------------------------------------------------

    /// At least one way to send alerts is set up.
    private var alertsReady: Bool { (setup?.email_ready ?? false) || (setup?.discord_ready ?? false) }

    private var alertsPage: some View {
        VStack(alignment: .leading, spacing: 28) {
            Text("Alerts are optional. New jobs always collect in Live Tracking, newest on top, so you can just open "
                 + "the app to see them. Set up email, Discord or both to also get them sent every 10 minutes.")
                .scaledFont(13).fixedSize(horizontal: false, vertical: true)
            section("Email (Gmail)", "Alerts come from your own Gmail, sent to yourself and anyone you add. Gmail needs "
                    + "an app password for this: a 16-letter password just for Role Radar. Creating one needs 2-Step "
                    + "Verification on your Google account.") {
                Link("Create an app password ↗", destination: URL(string: "https://myaccount.google.com/apppasswords")!)
                    .scaledFont(12)
                TextField("you@gmail.com", text: $address).textFieldStyle(.roundedBorder).frame(maxWidth: 320)
                SecureField("App password (16 letters)", text: $password).textFieldStyle(.roundedBorder).frame(maxWidth: 320)
                actionRow("email", "Save", disabled: address.isEmpty || password.isEmpty) { await saveEmail() }
                if let saved = setup?.email, setup?.email_ready ?? false {
                    field("Also send alerts to (a friend, your school email), one per line. Each person sees only "
                          + "your address.", text: $also, placeholder: "friend@example.com", height: 56)
                    HStack(spacing: 12) {
                        actionRow("also", "Save List") { await saveAlso() }
                        actionRow("test-email", "Send Test Email") { await sendTest("email") }
                    }
                    let others = setup?.also.count ?? 0
                    Text("Alerts go to \(saved)" + (others == 0 ? "." : " and \(others) other\(others == 1 ? "" : "s")."))
                        .scaledFont(11).foregroundStyle(.secondary)
                }
            }
            section("Discord", "Alerts go to a channel in your Discord server. In Discord, open the channel's "
                    + "settings, then Integrations → Webhooks → New Webhook → Copy Webhook URL, and paste it here.") {
                SecureField(setup?.discord_ready ?? false ? "Saved. Paste a new one to change it."
                            : "https://discord.com/api/webhooks/...", text: $webhook)
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 420)
                HStack(spacing: 12) {
                    actionRow("discord", "Save", disabled: webhook.trimmingCharacters(in: .whitespaces).isEmpty) {
                        await saveDiscord()
                    }
                    if setup?.discord_ready ?? false {
                        actionRow("test-discord", "Send Test Message") { await sendTest("discord") }
                    }
                }
            }
        }
    }

    // -- footer ----------------------------------------------------------------------

    private var footer: some View {
        HStack(alignment: .center, spacing: 10) {
            if page > 0 {
                Button("Back") { Task { await go(to: page - 1) } }.disabled(busy != nil)
            }
            if let note = notes["page"], !note.ok {
                Label(note.text, systemImage: "exclamationmark.triangle").scaledFont(11).foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            } else if page == Self.last {
                Text(setup?.ready ?? false
                     ? "All set. Role Radar checks while this Mac is on and the app is open, and it opens at login."
                     : "Pick your countries and roles to finish.")
                    .scaledFont(12).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            if busy == "page" { ProgressView().controlSize(.small) }
            if page < Self.last {
                Button("Next") { Task { await next() } }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy != nil || setup?.profession == nil || (page == 1 && countries.isEmpty)
                              || (page == 3 && picked.isEmpty))
            } else {
                Button(model.state?.checking == "laptop" ? "Done" : "Start Checking") {
                    Task {
                        await model.startChecking()
                        model.showingSetup = false  // the window turns into Live Tracking
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(!(setup?.ready ?? false) || Place.translocated)
            }
        }
    }

    // -- pieces ----------------------------------------------------------------------

    private func section<Content: View>(_ title: String, _ about: String?, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).scaledFont(15, weight: .semibold)
            if let about {
                Text(about).scaledFont(12).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            content()
        }
    }

    private func field(_ label: String, text: Binding<String>, placeholder: String = "", height: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).scaledFont(12).fixedSize(horizontal: false, vertical: true)
            TextEditor(text: text)
                .scaledFont(12, design: .monospaced)
                .scrollContentBackground(.hidden)
                .padding(6)
                .frame(height: height)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.3)))
                .overlay(alignment: .topLeading) {
                    if text.wrappedValue.isEmpty && !placeholder.isEmpty {
                        Text(placeholder).scaledFont(12, design: .monospaced).foregroundStyle(.tertiary)
                            .padding(.horizontal, 11).padding(.vertical, 6).allowsHitTesting(false)
                    }
                }
        }
    }

    private func actionRow(_ key: String, _ title: String, disabled: Bool = false,
                           action: @escaping () async -> Void) -> some View {
        HStack(spacing: 8) {
            Button(title) { Task { busy = key; await action(); busy = nil } }
                .disabled(disabled || busy != nil)
            if busy == key {
                ProgressView().controlSize(.small)
            } else if let note = notes[key] {
                Label(note.text, systemImage: note.ok ? "checkmark" : "exclamationmark.triangle")
                    .scaledFont(11).foregroundStyle(note.ok ? Color.green : Color.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private static func lines(_ text: String) -> [String] {
        text.split(whereSeparator: { $0 == "\n" || $0 == "," }).map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    private func json(_ object: [String: Any]) -> String {
        (try? JSONSerialization.data(withJSONObject: object, options: .sortedKeys)).map { String(decoding: $0, as: UTF8.self) } ?? "{}"
    }

    /// The countries, roles and qualifications pages' answers, for `setup profile`. Education goes once
    /// its page is open: until then the saved one (or none) stands.
    private func profileData() -> [String: Any] {
        var data: [String: Any] = [
            "roles": offered.filter(picked.contains) + ownTitles.filter(picked.contains),
            "exclude": offeredSkips.filter(skipped.contains) + ownSkips.filter(skipped.contains),
            "locations": Self.lines(cities), "countries": countries.sorted(),
            "max_experience_years": checkYears ? skipFrom - 1 as Any : NSNull(),
        ]
        if page == 4 { data["education"] = education }
        return data
    }

    /// The answers as they stand, whatever the page, to compare with `saved`.
    private var fingerprint: String {
        var data = profileData()
        data["education"] = education
        return json(data)
    }

    // -- actions ----------------------------------------------------------------------

    private func load() async {
        await model.loadSetup()
        guard !filled, let setup else { return }
        fillTitles()
        countries = Set(setup.countries ?? [])
        cities = (setup.cities ?? []).joined(separator: ", ")
        checkYears = setup.max_experience_years != nil || (setup.countries ?? []).isEmpty  // on until page 2 is saved
        skipFrom = (setup.max_experience_years ?? 2) + 1
        address = setup.email ?? ""
        also = setup.also.joined(separator: "\n")
        education = setup.education ?? "bachelors"
        saved = fingerprint
        // Open where there's something left to do.
        page = setup.profession == nil ? 0
            : (setup.countries ?? []).isEmpty ? 1
            : setup.roles.isEmpty ? 3
            : setup.education == nil ? 4
            : 0
        filled = true
    }

    /// The boxes from what's saved, target and non-target: the profession's ticked where saved, and their own after.
    private func fillTitles() {
        guard let setup else { return }
        (picked, ownTitles) = Self.boxes(setup.roles, offered: offered)
        (skipped, ownSkips) = Self.boxes(setup.exclude, offered: offeredSkips)
    }

    private static func boxes(_ saved: [String], offered: [String]) -> (ticked: Set<String>, own: [String]) {
        var ticked = Set<String>(), own: [String] = []
        for word in saved {
            if let known = offered.first(where: { $0.caseInsensitiveCompare(word) == .orderedSame }) {
                ticked.insert(known)
            } else {
                own.append(word)
                ticked.insert(word)
            }
        }
        return (ticked, own)
    }

    /// Show the choice straight away, save it, and stay on the page: Next moves on.
    private func pick(_ id: String) async {
        guard id != setup?.profession else { return }
        choosing = id
        let problem = await model.setupStep(["profession"], stdin: json(["profession": id]))
        choosing = nil
        notes["profession"] = problem.map { ($0, false) }
        if problem == nil {
            fillTitles()
            saved = fingerprint  // a new profession brings its own boxes, saved already
        }
    }

    private func next() async { await go(to: page + 1, always: true) }

    /// Move to another page. Leaving the countries, roles or qualifications page saves it first: always
    /// with Next, and with Back or a page's name whenever something changed, so no change is lost.
    private func go(to target: Int, always: Bool = false) async {
        notes["page"] = nil
        if fingerprint != saved || (always && [1, 3, 4].contains(page)) {
            busy = "page"
            let problem = await model.setupStep(["profile"], stdin: json(profileData()))
            busy = nil
            if let problem {
                notes["page"] = (problem, false)
                return
            }
            saved = fingerprint
        }
        page = target
    }

    private func saveEmail() async {
        let problem = await model.setupStep(["email"], stdin: json(["address": address, "password": password]))
        notes["email"] = problem.map { ($0, false) } ?? ("Saved in your Mac's Keychain", true)
        if problem == nil {
            password = ""
            await model.set("email", on: true)  // set up, so alerts go there
        }
    }

    private func saveDiscord() async {
        let problem = await model.setupStep(["discord"], stdin: json(["webhook": webhook]))
        notes["discord"] = problem.map { ($0, false) } ?? ("Saved in your Mac's Keychain", true)
        if problem == nil {
            webhook = ""
            await model.set("discord", on: true)
        }
    }

    private func saveAlso() async {
        let problem = await model.setupStep(["recipients"], stdin: json(["also": Self.lines(also)]))
        notes["also"] = problem.map { ($0, false) } ?? ("Saved", true)
    }

    /// The companies matching what's typed (nothing typed: the ones turned off).
    private func search() async {
        let typed = query
        let result = await model.setupCommand(["find"], stdin: json(["query": typed, "limit": shown,
                                                                     "which": typed.isEmpty ? "off" : "all"]))
        guard typed == query else { return }  // they've typed more since
        switch result {
        case .success(let data):
            found = try? JSONDecoder().decode(CompanySearch.self, from: data)
            notes["find"] = found == nil ? ("Unexpected reply from role-radar", false) : nil
        case .failure(let message):
            notes["find"] = (message, false)
        }
    }

    private func setTracked(_ company: CompanySearch.Company, _ on: Bool) async {
        turning.insert(company.name)
        let problem = await model.setupStep(["track"], stdin: json(["names": [company.name], "tracked": on]))
        turning.remove(company.name)
        notes["find"] = problem.map { ($0, false) }
        await search()
    }

    private func addCompany() async {
        let gaveCareers = !newCareers.trimmingCharacters(in: .whitespaces).isEmpty
        let result = await model.setupCommand(["add"], stdin: json(["name": newCompany, "url": newCareers]))
        switch result {
        case .success(let data):
            guard let added = try? JSONDecoder().decode(AddResult.self, from: data) else {
                notes["add"] = ("Unexpected reply from role-radar", false)
                return
            }
            switch added.status {
            case "added":
                let jobs = added.jobs.map { $0 == 1 ? " (1 job listed now)" : " (\($0) jobs listed now)" } ?? ""
                notes["add"] = ("Added: Role Radar now tracks \(added.name)\(jobs).", true)
                newCompany = ""
                newCareers = ""
            case "listed":
                if let why = added.why {
                    notes["add"] = ("\(added.name) is on the list, but isn't tracked: \(why).", false)
                } else {
                    notes["add"] = (added.turned_on == true ? "\(added.name) was turned off; it's tracked again."
                                    : "\(added.name) is tracked already.", true)
                }
            default:
                let hint = !gaveCareers && added.url == nil ? " If you know its careers page, add it and try again." : ""
                notes["add"] = ("Role Radar can't track \(added.name) yet. We've noted it and are working on it.\(hint)",
                                false)
            }
            await model.loadSetup()
            await search()
        case .failure(let message):
            notes["add"] = (message, false)
        }
    }

    private func sendTest(_ channel: String) async {
        let result = await Model.cli(args: ["-m", "role_radar", "notifications", "test", "--channel", channel,
                                            "--config", Place.config])
        if case .failure(let message) = result {
            notes["test-" + channel] = (message, false)
        } else {
            notes["test-" + channel] = (channel == "email" ? "Sent. Check your inbox." : "Sent. Check the channel.", true)
        }
    }
}

/// A profession on Setup's first page: it lights up under the pointer, dips when pressed, and
/// shows a tick (a spinner while saving) once picked.
struct ProfessionCard: View {
    let option: SetupState.Profession
    let symbol: String
    let chosen: Bool
    let saving: Bool
    let disabled: Bool
    let pick: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: pick) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Image(systemName: symbol).scaledFont(26)
                        .foregroundStyle(chosen || hovering ? Color.accentColor : Color.secondary)
                        .frame(height: 34, alignment: .bottomLeading)
                    Spacer()
                    if saving {
                        ProgressView().controlSize(.small)
                    } else if chosen {
                        Image(systemName: "checkmark.circle.fill").scaledFont(18).foregroundStyle(Color.accentColor)
                    }
                }
                Text(option.name).scaledFont(15, weight: .semibold)
                Text(option.about).scaledFont(12).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(16)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .contentShape(RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(CardPress(chosen: chosen, hovering: hovering))
        .disabled(disabled && !chosen)
        .onHover { hovering = $0 }
        .modifier(LinkCursor(active: !disabled))
        .animation(.easeOut(duration: 0.12), value: hovering)
        .animation(.easeOut(duration: 0.12), value: chosen)
    }
}

/// The card's look: picked (accent outline), under the pointer (raised), pressed (dipped).
struct CardPress: ButtonStyle {
    let chosen: Bool
    let hovering: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(RoundedRectangle(cornerRadius: 12).fill(
                chosen ? Color.accentColor.opacity(0.12)
                    : configuration.isPressed ? Color.primary.opacity(0.12)
                    : hovering ? Color.primary.opacity(0.08) : Color.primary.opacity(0.04)))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(
                chosen ? Color.accentColor : hovering ? Color.accentColor.opacity(0.5) : Color.secondary.opacity(0.25),
                lineWidth: chosen ? 2 : hovering ? 1.5 : 1))
            .shadow(color: .black.opacity(hovering && !configuration.isPressed ? 0.18 : 0), radius: 6, y: 2)
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

/// A job title box on Setup's second page: ticked, it's one of the titles searched for.
struct Chip: View {
    let text: String
    let on: Bool
    let toggle: () -> Void

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: 4) {
                if on { Image(systemName: "checkmark").scaledFont(9, weight: .bold) }
                Text(text)
            }
            .scaledFont(12)
            .padding(.horizontal, 9).padding(.vertical, 4)
            .foregroundStyle(on ? Color.white : Color.primary)
            .background(Capsule().fill(on ? Color.accentColor : Color.primary.opacity(0.05)))
            .overlay(Capsule().stroke(on ? Color.clear : Color.secondary.opacity(0.35)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

/// Lays its views out left to right, starting a new row when one would overflow.
struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, widest: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width {
                x = 0
                y += row + spacing
                row = 0
            }
            x += size.width + spacing
            row = max(row, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: proposal.width ?? widest, height: y + row)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, row: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX {
                x = bounds.minX
                y += row + spacing
                row = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            row = max(row, size.height)
        }
    }
}

/// The menu bar icon. It also opens Setup on a packaged app's first launch: the label is the
/// one view that exists from the start, and opening a window needs a view's environment.
struct MenuLabel: View {
    @ObservedObject var model: Model
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Image(systemName: model.menuSymbol)
            .accessibilityLabel(model.menuLabel)
            .onChange(of: model.wantsWindow) { _, wants in
                if wants {
                    openWindow(id: MainWindow.id)
                    NSApp.activate()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: Dock.openWindow)) { _ in
                if !(model.setup?.ready ?? false) { model.showingSetup = true }
                openWindow(id: MainWindow.id)
                NSApp.activate()
            }
    }
}

#if !PANEL_SNAPSHOT
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillFinishLaunching(_ notification: Notification) {
        TextSize.reset()  // every opening starts at the standard size
        let event = NSAppleEventManager.shared().currentAppleEvent
        Dock.launchedAtLogin = event?.eventID == kAEOpenApplication
            && event?.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
    }

    /// Opening the packaged app while it runs (Finder, Spotlight, its Dock icon) shows its window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if Place.packaged { NotificationCenter.default.post(name: Dock.openWindow, object: nil) }
        return false
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        MainActor.assumeIsolated { Updates.shared.start() }
    }

    /// Quit (or logging out, or an update) stops the Mac's checker with the app.
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
            MenuLabel(model: model)
        }
        .menuBarExtraStyle(.window)

        Window("Role Radar", id: MainWindow.id) {
            MainWindow(model: model)
                .onAppear(perform: Dock.windowOpened)
                .onDisappear(perform: Dock.windowClosed)
        }
        .defaultSize(width: 1000, height: 780)
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(after: .appInfo) { CheckForUpdates() }
            CommandGroup(after: .toolbar) {
                Button("Bigger Text") { TextSize.change(by: 0.1) }.keyboardShortcut("=")
                Button("Smaller Text") { TextSize.change(by: -0.1) }.keyboardShortcut("-")
                Button("Standard Text Size") { TextSize.reset() }.keyboardShortcut("0")
                Divider()
            }
        }
    }
}
#endif
