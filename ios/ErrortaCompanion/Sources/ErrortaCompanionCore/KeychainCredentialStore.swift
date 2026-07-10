import Foundation
import Security

public struct MobileCredential: Codable, Equatable, Sendable {
    public let deviceId: String
    public let sessionToken: String

    public init(deviceId: String, sessionToken: String) {
        self.deviceId = deviceId
        self.sessionToken = sessionToken
    }
}

public enum KeychainCredentialError: Error, Equatable {
    case encodeFailed
    case decodeFailed
    case unhandledStatus(OSStatus)
}

public final class KeychainCredentialStore: @unchecked Sendable {
    private let service: String

    public init(service: String = "app.errorta.companion.credentials") {
        self.service = service
    }

    public func save(_ credential: MobileCredential, for desktopId: String) throws {
        guard let data = try? JSONEncoder().encode(credential) else {
            throw KeychainCredentialError.encodeFailed
        }
        try delete(desktopId: desktopId)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: desktopId,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw KeychainCredentialError.unhandledStatus(status)
        }
    }

    public func load(desktopId: String) throws -> MobileCredential? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: desktopId,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess else {
            throw KeychainCredentialError.unhandledStatus(status)
        }
        guard let data = item as? Data,
              let credential = try? JSONDecoder().decode(MobileCredential.self, from: data) else {
            throw KeychainCredentialError.decodeFailed
        }
        return credential
    }

    public func delete(desktopId: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: desktopId,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainCredentialError.unhandledStatus(status)
        }
    }
}
