import ErrortaCompanionCore
import SwiftUI

/// F070 — drives the live connection to a paired desktop: load the Keychain
/// credential, verify over the pinned session, and fetch the read-only runs
/// list. Replaces the hardcoded "Disconnected" stub.
@MainActor
final class DesktopHomeModel: ObservableObject {
    enum Connection: Equatable {
        case connecting
        case connected
        case disconnected(String)
    }

    // F076 — mutable so we can fold in hosts the desktop learns us (Tailscale)
    // without re-pairing; the pinned cert is unchanged.
    @Published private(set) var desktop: DesktopRecord
    @Published var connection: Connection = .connecting
    @Published var runs: [MobileRunProjection] = []
    @Published var rooms: [MobileRoomProjection] = []
    @Published var runStatus: String = "active"
    @Published var loadingRuns = false
    @Published var loadingRooms = false

    private let keychain = KeychainCredentialStore()
    private let store = DesktopStore()

    init(desktop: DesktopRecord) {
        self.desktop = desktop
    }

    private func client() -> DesktopClient? {
        guard let cred = try? keychain.load(desktopId: desktop.desktopId) else { return nil }
        return DesktopClient.pinned(record: desktop, credential: cred)
    }

    func connect() async {
        connection = .connecting
        guard let client = client() else {
            connection = .disconnected("Not paired on this iPhone — pair again.")
            return
        }
        do {
            _ = try await client.verify()
            connection = .connected
            await learnHosts(using: client)  // F076 — pick up Tailscale etc.
            await loadRooms(using: client)
            await loadRuns(using: client)
        } catch {
            connection = .disconnected(Self.message(for: error))
        }
    }

    /// F076 — refresh our stored host list from the desktop so a later
    /// network (Tailscale, away from home) is reachable on the SAME pairing.
    private func learnHosts(using client: DesktopClient) async {
        guard let info = try? await client.connectionInfo() else { return }
        let learned = info.candidates()
        guard !learned.isEmpty else { return }
        func key(_ h: HostCandidate) -> String { "\(h.kind):\(h.host):\(h.port ?? 0)" }
        let before = Set(desktop.hostCandidates.map(key))
        let after = Set(learned.map(key))
        guard before != after else { return }
        let updated = DesktopRecord(
            desktopId: desktop.desktopId,
            displayName: desktop.displayName,
            hostCandidates: learned,
            tlsCertSha256: desktop.tlsCertSha256,  // keep the pinned cert
            lastSuccessfulHost: desktop.lastSuccessfulHost)
        desktop = updated
        store.upsert(updated)
    }

    func refresh() async { await connect() }

    @Published var starting = false
    @Published var startError: String?

    func setStatus(_ status: String) async {
        runStatus = status
        if case .connected = connection, let client = client() {
            await loadRuns(using: client)
        }
    }

    func reload() async {
        if let client = client() {
            await loadRooms(using: client)
            await loadRuns(using: client)
        }
    }

    /// Start a Council run from the phone. Returns true on success.
    @discardableResult
    func startRun(prompt: String, roomId: String?) async -> Bool {
        guard let client = client() else { return false }
        starting = true
        startError = nil
        defer { starting = false }
        do {
            try await client.startRun(prompt: prompt, roomId: roomId)
            runStatus = "active"
            await loadRuns(using: client)
            return true
        } catch DesktopError.forbidden {
            startError = "Grant “start runs” to this phone on the desktop."
            return false
        } catch {
            startError = "Couldn't start the run."
            return false
        }
    }

    private func loadRuns(using client: DesktopClient) async {
        loadingRuns = true
        defer { loadingRuns = false }
        do { runs = try await client.runs(status: runStatus) }
        catch { runs = [] }  // connection already verified; an empty list is fine
    }

    func loadRooms() async {
        guard let client = client() else { return }
        await loadRooms(using: client)
    }

    private func loadRooms(using client: DesktopClient) async {
        loadingRooms = true
        defer { loadingRooms = false }
        do { rooms = try await client.rooms() }
        catch { rooms = [] }
    }

    static func message(for error: Error) -> String {
        switch error {
        case DesktopError.unauthorized: return "Access expired — pair this iPhone again."
        case DesktopError.connectorDisabled: return "The desktop's mobile connector is off."
        case DesktopError.forbidden: return "This device isn't allowed that yet."
        case DesktopError.noReachableHost, DesktopError.network:
            return "Can't reach the desktop. Same Wi-Fi, app open?"
        default: return "Couldn't connect to the desktop."
        }
    }
}

struct ConnectionStateRow: View {
    @ObservedObject var model: DesktopHomeModel

