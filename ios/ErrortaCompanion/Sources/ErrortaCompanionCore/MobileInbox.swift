import Foundation

public struct MobileInboxItem: Codable, Equatable, Identifiable, Sendable {
    public var id: String { inboxItemId }

    public let inboxItemId: String
    public let deviceId: String
    public let kind: String
    public let title: String?
    public let text: String
    public let sourceApp: String?
    public let createdAt: String
    public let status: String

    enum CodingKeys: String, CodingKey {
        case inboxItemId = "inbox_item_id"
        case deviceId = "device_id"
        case kind
        case title
        case text
        case sourceApp = "source_app"
        case createdAt = "created_at"
        case status
    }
}

public struct MobileInboxItemsResponse: Codable, Equatable, Sendable {
    public let items: [MobileInboxItem]
}

public struct MobileInboxCreateRequest: Codable, Equatable, Sendable {
    public let kind: String
    public let text: String
    public let title: String?
    public let sourceApp: String?

    public init(kind: String, text: String, title: String? = nil, sourceApp: String? = nil) {
        self.kind = kind
        self.text = text
        self.title = title
        self.sourceApp = sourceApp
    }

    enum CodingKeys: String, CodingKey {
        case kind
        case text
        case title
        case sourceApp = "source_app"
    }
}
