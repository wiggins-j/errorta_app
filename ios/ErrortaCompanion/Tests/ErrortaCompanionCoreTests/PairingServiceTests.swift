import Foundation
import Testing
@testable import ErrortaCompanionCore

// MARK: - Helpers

private func makePayload(
    cert: String? = "abc", token: String = "tok",
    hosts: [HostCandidate] = [HostCandidate(kind: "lan", host: "192.0.2.14", port: 8788)]
) -> PairingPayload {
    PairingPayload(
        connectorId: "mobconn_1", desktopName: "Mac", hosts: hosts, port: 8788,
        tlsCertSha256: cert, pairingToken: token,
        expiresAt: Date().addingTimeInterval(300))
}

private func http(_ url: URL?, _ code: Int) -> HTTPURLResponse {
    HTTPURLResponse(url: url ?? URL(string: "https://x")!, statusCode: code,
                    httpVersion: nil, headerFields: nil)!
}

private func json(_ s: String) -> Data { Data(s.utf8) }

/// Build a service whose transport routes by URL path.
private func service(
    _ handler: @escaping @Sendable (URLRequest) async throws -> (Data, HTTPURLResponse)
) -> PairingService {
    PairingService(transport: handler)
}

// MARK: - complete

@Test func completeReturnsHandshakeWithRequiresPin() async throws {
    let svc = service { req in
        (json(#"{"session_id":"s1","state":"awaiting_approval","requires_pin":true}"#),
         http(req.url, 200))
    }
    let hs = try await svc.connectAndComplete(payload: makePayload(), displayName: "iPhone")
    #expect(hs.sessionId == "s1")
    #expect(hs.requiresPin)
}

@Test func completeRequiresPinDefaultsTrueWhenAbsent() async throws {
    let svc = service { req in
        (json(#"{"session_id":"s1","state":"awaiting_approval"}"#), http(req.url, 200))
    }
    let hs = try await svc.connectAndComplete(payload: makePayload(), displayName: "iPhone")
    #expect(hs.requiresPin)  // fail-safe: never silently skip the PIN
}

@Test func completeFallsOverToSecondHostOnTransportFailure() async throws {
    let payload = makePayload(hosts: [
        HostCandidate(kind: "lan", host: "10.0.0.1", port: 8788),   // dead
        HostCandidate(kind: "lan", host: "192.0.2.14", port: 8788), // alive
    ])
    let svc = service { req in
        if req.url?.host == "10.0.0.1" { throw URLError(.cannotConnectToHost) }
        return (json(#"{"session_id":"s2","state":"awaiting_approval","requires_pin":true}"#),
                http(req.url, 200))
    }
    let hs = try await svc.connectAndComplete(payload: payload, displayName: "iPhone")
    #expect(hs.sessionId == "s2")
    #expect(hs.baseURL.host == "192.0.2.14")
}

@Test func completeServerRejectionStopsFallback() async throws {
    let calls = Counter()
    let payload = makePayload(hosts: [
        HostCandidate(kind: "lan", host: "10.0.0.1", port: 8788),
        HostCandidate(kind: "lan", host: "192.0.2.14", port: 8788),
    ])
    let svc = service { req in
        _ = await calls.next()
        return (json(#"{"detail":"pairing_token_expired"}"#), http(req.url, 400))
    }
    await #expect(throws: PairingError.expired) {
        _ = try await svc.connectAndComplete(payload: payload, displayName: "iPhone")
    }
    #expect(await calls.count == 1)  // authoritative rejection — no second host tried
}

// MARK: - verify-pin

@Test func verifyPinSucceeds() async throws {
    let svc = service { req in (json(#"{"state":"approved"}"#), http(req.url, 200)) }
    let hs = PairingHandshake(baseURL: URL(string: "https://192.0.2.14:8788")!,
                              sessionId: "s1", requiresPin: true, payload: makePayload())
    try await svc.verifyPin(handshake: hs, pin: "123456")
}

@Test func verifyPinMismatchMapsRemaining() async throws {
    let svc = service { req in
        (json(#"{"detail":"pairing_pin_mismatch","attempts_remaining":3}"#), http(req.url, 401))
    }
    let hs = PairingHandshake(baseURL: URL(string: "https://192.0.2.14:8788")!,
                              sessionId: "s1", requiresPin: true, payload: makePayload())
    await #expect(throws: PairingError.pinMismatch(remaining: 3)) {
        try await svc.verifyPin(handshake: hs, pin: "000000")
    }
}

@Test func verifyPinLockedMaps() async throws {
    let svc = service { req in
        (json(#"{"detail":"pairing_pin_locked"}"#), http(req.url, 429))
    }
    let hs = PairingHandshake(baseURL: URL(string: "https://192.0.2.14:8788")!,
                              sessionId: "s1", requiresPin: true, payload: makePayload())
    await #expect(throws: PairingError.pinLocked) {
        try await svc.verifyPin(handshake: hs, pin: "000000")
    }
}

// MARK: - status / token delivery

@Test func awaitTokenDeliversOnApprovedResponse() async throws {
    let svc = service { req in
        (json(#"{"state":"approved","session_token":"sektok","device_id":"dev9"}"#),
         http(req.url, 200))
    }
    let hs = PairingHandshake(baseURL: URL(string: "https://192.0.2.14:8788")!,
                              sessionId: "s1", requiresPin: true, payload: makePayload())
    let cred = try await svc.awaitToken(handshake: hs, sleep: { _ in })
    #expect(cred.deviceId == "dev9")
    #expect(cred.sessionToken == "sektok")
}

@Test func awaitTokenPollsThroughAwaitingThenApproved() async throws {
    let counter = Counter()
    let svc = service { req in
        let n = await counter.next()
        if n < 2 {
            return (json(#"{"state":"awaiting_approval"}"#), http(req.url, 200))
        }
        return (json(#"{"state":"approved","session_token":"t","device_id":"d"}"#),
                http(req.url, 200))
    }
    let hs = PairingHandshake(baseURL: URL(string: "https://192.0.2.14:8788")!,
                              sessionId: "s1", requiresPin: true, payload: makePayload())
    let cred = try await svc.awaitToken(handshake: hs, sleep: { _ in })
    #expect(cred.sessionToken == "t")
}

@Test func awaitTokenConsumedThrows() async throws {
    let svc = service { req in
        (json(#"{"state":"consumed","session_token":null,"device_id":"d"}"#), http(req.url, 200))
    }
    let hs = PairingHandshake(baseURL: URL(string: "https://192.0.2.14:8788")!,
                              sessionId: "s1", requiresPin: true, payload: makePayload())
    await #expect(throws: PairingError.tokenNotDelivered) {
        _ = try await svc.awaitToken(handshake: hs, sleep: { _ in })
    }
}

// MARK: - error mapping unit

@Test func mapErrorCoversContractCodes() {
    #expect(PairingEndpoints.mapError(status: 400, body: json(#"{"detail":"pairing_token_expired"}"#)) == .expired)
    #expect(PairingEndpoints.mapError(status: 409, body: json(#"{"detail":"pairing_not_awaiting_approval"}"#)) == .notAwaitingApproval)
    #expect(PairingEndpoints.mapError(status: 401, body: json(#"{"detail":"pairing_token_unknown"}"#)) == .tokenRejected)
    #expect(PairingEndpoints.mapError(status: 429, body: json(#"{"detail":"pairing_pin_locked"}"#)) == .pinLocked)
}

@Test func mapErrorDistinguishesRateLimitFromPinLock() {
    // complete/status flood guard must NOT read as a PIN lockout.
    #expect(PairingEndpoints.mapError(status: 429, body: json(#"{"detail":"pairing_rate_limited"}"#)) == .rateLimited)
    #expect(PairingEndpoints.mapError(status: 404, body: json(#"{"detail":"pairing_session_not_found"}"#)) == .sessionNotFound)
    #expect(PairingEndpoints.mapError(status: 409, body: json(#"{"detail":"pairing_pin_not_required"}"#)) == .pinNotRequired)
}

private actor Counter {
    private var n = 0
    var count: Int { n }
    func next() -> Int { defer { n += 1 }; return n }
}
