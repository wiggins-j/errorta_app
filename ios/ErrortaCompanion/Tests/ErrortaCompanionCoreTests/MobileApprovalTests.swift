import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func mobileApprovalCardDecodesActionsAndSafeDetails() throws {
    let json = """
    {
      "decisions": [{
        "decision_id": "pd-1",
        "run_id": "run-1",
        "state": "pending",
        "revision": 1,
        "title": "Allow web fetch",
        "summary": "Reviewer requests web fetch remote egress: example.com.",
        "risk": "medium",
        "phase": "tool_call",
        "requester": {"kind": "member", "id": "reviewer", "name": "Reviewer"},
        "decision_class": "web_fetch_remote_egress",
        "safe_details": {"tool_id": "web_fetch", "domain": "example.com"},
        "actions": {
          "can_approve": false,
          "can_deny": false,
          "requires_confirmation": true,
          "required_capability": "approve_remote_egress"
        },
        "created_at": "2026-06-14T12:00:00Z",
        "expires_at": null
      }]
    }
    """

    let decoded = try JSONDecoder().decode(
        MobileApprovalInboxResponse.self,
        from: Data(json.utf8)
    )
    let card = try #require(decoded.decisions.first)

    #expect(card.title == "Allow web fetch")
    #expect(card.actions.canApprove == false)
    #expect(card.actions.requiresConfirmation == true)
    #expect(card.safeDetails["domain"] == "example.com")
}
