import Foundation

public struct MobileAttentionRun: Codable, Equatable, Identifiable, Sendable {
    public var id: String { runId }

    public let runId: String
    public let title: String
    public let status: String
    public let roomName: String?
    public let needsAttention: Bool
    public let attentionReasons: [String]
    public let latestAttentionAt: String
    public let pendingDecisionCount: Int

    enum CodingKeys: String, CodingKey {
        case runId = "run_id"
        case title
        case status
        case roomName = "room_name"
        case needsAttention = "needs_attention"
        case attentionReasons = "attention_reasons"
        case latestAttentionAt = "latest_attention_at"
        case pendingDecisionCount = "pending_decision_count"
    }
}

public struct MobileAttentionResponse: Codable, Equatable, Sendable {
    public let needsAttention: Bool
    public let attentionCount: Int
    public let pendingDecisionCount: Int
    public let runs: [MobileAttentionRun]

    enum CodingKeys: String, CodingKey {
        case needsAttention = "needs_attention"
        case attentionCount = "attention_count"
        case pendingDecisionCount = "pending_decision_count"
        case runs
    }
}
