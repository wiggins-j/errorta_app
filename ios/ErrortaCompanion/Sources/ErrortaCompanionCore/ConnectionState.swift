import Foundation

public enum ConnectionState: String, Codable, Equatable, Sendable {
    case unpaired
    case pairingScanning = "pairing_scanning"
    case pairingExchanging = "pairing_exchanging"
    case pairedDisconnected = "paired_disconnected"
    case resolving
    case connecting
    case connected
    case authExpired = "auth_expired"
    case revoked
    case connectorDisabled = "connector_disabled"
    case versionIncompatible = "version_incompatible"
    case networkUnreachable = "network_unreachable"
}
