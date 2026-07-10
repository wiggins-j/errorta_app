import Foundation

public struct MobileNotificationSettings: Codable, Equatable, Sendable {
    public let enabled: Bool
    public let includeFailedRuns: Bool
    public let includePendingApprovals: Bool

    public init(
        enabled: Bool = false,
        includeFailedRuns: Bool = true,
        includePendingApprovals: Bool = true
    ) {
        self.enabled = enabled
        self.includeFailedRuns = includeFailedRuns
        self.includePendingApprovals = includePendingApprovals
    }
}

public struct MobileNotificationCandidate: Equatable, Sendable {
    public let title: String
    public let body: String
    public let runId: String?
}

public enum MobileNotificationPlanner {
    public static func candidates(
        attention: MobileAttentionResponse,
        settings: MobileNotificationSettings
    ) -> [MobileNotificationCandidate] {
        guard settings.enabled else { return [] }
        var out: [MobileNotificationCandidate] = []
        if settings.includePendingApprovals, attention.pendingDecisionCount > 0 {
            out.append(
                MobileNotificationCandidate(
                    title: "Errorta needs approval",
                    body: "\(attention.pendingDecisionCount) pending decision(s)",
                    runId: nil
                )
            )
        }
        if settings.includeFailedRuns {
            for run in attention.runs where run.attentionReasons.contains("run_failed") {
                out.append(
                    MobileNotificationCandidate(
                        title: "Errorta run failed",
                        body: run.title,
                        runId: run.runId
                    )
                )
            }
        }
        return out
    }
}
