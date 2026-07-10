import Foundation
import Testing
@testable import ErrortaCompanionCore

private func makeRecord(cert: String = "abc", host: String = "192.0.2.14", port: Int = 8788) -> DesktopRecord {
    DesktopRecord(
        desktopId: "mobconn_1", displayName: "Mac",
        hostCandidates: [HostCandidate(kind: "lan", host: host, port: port)],
        tlsCertSha256: cert)
}

private let cred = MobileCredential(deviceId: "dev1", sessionToken: "tok1")

private func http(_ url: URL?, _ code: Int) -> HTTPURLResponse {
    HTTPURLResponse(url: url ?? URL(string: "https://x")!, statusCode: code, httpVersion: nil, headerFields: nil)!
}
private func json(_ s: String) -> Data { Data(s.utf8) }

@Test func verifySucceedsAndParsesCapabilities() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { _ in
        (json(#"{"device_id":"dev1","capabilities":{"read_runs":true,"start_runs":false}}"#), http(nil, 200))
    }
    let caps = try await client.verify()
    #expect(caps.deviceId == "dev1")
    #expect(caps.can("read_runs"))
    #expect(!caps.can("start_runs"))
}

@Test func verifySendsAuthHeaders() async throws {
    let captured = Captured()
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        await captured.set(req)
        return (json(#"{"device_id":"dev1","capabilities":{}}"#), http(nil, 200))
    }
    _ = try await client.verify()
    let req = await captured.request
    #expect(req?.value(forHTTPHeaderField: "x-errorta-mobile-device-id") == "dev1")
    #expect(req?.value(forHTTPHeaderField: "authorization") == "Bearer tok1")
    #expect(req?.url?.absoluteString.contains("/mobile/v1/capabilities") == true)
    #expect(req?.url?.scheme == "https")  // cert present → pinned TLS
}

@Test func unauthorizedMapsToReauth() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { _ in
        (json(#"{"detail":"mobile_device_revoked"}"#), http(nil, 401))
    }
    await #expect(throws: DesktopError.unauthorized) { _ = try await client.verify() }
}

@Test func runsParsesProjection() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { _ in
        (json(#"{"runs":[{"run_id":"r1","title":"Pick a cache","status":"running","room_name":"Lab","updated_at":"t","needs_attention":false,"pending_decision_count":0}]}"#), http(nil, 200))
    }
    let runs = try await client.runs(status: "active")
    #expect(runs.count == 1)
    #expect(runs.first?.title == "Pick a cache")
    #expect(runs.first?.status == "running")
}

@Test func fallsOverToSecondHostOnTransportFailure() async throws {
    let record = DesktopRecord(
        desktopId: "c", displayName: "Mac",
        hostCandidates: [
            HostCandidate(kind: "lan", host: "10.0.0.9", port: 8788),
            HostCandidate(kind: "lan", host: "192.0.2.14", port: 8788),
        ], tlsCertSha256: "abc")
    let client = DesktopClient(record: record, credential: cred) { req in
        if req.url?.host == "10.0.0.9" { throw URLError(.cannotConnectToHost) }
        return (json(#"{"device_id":"dev1","capabilities":{}}"#), http(req.url, 200))
    }
    let caps = try await client.verify()
    #expect(caps.deviceId == "dev1")
}

@Test func sendMessagePostsToMessagesEndpoint() async throws {
    let captured = Captured()
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        await captured.set(req)
        return (json(#"{"accepted":true,"event":null,"client_request_id":null,"source_inbox_item_id":null}"#), http(req.url, 200))
    }
    try await client.sendMessage(runId: "r1", text: "optimize for memory")
    let req = await captured.request
    #expect(req?.httpMethod == "POST")
    #expect(req?.url?.absoluteString.contains("/mobile/v1/runs/r1/messages") == true)
    let bodyText = req?.httpBody.flatMap { String(data: $0, encoding: .utf8) } ?? ""
    #expect(bodyText.contains("optimize for memory"))
}

@Test func sendMessageWithoutCapabilityMapsForbidden() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        (json(#"{"detail":"mobile_capability_forbidden:send_messages"}"#), http(req.url, 403))
    }
    await #expect(throws: DesktopError.forbidden("mobile_capability_forbidden:send_messages")) {
        try await client.sendMessage(runId: "r1", text: "hi")
    }
}

@Test func sendMessageToTerminalRunMaps409() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        (json(#"{"detail":"mobile_run_terminal"}"#), http(req.url, 409))
    }
    await #expect(throws: DesktopError.conflict) {
        try await client.sendMessage(runId: "r1", text: "hi")
    }
}

@Test func pendingDecisionsParsesCards() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        (json(#"{"decisions":[{"decision_id":"d1","run_id":"r1","state":"pending","revision":1,"title":"Run code?","summary":"exec","risk":"high","phase":"impl","requester":{"kind":"member","id":"m1","name":"Gemma"},"decision_class":"code_exec","safe_details":{},"actions":{"can_approve":true,"can_deny":true,"requires_confirmation":true,"required_capability":"approve_code_exec"},"created_at":"t","expires_at":null}]}"#), http(req.url, 200))
    }
    let cards = try await client.pendingDecisions()
    #expect(cards.count == 1)
    #expect(cards.first?.actions.requiredCapability == "approve_code_exec")
}

@Test func resolveDecisionPostsApprove() async throws {
    let captured = Captured()
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        await captured.set(req)
        return (json("{}"), http(req.url, 200))
    }
    try await client.resolveDecision(runId: "r1", decisionId: "d1", approve: true)
    let req = await captured.request
    #expect(req?.httpMethod == "POST")
    #expect(req?.url?.absoluteString.contains("/pending-decisions/r1/d1/approve") == true)
}

@Test func resolveDecisionWithoutCapabilityForbidden() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        (json(#"{"detail":"mobile_capability_forbidden:approve_code_exec"}"#), http(req.url, 403))
    }
    await #expect(throws: DesktopError.forbidden("mobile_capability_forbidden:approve_code_exec")) {
        try await client.resolveDecision(runId: "r1", decisionId: "d1", approve: false)
    }
}

@Test func attentionParsesSummary() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        (json(#"{"needs_attention":true,"attention_count":1,"pending_decision_count":2,"runs":[{"run_id":"r1","title":"Pick cache","status":"running","room_name":"Lab","needs_attention":true,"attention_reasons":["pending_decision"],"latest_attention_at":"t","pending_decision_count":2}]}"#), http(req.url, 200))
    }
    let summary = try await client.attention()
    #expect(summary.needsAttention)
    #expect(summary.pendingDecisionCount == 2)
    #expect(summary.runs.first?.title == "Pick cache")
}

@Test func startRunPostsPrompt() async throws {
    let captured = Captured()
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        await captured.set(req)
        return (json("{}"), http(req.url, 200))
    }
    try await client.startRun(prompt: "Compare caches", roomId: "room1")
    let req = await captured.request
    #expect(req?.httpMethod == "POST")
    #expect(req?.url?.absoluteString.hasSuffix("/mobile/v1/runs") == true)
    let body = req?.httpBody.flatMap { String(data: $0, encoding: .utf8) } ?? ""
    #expect(body.contains("Compare caches"))
}

@Test func roomsParsesDesktopRoomSummaries() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        #expect(req.url?.absoluteString.hasSuffix("/mobile/v1/rooms") == true)
        return (json(#"{"rooms":[{"room_id":"room1","name":"Research","status_hint":"ready","updated_at":"2026-06-15T00:00:00Z","revision":7}]}"#), http(req.url, 200))
    }
    let rooms = try await client.rooms()
    #expect(rooms.count == 1)
    #expect(rooms[0].roomId == "room1")
    #expect(rooms[0].name == "Research")
    #expect(rooms[0].statusHint == "ready")
    #expect(rooms[0].revision == 7)
}

@Test func sendToInboxPosts() async throws {
    let captured = Captured()
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        await captured.set(req)
        return (json("{}"), http(req.url, 200))
    }
    try await client.sendToInbox(text: "https://example.com")
    let req = await captured.request
    #expect(req?.url?.absoluteString.hasSuffix("/mobile/v1/inbox-items") == true)
    #expect(req?.httpMethod == "POST")
}

@Test func runEventsParsesTranscript() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        (json(#"{"run":{},"events":[{"event_id":"e1","sequence":1,"type":"member_message","created_at":"t","actor":{"kind":"member","id":"m1","name":"Gemma"},"body":null,"mobile_visibility":"summary"}],"last_sequence":1}"#), http(req.url, 200))
    }
    let events = try await client.runEvents(runId: "r1")
    #expect(events.count == 1)
    #expect(events.first?.actor.name == "Gemma")
}

@Test func connectionInfoParsesAndBuildsCandidates() async throws {
    let client = DesktopClient(record: makeRecord(), credential: cred) { req in
        (json(#"{"hosts":[{"kind":"lan","host":"192.0.2.14"},{"kind":"tailscale","host":"100.64.1.2"}],"port":8788,"cert_sha256":"abc"}"#), http(req.url, 200))
    }
    let info = try await client.connectionInfo()
    #expect(info.hosts.count == 2)
    let cands = info.candidates()
    #expect(cands.contains { $0.kind == "tailscale" && $0.host == "100.64.1.2" && $0.port == 8788 })
    #expect(cands.contains { $0.kind == "lan" && $0.port == 8788 })
}

@Test func prefersTailscaleHostBeforeLan() async throws {
    // F071 — orderedHosts() ranks tailscale above lan, and DesktopClient tries
    // hosts in that order, so the first request goes to the tailscale host.
    let record = DesktopRecord(
        desktopId: "c", displayName: "Mac",
        hostCandidates: [
            HostCandidate(kind: "lan", host: "192.0.2.14", port: 8788),
            HostCandidate(kind: "tailscale", host: "100.64.1.2", port: 8788),
        ], tlsCertSha256: "abc")
    let first = Captured()
    let client = DesktopClient(record: record, credential: cred) { req in
        if await first.request == nil { await first.set(req) }
        return (json(#"{"device_id":"dev1","capabilities":{}}"#), http(req.url, 200))
    }
    _ = try await client.verify()
    #expect(await first.request?.url?.host == "100.64.1.2")
}

private actor Captured {
    var request: URLRequest?
    func set(_ r: URLRequest) { request = r }
}
