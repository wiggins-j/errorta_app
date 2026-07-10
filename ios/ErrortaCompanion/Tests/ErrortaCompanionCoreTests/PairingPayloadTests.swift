import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func pairingPayloadDecodesValidJSON() throws {
    let json = """
    {
      "schema": "errorta.mobile_pairing.v1",
      "connector_id": "mobconn_123",
      "desktop_name": "Dev Mac",
      "hosts": [{"kind": "tailscale", "host": "mac.tailnet.ts.net"}],
      "port": 8788,
      "tls_cert_sha256": "abc",
      "pairing_token": "token",
      "expires_at": "2026-06-14T12:05:00Z"
    }
    """
    let payload = try PairingPayload.decode(
        Data(json.utf8),
        now: ISO8601DateFormatter().date(from: "2026-06-14T12:00:00Z")!
    )

    #expect(payload.connectorId == "mobconn_123")
    #expect(payload.hosts.first?.kind == "tailscale")
}

@Test func pairingPayloadRejectsExpiredJSON() throws {
    let json = """
    {
      "schema": "errorta.mobile_pairing.v1",
      "connector_id": "mobconn_123",
      "desktop_name": "Dev Mac",
      "hosts": [{"kind": "lan", "host": "198.51.100.10"}],
      "port": 8788,
      "tls_cert_sha256": "abc",
      "pairing_token": "token",
      "expires_at": "2026-06-14T11:59:00Z"
    }
    """

    #expect(throws: PairingPayloadError.expired) {
        _ = try PairingPayload.decode(
            Data(json.utf8),
            now: ISO8601DateFormatter().date(from: "2026-06-14T12:00:00Z")!
        )
    }
}
