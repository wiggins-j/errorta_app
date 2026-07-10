import Foundation
import Testing
@testable import ErrortaCompanionCore

private func ephemeralDefaults() -> UserDefaults {
    // A throwaway, UNIQUE suite so parallel tests don't collide or touch the
    // real app domain (a timestamp can repeat across same-millisecond runs).
    let name = "test.errorta.desktopstore.\(UUID().uuidString)"
    let d = UserDefaults(suiteName: name)!
    d.removePersistentDomain(forName: name)
    return d
}

private func record(_ id: String, host: String = "192.0.2.14") -> DesktopRecord {
    DesktopRecord(
        desktopId: id, displayName: "Mac \(id)",
        hostCandidates: [HostCandidate(kind: "lan", host: host, port: 8788)],
        tlsCertSha256: "abc")
}

@Test func storeRoundTripsRecordsAcrossLoads() {
    let defaults = ephemeralDefaults()
    DesktopStore(defaults: defaults).upsert(record("c1"))
    // A fresh store instance (simulating relaunch) still sees it.
    let reloaded = DesktopStore(defaults: defaults).load()
    #expect(reloaded.count == 1)
    #expect(reloaded.first?.desktopId == "c1")
    #expect(reloaded.first?.tlsCertSha256 == "abc")
}

@Test func upsertReplacesByDesktopId() {
    let defaults = ephemeralDefaults()
    let store = DesktopStore(defaults: defaults)
    store.upsert(record("c1", host: "192.0.2.14"))
    store.upsert(record("c1", host: "192.0.2.99"))
    let all = store.load()
    #expect(all.count == 1)
    #expect(all.first?.hostCandidates.first?.host == "192.0.2.99")
}

@Test func removeDropsTheRecord() {
    let defaults = ephemeralDefaults()
    let store = DesktopStore(defaults: defaults)
    store.upsert(record("c1"))
    store.upsert(record("c2"))
    store.remove(desktopId: "c1")
    let all = store.load()
    #expect(all.map(\.desktopId) == ["c2"])
}
