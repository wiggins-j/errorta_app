import Foundation
import Testing
@testable import ErrortaCompanionCore

private func payloadJSON(
    cert: String? = "1253f9ce", token: String = "tok",
    expiresOffset: TimeInterval = 300
) -> String {
    let expires = ISO8601DateFormatter().string(from: Date().addingTimeInterval(expiresOffset))
    let certField = cert.map { "\"tls_cert_sha256\":\"\($0)\"," } ?? ""
    return """
    {"schema":"errorta.mobile_pairing.v1","connector_id":"mobconn_1",\
    "desktop_name":"Mac","hosts":[{"kind":"lan","host":"192.0.2.14"}],\
    "port":8788,\(certField)"pairing_token":"\(token)","expires_at":"\(expires)"}
    """
}

@Test func routerAcceptsValidPayload() {
    if case .pair(let p) = ScanResultRouter.route(payloadJSON()) {
        #expect(p.connectorId == "mobconn_1")
        #expect(p.tlsCertSha256 == "1253f9ce")
    } else {
        Issue.record("expected .pair")
    }
}

@Test func routerFlagsExpired() {
    #expect(ScanResultRouter.route(payloadJSON(expiresOffset: -10)) == .expired)
}

@Test func routerFlagsInsecureWhenNoCert() {
    // No cert + scanner path (requireCert default true) → insecure.
    #expect(ScanResultRouter.route(payloadJSON(cert: nil)) == .insecure)
}

@Test func routerAllowsNoCertWhenCertNotRequired() {
    // Manual-paste path may accept the no-TLS loopback-dev payload.
    if case .pair = ScanResultRouter.route(Data(payloadJSON(cert: nil).utf8), requireCert: false) {
        // ok
    } else {
        Issue.record("expected .pair when requireCert == false")
    }
}

@Test func routerIgnoresNonPairingText() {
    #expect(ScanResultRouter.route("https://example.com/not-a-pairing") == .notPairing)
    #expect(ScanResultRouter.route("{\"schema\":\"something.else\"}") == .notPairing)
}
