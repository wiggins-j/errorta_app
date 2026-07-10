import Foundation
import Security

// MARK: - Wire models (F067 contract)

/// Response of `POST /mobile/v1/pairing/complete`.
public struct CompletePairingResponse: Equatable, Sendable {
    public let sessionId: String
    public let state: String
    public let requiresPin: Bool

    public init(sessionId: String, state: String, requiresPin: Bool) {
        self.sessionId = sessionId
        self.state = state
        self.requiresPin = requiresPin
    }
}

extension CompletePairingResponse: Decodable {
    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case state
        case requiresPin = "requires_pin"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try c.decode(String.self, forKey: .sessionId)
        state = try c.decode(String.self, forKey: .state)
        // Fail safe: an older sidecar that omits the field is treated as
        // PIN-required (never silently skip the second factor).
        requiresPin = try c.decodeIfPresent(Bool.self, forKey: .requiresPin) ?? true
    }
}

/// Response of `POST /mobile/v1/pairing/status`. The token rides the
/// `state == "approved"` response exactly once.
public struct PairingStatusResponse: Equatable, Sendable, Decodable {
    public let state: String
    public let sessionToken: String?
    public let deviceId: String?

    enum CodingKeys: String, CodingKey {
        case state
        case sessionToken = "session_token"
        case deviceId = "device_id"
    }
}

/// A successful `complete`, carrying the host we reached and what's needed next.
public struct PairingHandshake: Equatable, Sendable {
    public let baseURL: URL
    public let sessionId: String
    public let requiresPin: Bool
    public let payload: PairingPayload
}

public enum PairingError: Error, Equatable, Sendable {
    case pinMismatch(remaining: Int)
    case pinLocked            // verify-pin 429 pairing_pin_locked (session burned)
    case rateLimited          // complete/status 429 pairing_rate_limited (flood guard)
    case expired
    case notAwaitingApproval
    case pinNotRequired       // 409 pairing_pin_not_required (loopback-dev session)
    case sessionNotFound      // 404 pairing_session_not_found
    case tokenRejected        // pairing_token_unknown
    case insecurePayload      // a QR pairing with no cert to pin
    case noReachableHost
    case tokenNotDelivered    // approved but token already consumed / lost
    case denied
    case network
    case server(String)
    case decoding
}

// MARK: - Pure request builders + decoding (testable, no I/O)

public enum PairingEndpoints {
    /// `https://host:port` for each ordered host candidate in the payload.
    public static func baseURLs(for payload: PairingPayload) -> [URL] {
        let scheme = (payload.tlsCertSha256 ?? "").isEmpty ? "http" : "https"
        return DesktopRecord(pairingPayload: payload).orderedHosts().compactMap { host in
            let port = host.port ?? payload.port
            return URL(string: "\(scheme)://\(host.host):\(port)")
        }
    }

    public static func request(baseURL: URL, path: String, json: [String: Any]) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: baseURL) else {
            throw PairingError.server("invalid_url")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "content-type")
        req.setValue("application/json", forHTTPHeaderField: "accept")
        req.httpBody = try JSONSerialization.data(withJSONObject: json)
        return req
    }

    public static func completeRequest(
        baseURL: URL, payload: PairingPayload, displayName: String, publicKey: String
    ) throws -> URLRequest {
        var body: [String: Any] = [
            "pairing_token": payload.pairingToken,
            "display_name": displayName,
            "platform": "ios",
            "public_key": publicKey,
        ]
        if let cert = payload.tlsCertSha256 { body["tls_cert_sha256"] = cert }
        return try request(baseURL: baseURL, path: "/mobile/v1/pairing/complete", json: body)
    }

    public static func verifyPinRequest(
        baseURL: URL, sessionId: String, pairingToken: String, pin: String
    ) throws -> URLRequest {
        try request(baseURL: baseURL, path: "/mobile/v1/pairing/verify-pin", json: [
            "session_id": sessionId, "pairing_token": pairingToken, "pin": pin,
        ])
    }

    public static func statusRequest(
        baseURL: URL, sessionId: String, pairingToken: String
    ) throws -> URLRequest {
        try request(baseURL: baseURL, path: "/mobile/v1/pairing/status", json: [
            "session_id": sessionId, "pairing_token": pairingToken,
        ])
    }

    /// Map a non-2xx pairing response body to a typed error (F067 error codes).
    public static func mapError(status: Int, body: Data) -> PairingError {
        struct ErrorBody: Decodable { let detail: String?; let attempts_remaining: Int? }
        let parsed = try? JSONDecoder().decode(ErrorBody.self, from: body)
        let detail = parsed?.detail ?? ""
        switch (status, detail) {
        case (401, "pairing_pin_mismatch"):
            return .pinMismatch(remaining: parsed?.attempts_remaining ?? 0)
        case (401, "pairing_token_unknown"), (400, "pairing_token_unknown"):
            return .tokenRejected
        case (429, "pairing_pin_locked"):
            return .pinLocked
        case (429, _):
            // complete/status flood guard (pairing_rate_limited) — NOT a PIN lock.
            return .rateLimited
        case (400, "pairing_token_expired"):
            return .expired
        case (404, _):
            return .sessionNotFound
        case (409, "pairing_pin_not_required"):
            return .pinNotRequired
        case (409, _):
            return .notAwaitingApproval
        default:
            return .server(detail.isEmpty ? "http_\(status)" : detail)
        }
    }
}

