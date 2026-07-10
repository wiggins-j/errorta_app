import CryptoKit
import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func certificatePinningRejectsMismatch() {
    let data = Data("certificate".utf8)
    let expected = SHA256.hash(data: Data("other".utf8)).map {
        String(format: "%02x", $0)
    }.joined()

    #expect(throws: CertificatePinningError.mismatch) {
        try CertificatePinning().validate(expectedSha256: expected, certificateData: data)
    }
}
