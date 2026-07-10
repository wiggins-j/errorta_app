import ErrortaCompanionCore
import SwiftUI
#if os(iOS)
import VisionKit
#endif

/// Numeric-PIN keyboard hints — iOS-only modifiers, no-op on the macOS dev harness.
private extension View {
    func numericPINField() -> some View {
        #if os(iOS)
        return self.keyboardType(.numberPad).textContentType(.oneTimeCode)
        #else
        return self
        #endif
    }
}

// MARK: - Flow model

/// F068 — drives the live pairing handshake: scan → complete → (PIN) → poll →
/// store. UI-thread state; all networking is the Core `PairingService` over a
/// pinned TLS session.
@MainActor
final class PairingFlowModel: ObservableObject {
    enum Step: Equatable {
        case scanning
        case connecting(desktopName: String)
        case pin(desktopName: String)
        case waitingApproval(desktopName: String)
        case done
        case failed(String)
    }

    @Published var step: Step = .scanning
    @Published var pin = PinEntryModel()
    @Published var submitting = false

    private var handshake: PairingHandshake?
    private let keychain = KeychainCredentialStore()
    let onPaired: (DesktopRecord) -> Void

    init(onPaired: @escaping (DesktopRecord) -> Void) {
        self.onPaired = onPaired
    }

    /// Handle a scanned QR string (or pasted JSON). Returns false if the scanner
    /// should keep running (not-ours / transient), true once we've taken over.
    @discardableResult
    func handleScanned(_ text: String) -> Bool {
        switch ScanResultRouter.route(text) {
        case .pair(let payload):
            connect(payload: payload)
            return true
        case .expired:
            step = .failed("This pairing code expired — get a fresh one on your computer.")
            return true
        case .insecure:
            step = .failed("That isn't a secure pairing code.")
            return true
        case .notPairing:
            return false
        }
    }

    func connect(payload: PairingPayload) {
        // Defense in depth (independent of the scanner's requireCert gate): never
        // talk plaintext to a non-loopback host. A QR pairing always has a cert.
        let cert = payload.tlsCertSha256 ?? ""
        let loopbackHosts: Set<String> = ["127.0.0.1", "localhost", "::1"]
        let allLoopback = payload.hosts.allSatisfy { loopbackHosts.contains($0.host) }
        if cert.isEmpty && !allLoopback {
            step = .failed("That isn't a secure pairing code.")
            return
        }
        step = .connecting(desktopName: payload.desktopName)
        let service = PairingService.pinned(expectedSha256: cert)
        Task {
            do {
                let hs = try await service.connectAndComplete(
                    payload: payload, displayName: deviceDisplayName())
                self.handshake = hs
                if hs.requiresPin {
                    self.step = .pin(desktopName: payload.desktopName)
                } else {
                    self.step = .waitingApproval(desktopName: payload.desktopName)
                    await self.awaitTokenAndStore(service: service, payload: payload)
                }
            } catch {
                self.step = .failed(Self.message(for: error))
            }
        }
    }

    func submitPin() {
        guard let handshake, pin.canSubmit, !submitting else { return }
        let service = PairingService.pinned(
            expectedSha256: handshake.payload.tlsCertSha256 ?? "")
        submitting = true
        let entered = pin.digits
        Task {
            defer { self.submitting = false }
            do {
                try await service.verifyPin(handshake: handshake, pin: entered)
                await self.awaitTokenAndStore(service: service, payload: handshake.payload)
            } catch let e as PairingError {
                let locked = self.pin.apply(error: e)
                if locked {
                    // Session burned — leave the PIN screen so "Scan again" shows.
                    self.step = .failed(Self.message(for: PairingError.pinLocked))
                    return
                }
                if case .pinMismatch = e { return }  // stay on PIN screen, show remaining
                self.step = .failed(Self.message(for: e))
            } catch {
                self.step = .failed(Self.message(for: error))
            }
        }
    }

    private func awaitTokenAndStore(service: PairingService, payload: PairingPayload) async {
        guard let handshake else { return }
        do {
            let credential = try await service.awaitToken(handshake: handshake)
            try keychain.save(credential, for: payload.connectorId)
            self.onPaired(DesktopRecord(pairingPayload: payload))
            self.step = .done
        } catch {
            self.step = .failed(Self.message(for: error))
        }
    }

    private func deviceDisplayName() -> String {
        #if canImport(UIKit)
        return UIDevice.current.name
        #else
        return "iPhone"
        #endif
    }

    static func message(for error: Error) -> String {
        switch error {
        case PairingError.expired: return "This pairing code expired."
        case PairingError.pinLocked: return "Too many PIN attempts. Start a new pairing on your computer."
        case PairingError.rateLimited: return "Too many attempts. Wait a moment, then start a new pairing."
        case PairingError.notAwaitingApproval: return "This pairing is no longer waiting — start over."
        case PairingError.pinNotRequired, PairingError.sessionNotFound:
            return "This pairing is no longer valid — start a new one on your computer."
        case PairingError.tokenRejected: return "The desktop rejected this pairing code."
        case PairingError.noReachableHost, PairingError.network:
            return "Couldn't reach the desktop. Same Wi-Fi? Connector enabled?"
        case PairingError.tokenNotDelivered: return "Pairing completed elsewhere — start over."
        case PairingError.denied: return "The desktop denied this device."
        case PairingError.insecurePayload: return "That isn't a secure pairing code."
        default: return "Pairing failed. Please try again."
        }
    }
}

// MARK: - Entry sheet

