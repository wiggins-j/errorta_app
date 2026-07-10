import ErrortaCompanionCore
import SwiftUI

struct RootView: View {
    // F070 — load persisted desktops on launch so pairing survives a relaunch.
    @State private var desktops: [DesktopRecord] = DesktopStore().load()
    @State private var showingPairing = false
    private let store = DesktopStore()

    var body: some View {
        NavigationStack {
            List {
                if desktops.isEmpty {
                    ContentUnavailableView {
                        Label {
                            Text("No Desktops")
                        } icon: {
                            Image("BrandLogo")
                                .resizable()
                                .scaledToFit()
                                .frame(width: 72, height: 72)
                                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                        }
                    } description: {
                        Text("Pair this iPhone from Errorta desktop settings.")
                    }
                } else {
                    ForEach(desktops) { desktop in
                        NavigationLink(desktop.displayName) {
                            DesktopHomeView(desktop: desktop)
                        }
                    }
                    .onDelete { indexSet in
                        for index in indexSet {
                            let id = desktops[index].desktopId
                            try? KeychainCredentialStore().delete(desktopId: id)
                            store.remove(desktopId: id)
                        }
                        desktops.remove(atOffsets: indexSet)
                    }
                }
            }
            .navigationTitle("Errorta")
            .toolbar {
                Button {
                    showingPairing = true
                } label: {
                    Label("Add Desktop", systemImage: "qrcode.viewfinder")
                }
            }
            .sheet(isPresented: $showingPairing) {
                PairingSheet { desktop in
                    if !desktops.contains(where: { $0.desktopId == desktop.desktopId }) {
                        desktops.append(desktop)
                    }
                    store.upsert(desktop)  // persist so it survives relaunch
                    showingPairing = false
                }
            }
        }
        // Re-read on appear in case another scene/instance changed it.
        .onAppear { desktops = store.load() }
    }
}

struct DesktopHomeView: View {
    @StateObject private var model: DesktopHomeModel
    @State private var selectedPane: DesktopPane = .runs
    @State private var showStartRun = false

    init(desktop: DesktopRecord) {
        _model = StateObject(wrappedValue: DesktopHomeModel(desktop: desktop))
    }

    var body: some View {
        List {
            Section("Connection") {
                ConnectionStateRow(model: model)
            }
            Section {
                Picker("View", selection: $selectedPane) {
                    ForEach(DesktopPane.allCases) { pane in
                        Label(pane.title, systemImage: pane.systemImage).tag(pane)
                    }
                }
                .pickerStyle(.segmented)
            }
            switch selectedPane {
            case .runs:
                RunListPane(model: model)
            case .approvals:
                ApprovalPane(desktop: model.desktop)
            case .inbox:
                HandoffPane(desktop: model.desktop)
            case .attention:
                AttentionPane(desktop: model.desktop)
            }
        }
        .navigationTitle(model.desktop.displayName)
        .toolbar {
            Button {
                showStartRun = true
            } label: {
                Label("New run", systemImage: "plus")
            }
        }
        .sheet(isPresented: $showStartRun) {
            StartRunSheet(model: model)
        }
        .refreshable { await model.refresh() }
        .task { await model.connect() }
    }
}

struct StartRunSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var model: DesktopHomeModel
    @State private var prompt = ""
    @State private var selectedRoomId = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Room") {
                    if model.loadingRooms {
                        HStack {
                            ProgressView()
                            Text("Loading rooms…").foregroundStyle(.secondary)
                        }
                    } else if model.rooms.isEmpty {
                        ContentUnavailableView(
                            "No Rooms",
                            systemImage: "rectangle.stack.badge.plus",
                            description: Text("Create rooms on the desktop, then refresh this sheet.")
                        )
                    } else {
                        Picker("Room", selection: $selectedRoomId) {
                            ForEach(model.rooms) { room in
                                Text(room.name).tag(room.roomId)
                            }
                        }
                    }
                    Button("Refresh rooms") {
                        Task { await refreshRooms() }
                    }
                    .disabled(model.loadingRooms)
                }
                Section("Prompt") {
                    TextField("Ask the council…", text: $prompt, axis: .vertical)
                        .lineLimit(3...8)
                        .autocorrectionDisabled()
                }
                if let err = model.startError {
                    Text(err).font(.caption).foregroundStyle(.red)
                }
            }
            .navigationTitle("New Run")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Start") {
                        Task {
                            let roomId = selectedRoomId.isEmpty ? model.rooms.first?.roomId : selectedRoomId
                            if await model.startRun(prompt: prompt, roomId: roomId) { dismiss() }
                        }
                    }
                    .disabled(
                        prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
                        model.starting ||
                        selectedRoomId.isEmpty
                    )
                }
            }
        }
        .task { await refreshRooms() }
    }

    private func refreshRooms() async {
        await model.loadRooms()
        if selectedRoomId.isEmpty || !model.rooms.contains(where: { $0.roomId == selectedRoomId }) {
            selectedRoomId = model.rooms.first?.roomId ?? ""
        }
    }
}

enum DesktopPane: String, CaseIterable, Identifiable {
    case runs
    case approvals
    case inbox
    case attention

    var id: String { rawValue }

    var title: String {
        switch self {
        case .runs: "Runs"
        case .approvals: "Approvals"
        case .inbox: "Inbox"
        case .attention: "Attention"
        }
    }

