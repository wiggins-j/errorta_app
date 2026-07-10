import Foundation

public struct MobileDecisionActions: Codable, Equatable, Sendable {
    public let canApprove: Bool
    public let canDeny: Bool
    public let requiresConfirmation: Bool
    public let requiredCapability: String

    enum CodingKeys: String, CodingKey {
        case canApprove = "can_approve"
        case canDeny = "can_deny"
        case requiresConfirmation = "requires_confirmation"
        case requiredCapability = "required_capability"
    }
}

public struct MobileApprovalCard: Codable, Equatable, Identifiable, Sendable {
    public var id: String { decisionId }

    public let decisionId: String
    public let runId: String
    public let state: String
    public let revision: Int
    public let title: String
    public let summary: String
    public let risk: String
    public let phase: String
    public let requester: MobileActor
    public let decisionClass: String
    public let safeDetails: [String: String]
    public let actions: MobileDecisionActions
    public let createdAt: String
    public let expiresAt: String?

    enum CodingKeys: String, CodingKey {
        case decisionId = "decision_id"
        case runId = "run_id"
        case state
        case revision
        case title
        case summary
        case risk
        case phase
        case requester
        case decisionClass = "decision_class"
        case safeDetails = "safe_details"
        case actions
        case createdAt = "created_at"
        case expiresAt = "expires_at"
    }
}

public struct MobileApprovalInboxResponse: Codable, Equatable, Sendable {
    public let decisions: [MobileApprovalCard]
}
