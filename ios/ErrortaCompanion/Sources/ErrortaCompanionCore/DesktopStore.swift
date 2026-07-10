import Foundation

/// F070 — persists the list of paired desktops so they survive app relaunch.
/// The *credential* (session token) lives in the Keychain
/// (`KeychainCredentialStore`); this stores only the non-secret record (name,
/// hosts, pinned cert fingerprint) in UserDefaults. Keyed by `desktopId`.
public final class DesktopStore: @unchecked Sendable {
    private let defaults: UserDefaults
    private let key = "errorta.pairedDesktops.v1"

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    public func load() -> [DesktopRecord] {
        guard let data = defaults.data(forKey: key) else { return [] }
        return (try? JSONDecoder().decode([DesktopRecord].self, from: data)) ?? []
    }

    public func save(_ records: [DesktopRecord]) {
        guard let data = try? JSONEncoder().encode(records) else { return }
        defaults.set(data, forKey: key)
    }

    /// Insert or replace by desktopId; returns the new full list.
    @discardableResult
    public func upsert(_ record: DesktopRecord) -> [DesktopRecord] {
        var all = load().filter { $0.desktopId != record.desktopId }
        all.append(record)
        save(all)
        return all
    }

    /// Remove a desktop's record; returns the new full list.
    @discardableResult
    public func remove(desktopId: String) -> [DesktopRecord] {
        let all = load().filter { $0.desktopId != desktopId }
        save(all)
        return all
    }
}