    var systemImage: String {
        switch self {
        case .runs: "list.bullet.rectangle"
        case .approvals: "checkmark.seal"
        case .inbox: "tray.and.arrow.down"
        case .attention: "bell"
        }
    }
}




struct EventBodyView: View {
    let eventBody: MobileEventBody?

    var body: some View {
        switch eventBody {
        case .markdown(_, let text):
            Text(text)
        case .toolCall(let card):
            Label(card.summary ?? card.status, systemImage: "wrench.and.screwdriver")
        case .pendingDecision(let card):
            Label(card.reasonCode ?? "Pending decision", systemImage: "checkmark.seal")
        case .runStatus(let status, let reason):
            Label(reason ?? status, systemImage: "circle.dashed")
        case .summary(let text, _):
            Label(text, systemImage: "text.badge.checkmark")
        case .unknown, .none:
            // The simple transcript filters these out (metadata-only events);
            // if one slips through, render nothing rather than noise.
            EmptyView()
        }
    }
}

struct ApprovalPane: View {
    @StateObject private var model: ApprovalsModel

    init(desktop: DesktopRecord) {
        _model = StateObject(wrappedValue: ApprovalsModel(desktop: desktop))
    }

    var body: some View {
        Section("Approvals") {
            if let note = model.note {
                Text(note).font(.caption).foregroundStyle(.secondary)
            }
            if model.loading && model.decisions.isEmpty {
                HStack { ProgressView(); Text("Loading…").foregroundStyle(.secondary) }
            } else if model.decisions.isEmpty {
                ContentUnavailableView(
                    "No Pending Approvals",
                    systemImage: "checkmark.seal",
                    description: Text("Tool and policy approvals appear here.")
                )
            } else {
                ForEach(model.decisions) { decision in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(decision.title).font(.headline)
                        Text(decision.summary).foregroundStyle(.secondary)
                        HStack {
                            Text(decision.risk.capitalized).font(.caption)
                                .foregroundStyle(decision.risk == "high" ? .red : .secondary)
                            Spacer()
                            if model.busyId == decision.decisionId {
                                ProgressView()
                            } else {
                                Button("Deny", role: .destructive) {
                                    Task { await model.resolve(decision, approve: false) }
                                }
                                .disabled(!decision.actions.canDeny)
                                Button("Approve") {
                                    Task { await model.resolve(decision, approve: true) }
                                }
                                .disabled(!decision.actions.canApprove)
                            }
                        }
                    }
                }
            }
        }
        .task { await model.load() }
        .refreshable { await model.load() }
    }
}

struct HandoffPane: View {
    @StateObject private var model: HandoffModel

    init(desktop: DesktopRecord) {
        _model = StateObject(wrappedValue: HandoffModel(desktop: desktop))
    }

    var body: some View {
        Section("Send to Desktop") {
            TextField("Text or URL", text: $model.text, axis: .vertical)
                .lineLimit(3...8)
                .autocorrectionDisabled()
            if let note = model.note {
                Text(note).font(.caption).foregroundStyle(.secondary)
            }
            Button {
                Task { await model.send() }
            } label: {
                if model.sending { ProgressView() }
                else { Label("Send to Desktop", systemImage: "square.and.arrow.up") }
            }
            .disabled(!model.canSend)
        }
        .task { await model.load() }
        if !model.items.isEmpty {
            Section("Recent") {
                ForEach(model.items) { item in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.text).lineLimit(2)
                        Text("\(item.kind) · \(item.status)")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

struct AttentionPane: View {
    @StateObject private var model: AttentionModel

    init(desktop: DesktopRecord) {
        _model = StateObject(wrappedValue: AttentionModel(desktop: desktop))
    }

    var body: some View {
        Section("Attention") {
            let runs = model.summary?.runs ?? []
            if runs.isEmpty {
                ContentUnavailableView(
                    "All Clear",
                    systemImage: "bell.slash",
                    description: Text("Failed runs and pending approvals appear here.")
                )
            } else {
                if let count = model.summary?.pendingDecisionCount, count > 0 {
                    Label("\(count) pending decision\(count == 1 ? "" : "s")", systemImage: "checkmark.seal")
                        .foregroundStyle(.orange)
                }
                ForEach(runs) { run in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(run.title)
                        Text(run.attentionReasons.joined(separator: ", "))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .task { await model.load() }
        .refreshable { await model.load() }
    }
}

struct PairDesktopSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var payloadText = ""
    @State private var errorText: String?
    /// Hands the validated raw pairing JSON up so it runs the SAME full login
    /// handshake as a scanned QR (complete → PIN → token → Keychain). Manual
    /// entry is the camera-less path to a real credential — not a host-only
    /// shim. A host-only update can't authenticate (no saved token), which is
    /// the whole reason this exists for off-LAN / no-camera pairing.
    let onSubmit: (String) -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section("Pairing Payload") {
                    TextEditor(text: $payloadText)
                        .frame(minHeight: 180)
                        .autocorrectionDisabled()
                    if let errorText {
                        Text(errorText)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("Add Desktop")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Pair") {
                        pair()
                    }
                    .disabled(payloadText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }

    private func pair() {
        let text = payloadText.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            // Validate locally for a friendly inline error; the parent re-runs
            // the authoritative route + full handshake over the network.
            _ = try PairingPayload.decode(Data(text.utf8))
            onSubmit(text)
        } catch {
            errorText = "Invalid or expired pairing code."
        }
    }
}
