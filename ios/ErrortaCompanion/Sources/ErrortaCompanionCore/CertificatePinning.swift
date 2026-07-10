import CryptoKit
import Foundation

public enum CertificatePinningError: Error, Equatable {
    case mismatch
}

public struct CertificatePinning: Sendable {
    public init() {}

    public func validate(expectedSha256: String, certificateData: Data) throws {
        let digest = SHA256.hash(data: certificateData)
        let actual = digest.map { String(format: "%02x", $0) }.joined()
        guard actual.lowercased() == expectedSha256.lowercased() else {
            throw CertificatePinningError.mismatch
        }
    }
}
