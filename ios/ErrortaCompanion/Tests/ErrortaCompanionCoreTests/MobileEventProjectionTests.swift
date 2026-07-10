import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func mobileEventProjectionDecodesToolCardsWithoutRawContent() throws {
    let json = """
    {
      "run": {
        "run_id": "run-1",
        "title": "Review parser",
        "status": "running",
        "room_name": "Coding Council",
        "updated_at": "2026-06-14T12:00:00Z",
        "needs_attention": true,
        "pending_decision_count": 1
      },
      "events": [{
        "event_id": "evt-1",
        "sequence": 3,
        "type": "tool_call_completed",
        "created_at": "2026-06-14T12:00:00Z",
        "actor": {"kind": "member", "id": "reviewer", "name": "Reviewer"},
        "body": {
          "type": "tool_call",
          "tool_id": "code_exec",
          "status": "completed",
          "summary": "Ran parser tests.",
          "content_sha256": "abc123",
          "artifact_count": 0,
          "decision_id": null
        },
        "mobile_visibility": "visible"
      }],
      "last_sequence": 3
    }
    """

    let decoded = try JSONDecoder().decode(MobileRunEventsResponse.self, from: Data(json.utf8))

    #expect(decoded.run.id == "run-1")
    #expect(decoded.events.first?.sequence == 3)
    if case .toolCall(let card) = decoded.events.first?.body {
        #expect(card.toolId == "code_exec")
        #expect(card.contentSha256 == "abc123")
    } else {
        Issue.record("expected tool call card")
    }
}

@Test func mobileEventProjectionDecodesSummaryBlocks() throws {
    let json = """
    {
      "event_id": "mobile-summary:0:101",
      "sequence": 100,
      "type": "mobile_summary",
      "created_at": "2026-06-14T12:00:00Z",
      "actor": {"kind": "system", "id": null, "name": "Errorta"},
      "body": {
        "type": "summary",
        "text": "100 earlier events hidden on mobile.",
        "event_count": 100
      },
      "mobile_visibility": "summary"
    }
    """

    let decoded = try JSONDecoder().decode(MobileEventProjection.self, from: Data(json.utf8))

    if case .summary(let text, let eventCount) = decoded.body {
        #expect(text.contains("earlier events"))
        #expect(eventCount == 100)
    } else {
        Issue.record("expected summary card")
    }
}
