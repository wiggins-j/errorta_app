import Foundation

public struct HostCandidate: Codable, Equatable, Hashable, Sendable {
    public let kind: String
    public let host: String
    public let port: Int?

    public init(kind: String, host: String, port: Int? = nil) {
        self.kind = kind
        self.host = host
        self.port = port
    }
}

public struct PairingPayload: Codable, Equatable, Sendable {
    public static let schema = "errorta.mobile_pairing.v1"

    public let schema: String
    public let connectorId: String
    public let desktopName: String
    public let hosts: [HostCandidate]
    public let port: Int
    // Optional on the wire: null only on the no-TLS loopback-dev manual-paste
    // path (which has no QR). A QR-scanned payload always carries it; the
    // scanner rejects a null-cert scan. See F067/F068.
    public let tlsCertSha256: String?
    public let pairingToken: String
    public let expiresAt: Date

    enum CodingKeys: String, CodingKey {
        case schema
        case connectorId = "connector_id"
        case desktopName = "desktop_name"
        case hosts
        case port
        case tlsCertSha256 = "tls_cert_sha256"
        case pairingToken = "pairing_token"
        case expiresAt = "expires_at"
    }

    public init(
        schema: String = PairingPayload.schema,
        connectorId: String,
        desktopName: String,
        hosts: [HostCandidate],
        port: Int,
        tlsCertSha256: String?,
        pairingToken: String,
        expiresAt: Date
    ) {
        self.schema = schema
        self.connectorId = connectorId
        self.desktopName = desktopName
        self.hosts = hosts
        self.port = port
        self.tlsCertSha256 = tlsCertSha256
        self.pairingToken = pairingToken
        self.expiresAt = expiresAt
    }

    public static func decode(_ data: Data, now: Date = Date()) throws -> PairingPayload {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let payload = try decoder.decode(PairingPayload.self, from: data)
        try payload.validate(now: now)
        return payload
    }

    public func validate(now: Date = Date()) throws {
        guard schema == PairingPayload.schema else {
            throw PairingPayloadError.unsupportedSchema
        }
        guard !connectorId.isEmpty, !pairingToken.isEmpty else {
            throw PairingPayloadError.missingCredential
        }
        guard !hosts.isEmpty else {
            throw PairingPayloadError.noHosts
        }
        guard expiresAt > now else {
            throw PairingPayloadError.expired
        }
    }
}

public enum PairingPayloadError: Error, Equatable {
    case unsupportedSchema
    case missingCredential
    case noHosts
    case expired
}
