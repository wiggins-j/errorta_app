import Foundation

/// F068 — pure, testable PIN-entry logic (no SwiftUI). The view in the app
/// target renders the boxes; this owns validation, normalization, and mapping a
/// `PairingError` to user-facing attempt state.
public struct PinEntryModel: Equatable, Sendable {
    public static let length = 6

    public private(set) var digits: String
    /// Tries left, surfaced by the server on a mismatch. nil until first miss.
    public private(set) var attemptsRemaining: Int?
    public private(set) var isLocked: Bool

    public init(digits: String = "", attemptsRemaining: Int? = nil, isLocked: Bool = false) {
        self.digits = PinEntryModel.normalize(digits)
        self.attemptsRemaining = attemptsRemaining
        self.isLocked = isLocked
    }

    /// Keep only digits, cap at 6.
    public static func normalize(_ raw: String) -> String {
        String(raw.filter(\.isNumber).prefix(length))
    }

    public var isComplete: Bool { digits.count == PinEntryModel.length }

    /// Can the user submit right now?
    public var canSubmit: Bool { isComplete && !isLocked }

    /// Replace the entered digits (e.g. on each keystroke).
    public mutating func setDigits(_ raw: String) {
        digits = PinEntryModel.normalize(raw)
    }

    /// Fold a failed verify-pin attempt into the model. Returns true if the
    /// session is now terminally locked (caller should bail to the scanner).
    @discardableResult
    public mutating func apply(error: PairingError) -> Bool {
        switch error {
        case .pinMismatch(let remaining):
            attemptsRemaining = max(0, remaining)
            digits = ""
            isLocked = remaining <= 0
        case .pinLocked:
            attemptsRemaining = 0
            isLocked = true
        default:
            break
        }
        return isLocked
    }

    /// Human-readable hint for the current state.
    public var helperText: String {
        if isLocked { return "Too many attempts. Start a new pairing on your computer." }
        if let n = attemptsRemaining { return "Incorrect PIN — \(n) \(n == 1 ? "try" : "tries") left." }
        return "Enter the 6-digit PIN shown on your computer."
    }
}
