import Foundation
import Testing
@testable import ErrortaCompanionCore

@Test func apiErrorMappingHandlesKnownDesktopStates() {
    let client = MobileApiClient()

    #expect(client.mapHTTPError(statusCode: 503, detail: "mobile_connector_disabled") == .connectorDisabled)
    #expect(client.mapHTTPError(statusCode: 401, detail: "mobile_device_revoked") == .revoked)
    #expect(client.mapHTTPError(statusCode: 401, detail: "mobile_device_auth_required") == .authExpired)
    #expect(
        client.mapHTTPError(
            statusCode: 403,
            detail: "mobile_capability_forbidden:read_runs"
        ) == .forbidden("mobile_capability_forbidden:read_runs")
    )
}

@Test func apiClientBuildsAuthorizedCommandRequests() throws {
    let client = MobileApiClient()
    let baseURL = try #require(URL(string: "https://errorta-desktop.test:8770/api"))

    let list = try client.makeRunsRequest(
        baseURL: baseURL,
        deviceId: "mob_dev_1",
        sessionToken: "session",
        status: "active"
    )
    #expect(list.url?.absoluteString == "https://errorta-desktop.test:8770/api/mobile/v1/runs?status=active")
    #expect(list.value(forHTTPHeaderField: "x-errorta-mobile-device-id") == "mob_dev_1")
    #expect(list.value(forHTTPHeaderField: "authorization") == "Bearer session")

    let rooms = try client.makeRoomsRequest(
        baseURL: baseURL,
        deviceId: "mob_dev_1",
        sessionToken: "session"
    )
    #expect(rooms.url?.absoluteString == "https://errorta-desktop.test:8770/api/mobile/v1/rooms")
    #expect(rooms.httpMethod == "GET")

    let create = try client.makeCreateRunRequest(
        baseURL: baseURL,
        deviceId: "mob_dev_1",
        sessionToken: "session",
        body: MobileRunCreateRequest(prompt: "Start", roomId: "room-1", clientRequestId: "client-1")
    )
    #expect(create.httpMethod == "POST")
    let payload = try #require(create.httpBody)
    let json = try JSONSerialization.jsonObject(with: payload) as? [String: Any]
    #expect(json?["prompt"] as? String == "Start")
    #expect(json?["room_id"] as? String == "room-1")
    #expect(json?["client_request_id"] as? String == "client-1")
}
