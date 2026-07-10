import Foundation

public struct DesktopRecord: Codable, Equatable, Identifiable, Sendable {
    public var id: String { desktopId }

    public let desktopId: String
    public var displayName: String
    public var hostCandidates: [HostCandidate]
    public var tlsCertSha256: String
    public var lastSuccessfulHost: String?

    enum CodingKeys: String, CodingKey {
        case desktopId = "desktop_id"
        case displayName = "display_name"
        case hostCandidates = "host_candidates"
        case tlsCertSha256 = "tls_cert_sha256"
        case lastSuccessfulHost = "last_successful_host"
    }

    public init(
        desktopId: String,
        displayName: String,
        hostCandidates: [HostCandidate],
        tlsCertSha256: String,
        lastSuccessfulHost: String? = nil
    ) {
        self.desktopId = desktopId
        self.displayName = displayName
        self.hostCandidates = hostCandidates
        self.tlsCertSha256 = tlsCertSha256
        self.lastSuccessfulHost = lastSuccessfulHost
    }

    public init(pairingPayload: PairingPayload) {
        self.init(
            desktopId: pairingPayload.connectorId,
            displayName: pairingPayload.desktopName,
            hostCandidates: pairingPayload.hosts.map {
                HostCandidate(kind: $0.kind, host: $0.host, port: $0.port ?? pairingPayload.port)
            },
            // Empty only on the no-TLS loopback-dev path (no cert to pin). A
            // QR-scanned payload always carries a non-empty fingerprint.
            tlsCertSha256: pairingPayload.tlsCertSha256 ?? ""
        )
    }

    public func orderedHosts() -> [HostCandidate] {
        let indexed = hostCandidates.enumerated()
        return indexed.sorted { lhs, rhs in
            let left = priority(lhs.element, originalIndex: lhs.offset)
            let right = priority(rhs.element, originalIndex: rhs.offset)
            if left.group != right.group {
                return left.group < right.group
            }
            return left.originalIndex < right.originalIndex
        }.map(\.element)
    }

    private func priority(
        _ candidate: HostCandidate,
        originalIndex: Int
    ) -> (group: Int, originalIndex: Int) {
        if let lastSuccessfulHost, candidate.host == lastSuccessfulHost {
            return (0, originalIndex)
        }
        switch candidate.kind {
        case "tailscale":
            return (1, originalIndex)
        case "lan", "loopback_dev":
            return (2, originalIndex)
        default:
            return (3, originalIndex)
        }
    }
}
