import Foundation

/// Response of `GET /mobile/v1/capabilities` — the cheapest authorized call,
/// used to verify a paired desktop is reachable and the token still works.
public struct CapabilitiesResponse: Decodable, Equatable, Sendable {
    public let deviceId: String
    public let capabilities: [String: Bool]

    enum CodingKeys: String, CodingKey {
        case deviceId = "device_id"
        case capabilities
    }

    public func can(_ capability: String) -> Bool { capabilities[capability] ?? false }
}

public enum DesktopError: Error, Equatable, Sendable {
    case unauthorized          // 401 — token revoked/expired; re-pair
    case forbidden(String)     // 403 — device lacks the capability
    case conflict              // 409 — run finished, or a decision changed under us
    case connectorDisabled     // 503 — desktop turned the connector off
    case noReachableHost
    case network
    case server(String)
    case decoding
}

/// Response of `GET /mobile/v1/runs/{id}/events`.
public struct RunEventsResponse: Decodable, Equatable, Sendable {
    public let events: [MobileEventProjection]
    public let lastSequence: Int

    enum CodingKeys: String, CodingKey {
        case events
        case lastSequence = "last_sequence"
    }
}

/// F070 — talks to a paired desktop over its pinned TLS session using the stored
/// Keychain credential. Transport-injected so the request/decode/fallover logic
/// is unit-testable without a socket (mirrors PairingService).
public struct DesktopClient: Sendable {
    public typealias Transport = @Sendable (URLRequest) async throws -> (Data, HTTPURLResponse)

    private let record: DesktopRecord
    private let credential: MobileCredential
    private let transport: Transport
    private let api = MobileApiClient()

    public init(record: DesktopRecord, credential: MobileCredential, transport: @escaping Transport) {
        self.record = record
        self.credential = credential
        self.transport = transport
    }

