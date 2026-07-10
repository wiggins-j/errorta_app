import Foundation

public struct MobileActor: Codable, Equatable, Sendable {
    public let kind: String
    public let id: String?
    public let name: String
}

public struct MobileEventProjection: Codable, Equatable, Identifiable, Sendable {
    public var id: String { eventId }

    public let eventId: String
    public let sequence: Int
    public let type: String
    public let createdAt: String
    public let actor: MobileActor
    public let body: MobileEventBody?
    public let mobileVisibility: String

    enum CodingKeys: String, CodingKey {
        case eventId = "event_id"
        case sequence
        case type
        case createdAt = "created_at"
        case actor
        case body
        case mobileVisibility = "mobile_visibility"
    }
}

public enum MobileEventBody: Codable, Equatable, Sendable {
    case markdown(format: String, text: String)
    case toolCall(MobileToolCallCard)
    case pendingDecision(MobilePendingDecisionCard)
    case runStatus(status: String, reason: String?)
    case summary(text: String, eventCount: Int)
    case unknown

    private enum CodingKeys: String, CodingKey {
        case type
        case format
        case text
        case toolId = "tool_id"
        case status
        case summary
        case contentSha256 = "content_sha256"
        case artifactCount = "artifact_count"
        case decisionId = "decision_id"
        case phase
        case reasonCode = "reason_code"
        case reason
        case eventCount = "event_count"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let type = try container.decodeIfPresent(String.self, forKey: .type)
        if type == "tool_call" {
            self = .toolCall(try MobileToolCallCard(from: decoder))
            return
        }
        if type == "pending_decision" {
            self = .pendingDecision(try MobilePendingDecisionCard(from: decoder))
            return
        }
        if type == "run_status" {
            let status = try container.decode(String.self, forKey: .status)
            let reason = try container.decodeIfPresent(String.self, forKey: .reason)
            self = .runStatus(status: status, reason: reason)
            return
        }
        if type == "summary" {
            let text = try container.decode(String.self, forKey: .text)
            let eventCount = try container.decode(Int.self, forKey: .eventCount)
            self = .summary(text: text, eventCount: eventCount)
            return
        }
        if let format = try container.decodeIfPresent(String.self, forKey: .format),
           let text = try container.decodeIfPresent(String.self, forKey: .text) {
            self = .markdown(format: format, text: text)
            return
        }
        self = .unknown
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .markdown(let format, let text):
            try container.encode(format, forKey: .format)
            try container.encode(text, forKey: .text)
        case .toolCall(let card):
            try card.encode(to: encoder)
        case .pendingDecision(let card):
            try card.encode(to: encoder)
        case .runStatus(let status, let reason):
            try container.encode("run_status", forKey: .type)
            try container.encode(status, forKey: .status)
            try container.encodeIfPresent(reason, forKey: .reason)
        case .summary(let text, let eventCount):
            try container.encode("summary", forKey: .type)
            try container.encode(text, forKey: .text)
            try container.encode(eventCount, forKey: .eventCount)
        case .unknown:
            try container.encode("unknown", forKey: .type)
        }
    }
}

public struct MobileToolCallCard: Codable, Equatable, Sendable {
    public let type: String
    public let toolId: String?
    public let status: String
    public let summary: String?
    public let contentSha256: String?
    public let artifactCount: Int
    public let decisionId: String?

    enum CodingKeys: String, CodingKey {
        case type
        case toolId = "tool_id"
        case status
        case summary
        case contentSha256 = "content_sha256"
        case artifactCount = "artifact_count"
        case decisionId = "decision_id"
    }
}

public struct MobilePendingDecisionCard: Codable, Equatable, Sendable {
    public let type: String
    public let decisionId: String?
    public let phase: String?
    public let reasonCode: String?

    enum CodingKeys: String, CodingKey {
        case type
        case decisionId = "decision_id"
        case phase
        case reasonCode = "reason_code"
    }
}

public struct MobileRunEventsResponse: Codable, Equatable, Sendable {
    public let run: MobileRunProjection
    public let events: [MobileEventProjection]
    public let lastSequence: Int

    enum CodingKeys: String, CodingKey {
        case run
        case events
        case lastSequence = "last_sequence"
    }
}
