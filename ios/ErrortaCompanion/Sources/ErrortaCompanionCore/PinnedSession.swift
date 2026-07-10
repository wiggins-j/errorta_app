import Foundation

/// F066 — a URLSession whose server trust is decided by pinning the desktop's
/// self-signed leaf certificate (its DER SHA-256, matching F065's
/// `cert_der_sha256`). A self-signed cert fails the system trust evaluation
/// before any app code runs, so we MUST evaluate trust in a delegate; doing the
/// pin here means no ATS exception is needed.
public final class PinnedSessionDelegate: NSObject, URLSessionDelegate, Sendable {
    private let expectedSha256: String
    private let pinning = CertificatePinning()

    public init(expectedSha256: String) {
        self.expectedSha256 = expectedSha256.lowercased()
    }

    /// Testable core: does the presented leaf certificate match the pin?
    public func leafMatches(certificateData: Data) -> Bool {
        do {
            try pinning.validate(expectedSha256: expectedSha256, certificateData: certificateData)
            return true
        } catch {
            return false
        }
    }

    public func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard
            challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
            let trust = challenge.protectionSpace.serverTrust,
            let leaf = leafCertificateData(from: trust),
            leafMatches(certificateData: leaf)
        else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        // Pin matched — accept this specific leaf without system anchor trust.
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

/// Extract the leaf certificate's DER bytes from a server trust (the bytes
/// F065 fingerprints). Available across the OS versions the app targets.
public func leafCertificateData(from trust: SecTrust) -> Data? {
    if #available(iOS 15.0, macOS 12.0, *) {
        guard
            let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
            let leaf = chain.first
        else { return nil }
        return SecCertificateCopyData(leaf) as Data
    } else {
        guard let leaf = SecTrustGetCertificateAtIndex(trust, 0) else { return nil }
        return SecCertificateCopyData(leaf) as Data
    }
}

/// Build a URLSession that pins the desktop's leaf cert. Use this for every
/// request to a paired desktop.
public func makePinnedSession(expectedSha256: String) -> URLSession {
    URLSession(
        configuration: .ephemeral,
        delegate: PinnedSessionDelegate(expectedSha256: expectedSha256),
        delegateQueue: nil
    )
}
