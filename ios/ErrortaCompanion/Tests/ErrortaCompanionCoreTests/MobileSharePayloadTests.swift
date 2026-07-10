import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func sharePayloadClassifiesHTTPSAsURLInboxItem() {
    let payload = MobileSharePayload(
        text: " https://example.com/spec ",
        title: "Spec",
        sourceApp: "com.apple.mobilesafari"
    )

    #expect(payload.kind == "url")
    #expect(payload.text == "https://example.com/spec")
    #expect(payload.inboxRequest.sourceApp == "com.apple.mobilesafari")
}

@Test func sharePayloadClassifiesPlainTextAsTextInboxItem() {
    let payload = MobileSharePayload(text: "Ask the council to inspect this.")

    #expect(payload.kind == "text")
    #expect(payload.inboxRequest.kind == "text")
}
