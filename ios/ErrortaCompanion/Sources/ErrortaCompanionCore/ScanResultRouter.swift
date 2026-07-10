import Foundation

/// F068 — pure, testable routing of a scanned QR string (or pasted JSON) into a
/// next-step outcome. Kept UI-free so the scanner view (app target) and the
/// manual-paste sheet share one decode/validate path.
public enum ScanRoute: Equatable, Sendable {
    /// A valid, in-date Errorta pairing payload — proceed to connect.
    case pair(PairingPayload)
    /// Decoded as our schema but the code has expired — tell the user to get a
    /// fresh one on the desktop; keep scanning.
    case expired
    /// A secure (QR) pairing requires a pinned cert; this payload had none.
    case insecure
    /// Not an Errorta pairing code at all — ignore and keep scanning.
    case notPairing
}

public enum ScanResultRouter {
    /// Route a scanned/pasted UTF-8 string.
    public static func route(_ text: String, now: Date = Date()) -> ScanRoute {
        route(Data(text.utf8), now: now)
    }

    /// Route raw bytes. `requireCert` defaults true for the scanner path: a QR
    /// pairing MUST carry a TLS fingerprint to pin. The manual-paste sheet may
    /// pass `requireCert: false` to allow the no-TLS loopback-dev payload.
    public static func route(_ data: Data, now: Date = Date(), requireCert: Bool = true) -> ScanRoute {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let payload = try? decoder.decode(PairingPayload.self, from: data) else {
            return .notPairing
        }
        // Schema/credential/hosts checks distinguish "not ours" from "expired".
        do {
            try payload.validate(now: now)
        } catch PairingPayloadError.expired {
            return .expired
        } catch {
            return .notPairing
        }
        if requireCert, (payload.tlsCertSha256 ?? "").isEmpty {
            return .insecure
        }
        return .pair(payload)
    }
}
