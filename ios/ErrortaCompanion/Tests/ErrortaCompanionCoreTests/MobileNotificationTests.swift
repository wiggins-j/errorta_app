import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func notificationPlannerStaysSilentWhenDisabled() {
    let attention = MobileAttentionResponse(
        needsAttention: true,
        attentionCount: 1,
        pendingDecisionCount: 1,
        runs: []
    )

    #expect(
        MobileNotificationPlanner.candidates(
            attention: attention,
            settings: MobileNotificationSettings(enabled: false)
        ).isEmpty
    )
}

@Test func notificationPlannerEmitsApprovalAndFailureCards() {
    let failed = MobileAttentionRun(
        runId: "run-1",
        title: "Failed coding council",
        status: "failed",
        roomName: "Coding Council",
        needsAttention: true,
        attentionReasons: ["run_failed"],
        latestAttentionAt: "2026-06-14T12:00:00Z",
        pendingDecisionCount: 0
    )
    let attention = MobileAttentionResponse(
        needsAttention: true,
        attentionCount: 2,
        pendingDecisionCount: 1,
        runs: [failed]
    )

    let candidates = MobileNotificationPlanner.candidates(
        attention: attention,
        settings: MobileNotificationSettings(enabled: true)
    )

    #expect(candidates.map(\.title) == ["Errorta needs approval", "Errorta run failed"])
    #expect(candidates.last?.runId == "run-1")
}