struct PairingSheet: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model: PairingFlowModel
    @State private var showingManual = false

    init(onPaired: @escaping (DesktopRecord) -> Void) {
        _model = StateObject(wrappedValue: PairingFlowModel(onPaired: onPaired))
    }

    var body: some View {
        NavigationStack {
            Group {
                switch model.step {
                case .scanning:
                    ScannerPane(onScanned: { model.handleScanned($0) })
                case .connecting(let name):
                    ProgressPane(title: "Connecting to \(name)…")
                case .pin(let name):
                    PinEntryView(desktopName: name, model: model)
                case .waitingApproval(let name):
                    ProgressPane(title: "Waiting for approval on \(name)…")
                case .done:
                    DonePane(onClose: { dismiss() })
                case .failed(let message):
                    FailedPane(message: message, onRetry: { model.step = .scanning })
                }
            }
            .navigationTitle("Add Desktop")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button("Enter manually") { showingManual = true }
                }
            }
            .sheet(isPresented: $showingManual) {
                PairDesktopSheet { pasted in
                    // Close the paste sheet and run the SAME pipeline the camera
                    // does — complete → PIN → token → Keychain. The PairingSheet
                    // step machine then shows connecting / PIN / approval / done.
                    showingManual = false
                    model.handleScanned(pasted)
                }
            }
            .onChange(of: model.step) { _, step in
                if step == .done { dismiss() }
            }
        }
    }
}

// MARK: - Panes

private struct ScannerPane: View {
    let onScanned: (String) -> Void

    var body: some View {
        if QRScannerView.isSupported {
            QRScannerView(onScanned: onScanned)
                .overlay(alignment: .bottom) {
                    Text("Scan the QR code shown by Errorta on your computer")
                        .font(.callout)
                        .padding(12)
                        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
                        .padding()
                }
                .ignoresSafeArea(edges: .bottom)
        } else {
            ContentUnavailableView(
                "Camera Unavailable",
                systemImage: "camera.fill",
                description: Text("Use “Enter manually” to paste the pairing code.")
            )
        }
    }
}

private struct ProgressPane: View {
    let title: String
    var body: some View {
        VStack(spacing: 16) {
            ProgressView()
            Text(title).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct DonePane: View {
    let onClose: () -> Void
    var body: some View {
        ContentUnavailableView {
            Label("Paired", systemImage: "checkmark.seal.fill")
        } description: {
            Text("This iPhone is connected (read-only). Grant more from the desktop.")
        } actions: {
            Button("Done", action: onClose).buttonStyle(.borderedProminent)
        }
    }
}

private struct FailedPane: View {
    let message: String
    let onRetry: () -> Void
    var body: some View {
        ContentUnavailableView {
            Label("Pairing Failed", systemImage: "exclamationmark.triangle.fill")
        } description: {
            Text(message)
        } actions: {
            Button("Scan again", action: onRetry).buttonStyle(.borderedProminent)
        }
    }
}

// MARK: - PIN entry view

struct PinEntryView: View {
    let desktopName: String
    @ObservedObject var model: PairingFlowModel
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 24) {
            Text("Enter the PIN shown on \(desktopName)")
                .font(.headline)
                .multilineTextAlignment(.center)

            TextField("", text: Binding(
                get: { model.pin.digits },
                set: { model.pin.setDigits($0) }
            ))
            .numericPINField()
            .font(.system(.largeTitle, design: .monospaced))
            .multilineTextAlignment(.center)
            .focused($focused)
            .disabled(model.pin.isLocked || model.submitting)

            Text(model.pin.helperText)
                .font(.callout)
                .foregroundStyle(model.pin.attemptsRemaining != nil || model.pin.isLocked ? .red : .secondary)
                .multilineTextAlignment(.center)

            Button {
                model.submitPin()
            } label: {
                if model.submitting { ProgressView() } else { Text("Pair") }
            }
            .buttonStyle(.borderedProminent)
            .disabled(!model.pin.canSubmit || model.submitting)

            Spacer()
        }
        .padding()
        .onAppear { focused = true }
    }
}

// MARK: - Camera QR scanner (VisionKit, iOS only)

#if os(iOS)
struct QRScannerView: UIViewControllerRepresentable {
    let onScanned: (String) -> Void

    static var isSupported: Bool {
        DataScannerViewController.isSupported && DataScannerViewController.isAvailable
    }

    func makeCoordinator() -> Coordinator { Coordinator(onScanned: onScanned) }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced,
            isHighFrameRateTrackingEnabled: false,
            isHighlightingEnabled: true
        )
        scanner.delegate = context.coordinator
        return scanner
    }

    func updateUIViewController(_ scanner: DataScannerViewController, context: Context) {
        try? scanner.startScanning()
    }

    static func dismantleUIViewController(_ scanner: DataScannerViewController, coordinator: Coordinator) {
        scanner.stopScanning()
    }

    @MainActor
    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let onScanned: (String) -> Void
        private var handled = false

        init(onScanned: @escaping (String) -> Void) { self.onScanned = onScanned }

        func dataScanner(
            _ dataScanner: DataScannerViewController,
            didAdd addedItems: [RecognizedItem],
            allItems: [RecognizedItem]
        ) {
            guard !handled else { return }
            for item in addedItems {
                if case let .barcode(barcode) = item, let text = barcode.payloadStringValue {
                    handled = true
                    onScanned(text)
                    return
                }
            }
        }
    }
}
#else
/// macOS dev-harness stub: no camera; the manual-paste path is used instead.
struct QRScannerView: View {
    let onScanned: (String) -> Void
    static var isSupported: Bool { false }
    var body: some View { EmptyView() }
}
#endif