// MARK: - Orchestration (injectable transport)

public struct PairingService: Sendable {
    public typealias Transport = @Sendable (URLRequest) async throws -> (Data, HTTPURLResponse)

    private let transport: Transport

    public init(transport: @escaping Transport) {
        self.transport = transport
    }

    /// Default transport: a TLS session that pins the payload's leaf cert.
    public static func pinned(expectedSha256: String) -> PairingService {
        let session = makePinnedSession(expectedSha256: expectedSha256)
        return PairingService { request in
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw PairingError.network }
            return (data, http)
        }
    }

    /// A random opaque public key for the device record (auth is the minted
    /// bearer token; this is a forward-looking identifier, not yet used to sign).
    public static func generatePublicKey() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        if SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) != errSecSuccess {
            for i in bytes.indices { bytes[i] = UInt8.random(in: 0...255) }
        }
        return Data(bytes).base64EncodedString()
    }

    /// Try each ordered host until `complete` succeeds; returns the reached host.
    public func connectAndComplete(
        payload: PairingPayload,
        displayName: String,
        publicKey: String = PairingService.generatePublicKey()
    ) async throws -> PairingHandshake {
        let urls = PairingEndpoints.baseURLs(for: payload)
        guard !urls.isEmpty else { throw PairingError.noReachableHost }
        var lastError: Error = PairingError.noReachableHost
        for baseURL in urls {
            do {
                let req = try PairingEndpoints.completeRequest(
                    baseURL: baseURL, payload: payload, displayName: displayName, publicKey: publicKey)
                let (data, http) = try await transport(req)
                guard (200..<300).contains(http.statusCode) else {
                    // A reachable host that rejected us is authoritative — stop.
                    throw PairingEndpoints.mapError(status: http.statusCode, body: data)
                }
                let resp = try decode(CompletePairingResponse.self, data)
                return PairingHandshake(
                    baseURL: baseURL, sessionId: resp.sessionId,
                    requiresPin: resp.requiresPin, payload: payload)
            } catch let e as PairingError {
                throw e  // server-authoritative rejection — don't try other hosts
            } catch {
                lastError = error  // transport/connection failure — try next host
                continue
            }
        }
        if lastError is PairingError { throw lastError }
        throw PairingError.noReachableHost
    }

    /// Submit the PIN. Throws `PairingError` on any non-2xx (caller maps to UX).
    public func verifyPin(handshake: PairingHandshake, pin: String) async throws {
        let req = try PairingEndpoints.verifyPinRequest(
            baseURL: handshake.baseURL, sessionId: handshake.sessionId,
            pairingToken: handshake.payload.pairingToken, pin: pin)
        let (data, http): (Data, HTTPURLResponse)
        do {
            (data, http) = try await transport(req)
        } catch {
            throw PairingError.network
        }
        guard (200..<300).contains(http.statusCode) else {
            throw PairingEndpoints.mapError(status: http.statusCode, body: data)
        }
    }

    /// Poll `status` until the token is delivered (on the `approved` response),
    /// or a terminal state / attempt budget is hit.
    public func awaitToken(
        handshake: PairingHandshake,
        maxAttempts: Int = 60,
        delay: Duration = .milliseconds(800),
        sleep: @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) async throws -> MobileCredential {
        for attempt in 0..<max(1, maxAttempts) {
            if attempt > 0 { try await sleep(delay) }
            let status = try await pollOnce(handshake: handshake)
            switch status.state {
            case "approved":
                guard let token = status.sessionToken, let deviceId = status.deviceId else {
                    throw PairingError.tokenNotDelivered
                }
                return MobileCredential(deviceId: deviceId, sessionToken: token)
            case "consumed":
                // Token already delivered/lost — can't recover this session.
                throw PairingError.tokenNotDelivered
            case "denied":
                throw PairingError.denied
            case "expired":
                throw PairingError.expired
            default:
                continue  // awaiting_device / awaiting_approval
            }
        }
        throw PairingError.network
    }

    private func pollOnce(handshake: PairingHandshake) async throws -> PairingStatusResponse {
        let req = try PairingEndpoints.statusRequest(
            baseURL: handshake.baseURL, sessionId: handshake.sessionId,
            pairingToken: handshake.payload.pairingToken)
        let (data, http): (Data, HTTPURLResponse)
        do {
            (data, http) = try await transport(req)
        } catch {
            throw PairingError.network
        }
        guard (200..<300).contains(http.statusCode) else {
            throw PairingEndpoints.mapError(status: http.statusCode, body: data)
        }
        return try decode(PairingStatusResponse.self, data)
    }

    private func decode<T: Decodable>(_ type: T.Type, _ data: Data) throws -> T {
        do { return try JSONDecoder().decode(type, from: data) }
        catch { throw PairingError.decoding }
    }
}
