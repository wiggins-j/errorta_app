import CryptoKit
import Foundation
import Testing
@testable import ErrortaCompanionCore

private func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

@Test func pinnedDelegateAcceptsMatchingLeaf() {
    // Stand-in for a leaf cert's DER bytes; the delegate pins the SHA-256 of
    // exactly these bytes (matching F065's cert_der_sha256).
    let der = Data("FAKE_LEAF_DER_BYTES".utf8)
    let delegate = PinnedSessionDelegate(expectedSha256: sha256Hex(der))
    #expect(delegate.leafMatches(certificateData: der))
}

@Test func pinnedDelegateRejectsMismatch() {
    let der = Data("FAKE_LEAF_DER_BYTES".utf8)
    let delegate = PinnedSessionDelegate(expectedSha256: sha256Hex(der))
    #expect(!delegate.leafMatches(certificateData: Data("DIFFERENT".utf8)))
}

@Test func pinnedDelegateIsCaseInsensitiveOnFingerprint() {
    let der = Data("abc".utf8)
    let delegate = PinnedSessionDelegate(expectedSha256: sha256Hex(der).uppercased())
    #expect(delegate.leafMatches(certificateData: der))
}
