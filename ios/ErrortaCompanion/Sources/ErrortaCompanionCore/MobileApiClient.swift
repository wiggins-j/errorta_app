import Foundation

public struct MobileRunProjection: Codable, Equatable, Identifiable, Sendable {
    public var id: String { runId }

    public let runId: String
    public let title: String
    public let status: String
    public let roomName: String?
    public let updatedAt: String
    public let needsAttention: Bool
    public let pendingDecisionCount: Int

    enum CodingKeys: String, CodingKey {
        case runId = "run_id"
        case title
        case status
        case roomName = "room_name"
        case updatedAt = "updated_at"
        case needsAttention = "needs_attention"
        case pendingDecisionCount = "pending_decision_count"
    }
}

public struct MobileRunsResponse: Codable, Equatable, Sendable {
    public let runs: [MobileRunProjection]
}

public struct MobileRoomProjection: Codable, Equatable, Identifiable, Sendable {
    public var id: String { roomId }

    public let roomId: String
    public let name: String
    public let statusHint: String
    public let updatedAt: String
    public let revision: Int

    enum CodingKeys: String, CodingKey {
        case roomId = "room_id"
        case name
        case statusHint = "status_hint"
        case updatedAt = "updated_at"
        case revision
    }
}

public struct MobileRoomsResponse: Codable, Equatable, Sendable {
    public let rooms: [MobileRoomProjection]
}

public struct MobileRunCreateRequest: Codable, Equatable, Sendable {
    public let prompt: String?
    public let roomId: String?
    public let corpusIds: [String]
    public let sourceInboxItemId: String?
    public let clientRequestId: String?
    public let dryFakeMembers: Bool

    public init(
        prompt: String? = nil,
        roomId: String? = nil,
        corpusIds: [String] = [],
        sourceInboxItemId: String? = nil,
        clientRequestId: String? = nil,
        dryFakeMembers: Bool = false
    ) {
        self.prompt = prompt
        self.roomId = roomId
        self.corpusIds = corpusIds
        self.sourceInboxItemId = sourceInboxItemId
        self.clientRequestId = clientRequestId
        self.dryFakeMembers = dryFakeMembers
    }

    enum CodingKeys: String, CodingKey {
        case prompt
        case roomId = "room_id"
        case corpusIds = "corpus_ids"
        case sourceInboxItemId = "source_inbox_item_id"
        case clientRequestId = "client_request_id"
        case dryFakeMembers = "dry_fake_members"
    }
}

public struct MobileRunCreateResponse: Codable, Equatable, Sendable {
    public let run: [String: JSONValue]
    public let events: [[String: JSONValue]]
    public let clientRequestId: String?
    public let sourceInboxItemId: String?

    enum CodingKeys: String, CodingKey {
        case run
        case events
        case clientRequestId = "client_request_id"
        case sourceInboxItemId = "source_inbox_item_id"
    }
}

public struct MobileFollowUpRequest: Codable, Equatable, Sendable {
    public let message: String?
    public let sourceInboxItemId: String?
    public let clientRequestId: String?

    public init(
        message: String? = nil,
        sourceInboxItemId: String? = nil,
        clientRequestId: String? = nil
    ) {
        self.message = message
        self.sourceInboxItemId = sourceInboxItemId
        self.clientRequestId = clientRequestId
    }

    enum CodingKeys: String, CodingKey {
        case message
        case sourceInboxItemId = "source_inbox_item_id"
        case clientRequestId = "client_request_id"
    }
}

public struct MobileFollowUpResponse: Codable, Equatable, Sendable {
    public let accepted: Bool
    public let event: [String: JSONValue]?
    public let clientRequestId: String?
    public let sourceInboxItemId: String?

    enum CodingKeys: String, CodingKey {
        case accepted
        case event
        case clientRequestId = "client_request_id"
        case sourceInboxItemId = "source_inbox_item_id"
    }
}

public struct MobileCancelRequest: Codable, Equatable, Sendable {
    public let reason: String?
    public let clientRequestId: String?

    public init(reason: String? = nil, clientRequestId: String? = nil) {
        self.reason = reason
        self.clientRequestId = clientRequestId
    }

    enum CodingKeys: String, CodingKey {
        case reason
        case clientRequestId = "client_request_id"
    }
}

public struct MobileCancelResponse: Codable, Equatable, Sendable {
    public let run: [String: JSONValue]
    public let event: [String: JSONValue]?
    public let clientRequestId: String?

