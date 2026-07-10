import Foundation

public struct MobileSharePayload: Equatable, Sendable {
    public let kind: String
    public let title: String?
    public let text: String
    public let sourceApp: String?

    public init(text: String, title: String? = nil, sourceApp: String? = nil) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        self.kind = Self.looksLikeURL(trimmed) ? "url" : "text"
        self.title = title
        self.text = trimmed
        self.sourceApp = sourceApp
    }

    public var inboxRequest: MobileInboxCreateRequest {
        MobileInboxCreateRequest(
            kind: kind,
            text: text,
            title: title,
            sourceApp: sourceApp
        )
    }

    private static func looksLikeURL(_ value: String) -> Bool {
        guard let components = URLComponents(string: value) else { return false }
        return components.scheme == "http" || components.scheme == "https"
    }
}
