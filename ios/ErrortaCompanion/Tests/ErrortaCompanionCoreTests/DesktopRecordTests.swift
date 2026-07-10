import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func hostOrderingPrefersLastSuccessfulThenTailscaleThenLan() {
    let record = DesktopRecord(
        desktopId: "mobconn_123",
        displayName: "Dev Mac",
        hostCandidates: [
            HostCandidate(kind: "lan", host: "198.51.100.50"),
            HostCandidate(kind: "explicit_host", host: "manual.example"),
            HostCandidate(kind: "tailscale", host: "mac.tailnet.ts.net"),
        ],
        tlsCertSha256: "abc",
        lastSuccessfulHost: "manual.example"
    )

    let ordered = record.orderedHosts().map(\.host)

    #expect(ordered == [
        "manual.example",
        "mac.tailnet.ts.net",
        "198.51.100.50",
    ])
}
