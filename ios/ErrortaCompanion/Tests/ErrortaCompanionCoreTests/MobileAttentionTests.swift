import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func mobileAttentionResponseDecodesGenericAttentionState() throws {
    let json = """
    {
      "needs_attention": true,
      "attention_count": 1,
      "pending_decision_count": 1,
      "runs": [{
        "run_id": "run-1",
        "title": "Needs attention",
        "status": "running",
        "room_name": "Coding Council",
        "needs_attention": true,
        "attention_reasons": ["pending_decision"],
        "latest_attention_at": "2026-06-14T12:00:00Z",
        "pending_decision_count": 1
      }]
    }
    """

    let decoded = try JSONDecoder().decode(MobileAttentionResponse.self, from: Data(json.utf8))

    #expect(decoded.needsAttention == true)
    #expect(decoded.attentionCount == 1)
    #expect(decoded.runs.first?.attentionReasons == ["pending_decision"])
}