    /// Default: a TLS session pinned to the desktop's stored leaf cert.
    public static func pinned(record: DesktopRecord, credential: MobileCredential) -> DesktopClient {
        let session = makePinnedSession(expectedSha256: record.tlsCertSha256)
        return DesktopClient(record: record, credential: credential) { request in
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw DesktopError.network }
            return (data, http)
        }
    }

    /// `https://host:port` for each ordered host (http only when no cert to pin).
    func baseURLs() -> [URL] {
        let scheme = record.tlsCertSha256.isEmpty ? "http" : "https"
        return record.orderedHosts().compactMap { host in
            let port = host.port ?? 8770
            return URL(string: "\(scheme)://\(host.host):\(port)")
        }
    }

    /// F076 — the desktop's current reachable hosts (LAN + Tailscale, if enabled).
    /// The app refreshes its stored host list from this so enabling Tailscale
    /// later is picked up without re-pairing (pair once, roam LAN↔Tailscale).
    public func connectionInfo() async throws -> ConnectionInfo {
        try await send { base in
            try self.api.makeAuthorizedRequest(
                baseURL: base, path: "mobile/v1/connection-info",
                deviceId: self.credential.deviceId, sessionToken: self.credential.sessionToken,
                body: Optional<Data>.none)
        }
    }

    /// Verify reachability + token validity (and learn current capabilities).
    public func verify() async throws -> CapabilitiesResponse {
        try await send { base in
            try self.api.makeAuthorizedRequest(
                baseURL: base, path: "mobile/v1/capabilities",
                deviceId: self.credential.deviceId, sessionToken: self.credential.sessionToken)
        }
    }

    /// Read-only list of runs. `status` is "active" or "recent".
    public func runs(status: String = "active") async throws -> [MobileRunProjection] {
        let resp: MobileRunsResponse = try await send { base in
            try self.api.makeRunsRequest(
                baseURL: base, deviceId: self.credential.deviceId,
                sessionToken: self.credential.sessionToken, status: status)
        }
        return resp.runs
    }

    /// Desktop-built Council rooms available for starting a new prompt.
    public func rooms() async throws -> [MobileRoomProjection] {
        let resp: MobileRoomsResponse = try await send { base in
            try self.api.makeRoomsRequest(
                baseURL: base, deviceId: self.credential.deviceId,
                sessionToken: self.credential.sessionToken)
        }
        return resp.rooms
    }

    /// A run's transcript events (read-only; `read_runs`).
    public func runEvents(runId: String, afterSequence: Int = 0) async throws -> [MobileEventProjection] {
        let resp: RunEventsResponse = try await send { base in
            try self.api.makeRunEventsRequest(
                baseURL: base, deviceId: self.credential.deviceId,
                sessionToken: self.credential.sessionToken,
                runId: runId, afterSequence: afterSequence)
        }
        return resp.events
    }

    /// Send a message into a run — a live F049 interjection the next council
    /// member picks up. Needs the `send_messages` capability (403 otherwise).
    public func sendMessage(runId: String, text: String) async throws {
        let body = MobileFollowUpRequest(message: text)
        try await sendVoid { base in
            try self.api.makeFollowUpRequest(
                baseURL: base, deviceId: self.credential.deviceId,
                sessionToken: self.credential.sessionToken,
                runId: runId, body: body)
        }
    }

    // MARK: - F073 approvals / attention / inbox / start / cancel

    /// Pending tool/policy decisions awaiting a human (any paired device).
    public func pendingDecisions() async throws -> [MobileApprovalCard] {
        let resp: DecisionsResponse = try await send { base in
            try self.authed(base, "mobile/v1/pending-decisions")
        }
        return resp.decisions
    }

    /// Approve or deny a pending decision. Needs the decision's
    /// `required_capability` (403); a 409 means the decision changed (refresh).
    public func resolveDecision(runId: String, decisionId: String, approve: Bool) async throws {
        let verb = approve ? "approve" : "reject"
        try await sendVoid { base in
            try self.authed(
                base, "mobile/v1/pending-decisions/\(runId)/\(decisionId)/\(verb)",
                method: "POST")
        }
    }

    /// Runs needing attention + pending-decision counts.
    public func attention() async throws -> MobileAttentionResponse {
        try await send { base in try self.authed(base, "mobile/v1/attention") }
    }

    /// Start a Council run. Needs `start_runs`. Returns nothing — refresh runs.
    public func startRun(prompt: String, roomId: String? = nil) async throws {
        var dict: [String: Any] = ["prompt": prompt]
        if let roomId { dict["room_id"] = roomId }
        let data = try JSONSerialization.data(withJSONObject: dict)  // Sendable
        try await sendVoid { base in
            try self.authed(base, "mobile/v1/runs", method: "POST", body: data)
        }
    }

    /// Cancel a run. Needs `cancel_runs`.
    public func cancelRun(runId: String, reason: String = "Cancelled from iPhone.") async throws {
        let data = try JSONSerialization.data(withJSONObject: ["reason": reason])
        try await sendVoid { base in
            try self.authed(base, "mobile/v1/runs/\(runId)/cancel", method: "POST", body: data)
        }
    }

    /// Inbox items shared to the desktop from this device.
    public func inboxItems() async throws -> [MobileInboxItem] {
        let resp: MobileInboxItemsResponse = try await send { base in
            try self.authed(base, "mobile/v1/inbox-items")
        }
        return resp.items
    }

    /// Hand a piece of text/URL to the desktop's inbox. Needs `send_messages`.
    public func sendToInbox(text: String, kind: String = "text") async throws {
        let data = try JSONSerialization.data(withJSONObject: ["kind": kind, "text": text])
        try await sendVoid { base in
            try self.authed(base, "mobile/v1/inbox-items", method: "POST", body: data)
        }
    }

    // MARK: - transport + fallover

    /// Build an authorized request for a JSON path (no dedicated builder needed).
    /// `body` is pre-serialized Data (Sendable) so it's safe in the @Sendable closure.
    private func authed(
        _ base: URL, _ path: String, method: String = "GET", body: Data? = nil
    ) throws -> URLRequest {
        var req = try api.makeAuthorizedRequest(
            baseURL: base, path: path, method: method,
            deviceId: credential.deviceId, sessionToken: credential.sessionToken,
            body: Optional<Data>.none)
        if let body {
            req.httpBody = body
            req.setValue("application/json", forHTTPHeaderField: "content-type")
        }
        return req
    }

    /// Try each ordered host; return the body of the first reachable 2xx.
    private func raw(_ build: @Sendable (URL) throws -> URLRequest) async throws -> Data {
        let urls = baseURLs()
        guard !urls.isEmpty else { throw DesktopError.noReachableHost }
        var lastTransportError: Error?
        for base in urls {
            do {
                let req = try build(base)
                let (data, http) = try await transport(req)
                guard (200..<300).contains(http.statusCode) else {
                    throw Self.mapError(status: http.statusCode, body: data)
                }
                return data
            } catch let e as DesktopError {
                throw e  // server-authoritative — don't try other hosts
            } catch {
                lastTransportError = error  // connection failure — try next host
                continue
            }
        }
        throw lastTransportError == nil ? DesktopError.noReachableHost : DesktopError.network
    }

    private func send<T: Decodable>(_ build: @Sendable (URL) throws -> URLRequest) async throws -> T {
        let data = try await raw(build)
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw DesktopError.decoding }
    }

    private func sendVoid(_ build: @Sendable (URL) throws -> URLRequest) async throws {
        _ = try await raw(build)
    }

    static func mapError(status: Int, body: Data) -> DesktopError {
        struct Body: Decodable { let detail: String? }
        let detail = (try? JSONDecoder().decode(Body.self, from: body))?.detail ?? ""
        switch status {
        case 401: return .unauthorized
        case 403: return .forbidden(detail.isEmpty ? "forbidden" : detail)
        case 409: return .conflict
        case 503: return .connectorDisabled
        default: return .server(detail.isEmpty ? "http_\(status)" : detail)
        }
    }
}

/// `{ "decisions": [...] }`
struct DecisionsResponse: Decodable {
    let decisions: [MobileApprovalCard]
}

/// Response of `GET /mobile/v1/connection-info` (F076).
public struct ConnectionInfo: Decodable, Equatable, Sendable {
    public struct Host: Decodable, Equatable, Sendable {
        public let kind: String
        public let host: String
    }
    public let hosts: [Host]
    public let port: Int
    public let certSha256: String?

    enum CodingKeys: String, CodingKey {
        case hosts, port
        case certSha256 = "cert_sha256"
    }

    /// Learned host candidates (the listener serves all on one port).
    public func candidates() -> [HostCandidate] {
        hosts.map { HostCandidate(kind: $0.kind, host: $0.host, port: port) }
    }
}