    enum CodingKeys: String, CodingKey {
        case run
        case event
        case clientRequestId = "client_request_id"
    }
}

public enum JSONValue: Codable, Equatable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }
}

public enum MobileApiError: Error, Equatable {
    case connectorDisabled
    case authExpired
    case revoked
    case versionIncompatible
    case networkUnreachable
    case forbidden(String)
    case server(String)
    case decoding
}

public struct MobileApiClient: Sendable {
    public init() {}

    public func makeAuthorizedRequest<Body: Encodable>(
        baseURL: URL,
        path: String,
        method: String = "GET",
        deviceId: String,
        sessionToken: String,
        body: Body? = Optional<Data>.none
    ) throws -> URLRequest {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        let basePath = components?.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ?? ""
        let rawRequestPath = path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let requestComponents = URLComponents(string: rawRequestPath)
        let requestPath = requestComponents?.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ?? rawRequestPath
        components?.path = "/" + [basePath, requestPath].filter { !$0.isEmpty }.joined(separator: "/")
        components?.query = requestComponents?.query
        guard let url = components?.url else {
            throw MobileApiError.server("invalid_url")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue(deviceId, forHTTPHeaderField: "x-errorta-mobile-device-id")
        request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "authorization")
        request.setValue("application/json", forHTTPHeaderField: "accept")
        if let body {
            request.httpBody = try JSONEncoder().encode(body)
            request.setValue("application/json", forHTTPHeaderField: "content-type")
        }
        return request
    }

    public func makeRunsRequest(
        baseURL: URL,
        deviceId: String,
        sessionToken: String,
        status: String
    ) throws -> URLRequest {
        try makeAuthorizedRequest(
            baseURL: baseURL,
            path: "mobile/v1/runs?status=\(status)",
            deviceId: deviceId,
            sessionToken: sessionToken
        )
    }

    public func makeRoomsRequest(
        baseURL: URL,
        deviceId: String,
        sessionToken: String
    ) throws -> URLRequest {
        try makeAuthorizedRequest(
            baseURL: baseURL,
            path: "mobile/v1/rooms",
            deviceId: deviceId,
            sessionToken: sessionToken
        )
    }

    public func makeRunEventsRequest(
        baseURL: URL,
        deviceId: String,
        sessionToken: String,
        runId: String,
        afterSequence: Int
    ) throws -> URLRequest {
        try makeAuthorizedRequest(
            baseURL: baseURL,
            path: "mobile/v1/runs/\(runId)/events?after_sequence=\(afterSequence)",
            deviceId: deviceId,
            sessionToken: sessionToken
        )
    }

    public func makeCreateRunRequest(
        baseURL: URL,
        deviceId: String,
        sessionToken: String,
        body: MobileRunCreateRequest
    ) throws -> URLRequest {
        try makeAuthorizedRequest(
            baseURL: baseURL,
            path: "mobile/v1/runs",
            method: "POST",
            deviceId: deviceId,
            sessionToken: sessionToken,
            body: body
        )
    }

    public func makeFollowUpRequest(
        baseURL: URL,
        deviceId: String,
        sessionToken: String,
        runId: String,
        body: MobileFollowUpRequest
    ) throws -> URLRequest {
        try makeAuthorizedRequest(
            baseURL: baseURL,
            path: "mobile/v1/runs/\(runId)/messages",
            method: "POST",
            deviceId: deviceId,
            sessionToken: sessionToken,
            body: body
        )
    }

    public func makeCancelRequest(
        baseURL: URL,
        deviceId: String,
        sessionToken: String,
        runId: String,
        body: MobileCancelRequest
    ) throws -> URLRequest {
        try makeAuthorizedRequest(
            baseURL: baseURL,
            path: "mobile/v1/runs/\(runId)/cancel",
            method: "POST",
            deviceId: deviceId,
            sessionToken: sessionToken,
            body: body
        )
    }

    public func mapHTTPError(statusCode: Int, detail: String?) -> MobileApiError {
        switch (statusCode, detail ?? "") {
        case (503, "mobile_connector_disabled"):
            return .connectorDisabled
        case (401, "mobile_device_revoked"):
            return .revoked
        case (401, _):
            return .authExpired
        case (403, let detail) where detail.hasPrefix("mobile_capability_forbidden"):
            return .forbidden(detail)
        default:
            return .server(detail ?? "http_\(statusCode)")
        }
    }

    public func mapTransportError(_ error: Error) -> MobileApiError {
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain {
            return .networkUnreachable
        }
        return .server(nsError.localizedDescription)
    }
}