    var body: some View {
        switch model.connection {
        case .connecting:
            Label("Connecting…", systemImage: "antenna.radiowaves.left.and.right")
                .foregroundStyle(.secondary)
        case .connected:
            Label("Connected", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .disconnected(let reason):
            VStack(alignment: .leading, spacing: 4) {
                Label("Disconnected", systemImage: "wifi.slash")
                    .foregroundStyle(.secondary)
                Text(reason).font(.caption).foregroundStyle(.secondary)
                Button("Retry") { Task { await model.connect() } }
                    .font(.caption)
            }
        }
        if let host = model.desktop.orderedHosts().first {
            Text("\(host.kind): \(host.host):\(host.port ?? 8770)")
                .font(.caption2).foregroundStyle(.tertiary)
        }
    }
}

struct RunListPane: View {
    @ObservedObject var model: DesktopHomeModel

    var body: some View {
        Section("Runs") {
            Picker("Status", selection: Binding(
                get: { model.runStatus },
                set: { newValue in Task { await model.setStatus(newValue) } }
            )) {
                Text("Active").tag("active")
                Text("Recent").tag("recent")
            }
            .pickerStyle(.segmented)

            if model.loadingRuns {
                HStack { ProgressView(); Text("Loading runs…").foregroundStyle(.secondary) }
            } else if model.runs.isEmpty {
                ContentUnavailableView(
                    "No Runs",
                    systemImage: "list.bullet.rectangle",
                    description: Text(connectedHint)
                )
            } else {
                ForEach(model.runs) { run in
                    NavigationLink {
                        RunDetailPane(desktop: model.desktop, run: run)
                    } label: {
                        VStack(alignment: .leading) {
                            Text(run.title)
                            Text(run.status).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    private var connectedHint: String {
        if case .connected = model.connection {
            return "No \(model.runStatus) runs on the desktop right now."
        }
        return "Connect to see runs."
    }
}

/// F072 — a single run: its transcript (read-only) + send a message into it
/// (a live F049 interjection). Building the pinned client from the Keychain
/// credential, same as DesktopHomeModel.
@MainActor
final class RunDetailModel: ObservableObject {
    let desktop: DesktopRecord
    let run: MobileRunProjection
    @Published var events: [MobileEventProjection] = []
    @Published var loading = false
    @Published var composer = ""
    @Published var sending = false
    @Published var note: String?

    private let keychain = KeychainCredentialStore()

    init(desktop: DesktopRecord, run: MobileRunProjection) {
        self.desktop = desktop
        self.run = run
    }

    private func client() -> DesktopClient? {
        guard let cred = try? keychain.load(desktopId: desktop.desktopId) else { return nil }
        return DesktopClient.pinned(record: desktop, credential: cred)
    }

    func load() async {
        guard let client = client() else { return }
        loading = true
        defer { loading = false }
        do { events = try await client.runEvents(runId: run.runId) }
        catch { /* keep whatever we have */ }
    }

    /// "Simple" transcript: the actual conversation (member messages, the final
    /// answer, your own messages, tool/decision/status cards) — NOT the internal
    /// plumbing events the backend tags `mobile_visibility == "metadata"` (which
    /// carry no body and would otherwise render as "Metadata event").
    var visibleEvents: [MobileEventProjection] {
        events.filter { $0.mobileVisibility != "metadata" }
    }

    func send() async {
        let text = composer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending, let client = client() else { return }
        sending = true
        note = nil
        defer { sending = false }
        do {
            try await client.sendMessage(runId: run.runId, text: text)
            composer = ""
            note = "Sent — the next council member will pick it up."
            await load()  // refresh so the message shows in the transcript
        } catch DesktopError.forbidden {
            note = "This phone can't send yet — grant “send messages” on the desktop."
        } catch DesktopError.conflict {
            note = "This run already finished."
        } catch {
            note = "Couldn't send. Same Wi-Fi / desktop open?"
        }
    }

    var canSend: Bool {
        !composer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !sending
    }

    @Published var cancelling = false

    func cancel() async {
        guard let client = client() else { return }
        cancelling = true
        note = nil
        defer { cancelling = false }
        do {
            try await client.cancelRun(runId: run.runId)
            note = "Run cancelled."
            await load()
        } catch DesktopError.forbidden {
            note = "Grant “cancel runs” to this phone on the desktop."
        } catch {
            note = "Couldn't cancel the run."
        }
    }
}

// MARK: - F073 pane models (approvals / attention / handoff)

/// Build the pinned client for a desktop from the stored Keychain credential.
@MainActor
func pinnedClient(for desktop: DesktopRecord) -> DesktopClient? {
    guard let cred = try? KeychainCredentialStore().load(desktopId: desktop.desktopId) else { return nil }
    return DesktopClient.pinned(record: desktop, credential: cred)
}

@MainActor
final class ApprovalsModel: ObservableObject {
    let desktop: DesktopRecord
    @Published var decisions: [MobileApprovalCard] = []
    @Published var loading = false
    @Published var note: String?
    @Published var busyId: String?

    init(desktop: DesktopRecord) { self.desktop = desktop }

    func load() async {
        guard let client = pinnedClient(for: desktop) else { return }
        loading = true
        defer { loading = false }
        do { decisions = try await client.pendingDecisions() }
        catch { /* keep what we have */ }
    }

    func resolve(_ card: MobileApprovalCard, approve: Bool) async {
        guard let client = pinnedClient(for: desktop) else { return }
        busyId = card.decisionId
        note = nil
        defer { busyId = nil }
        do {
            try await client.resolveDecision(
                runId: card.runId, decisionId: card.decisionId, approve: approve)
            decisions.removeAll { $0.decisionId == card.decisionId }
        } catch DesktopError.forbidden {
            note = "This phone can't \(approve ? "approve" : "deny") that — grant the capability on the desktop."
        } catch DesktopError.conflict {
            note = "That decision changed — refreshing."
            await load()
        } catch {
            note = "Couldn't update the decision."
        }
    }
}

@MainActor
final class AttentionModel: ObservableObject {
    let desktop: DesktopRecord
    @Published var summary: MobileAttentionResponse?
    @Published var loading = false

    init(desktop: DesktopRecord) { self.desktop = desktop }

    func load() async {
        guard let client = pinnedClient(for: desktop) else { return }
        loading = true
        defer { loading = false }
        do { summary = try await client.attention() }
        catch { /* keep */ }
    }
}

@MainActor
final class HandoffModel: ObservableObject {
    let desktop: DesktopRecord
    @Published var text = ""
    @Published var items: [MobileInboxItem] = []
    @Published var sending = false
    @Published var note: String?

    init(desktop: DesktopRecord) { self.desktop = desktop }

    func load() async {
        guard let client = pinnedClient(for: desktop) else { return }
        do { items = try await client.inboxItems() }
        catch { /* keep */ }
    }

    var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !sending
    }

    func send() async {
        let payload = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !payload.isEmpty, !sending, let client = pinnedClient(for: desktop) else { return }
        sending = true
        note = nil
        defer { sending = false }
        let kind = payload.lowercased().hasPrefix("http") ? "url" : "text"
        do {
            try await client.sendToInbox(text: payload, kind: kind)
            text = ""
            note = "Sent to the desktop inbox."
            await load()
        } catch DesktopError.forbidden {
            note = "Grant “send messages” to this phone on the desktop."
        } catch {
            note = "Couldn't send to the desktop."
        }
    }
}

struct RunDetailPane: View {
    @StateObject private var model: RunDetailModel

    init(desktop: DesktopRecord, run: MobileRunProjection) {
        _model = StateObject(wrappedValue: RunDetailModel(desktop: desktop, run: run))
    }

    var body: some View {
        List {
            Section("Status") {
                Text(model.run.status)
                if model.run.needsAttention {
                    Label("Needs attention", systemImage: "bell")
                }
            }
            Section("Transcript") {
                let shown = model.visibleEvents
                if model.loading && shown.isEmpty {
                    HStack { ProgressView(); Text("Loading…").foregroundStyle(.secondary) }
                } else if shown.isEmpty {
                    ContentUnavailableView(
                        "No Messages Yet",
                        systemImage: "text.bubble",
                        description: Text("Member turns and your messages appear here.")
                    )
                } else {
                    ForEach(shown) { event in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(event.actor.name).font(.caption).foregroundStyle(.secondary)
                            EventBodyView(eventBody: event.body)
                        }
                    }
                }
            }
            Section("Send a message") {
                TextField("Message to the council", text: $model.composer, axis: .vertical)
                    .lineLimit(2...6)
                    .autocorrectionDisabled()
                if let note = model.note {
                    Text(note).font(.caption).foregroundStyle(.secondary)
                }
                Button {
                    Task { await model.send() }
                } label: {
                    if model.sending { ProgressView() }
                    else { Label("Send", systemImage: "paperplane") }
                }
                .disabled(!model.canSend)
            }
            Section {
                Button(role: .destructive) {
                    Task { await model.cancel() }
                } label: {
                    if model.cancelling { ProgressView() }
                    else { Label("Cancel run", systemImage: "stop.circle") }
                }
                .disabled(model.cancelling)
            }
        }
        .navigationTitle(model.run.title)
        .task { await model.load() }
        .refreshable { await model.load() }
    }
}
