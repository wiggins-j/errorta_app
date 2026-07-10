import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func mobileInboxItemDecodesShareHandoff() throws {
    let json = """
    {
      "items": [{
        "inbox_item_id": "mob_inbox_1",
        "device_id": "mob_dev_1",
        "kind": "url",
        "title": "Docs",
        "text": "https://example.com/docs",
        "source_app": "com.apple.mobilesafari",
        "created_at": "2026-06-14T12:00:00Z",
        "status": "pending"
      }]
    }
    """

    let decoded = try JSONDecoder().decode(MobileInboxItemsResponse.self, from: Data(json.utf8))
    let item = try #require(decoded.items.first)

    #expect(item.kind == "url")
    #expect(item.text == "https://example.com/docs")
    #expect(item.sourceApp == "com.apple.mobilesafari")
}
