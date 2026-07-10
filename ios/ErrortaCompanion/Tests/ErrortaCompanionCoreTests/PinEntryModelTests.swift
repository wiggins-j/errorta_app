import Testing
@testable import ErrortaCompanionCore

@Test func pinNormalizesToSixDigits() {
    var m = PinEntryModel()
    m.setDigits("12ab34 56789")
    #expect(m.digits == "123456")
    #expect(m.isComplete)
    #expect(m.canSubmit)
}

@Test func pinIncompleteCannotSubmit() {
    var m = PinEntryModel()
    m.setDigits("123")
    #expect(!m.isComplete)
    #expect(!m.canSubmit)
}

@Test func pinMismatchSurfacesRemainingAndClears() {
    var m = PinEntryModel()
    m.setDigits("000000")
    let locked = m.apply(error: .pinMismatch(remaining: 3))
    #expect(!locked)
    #expect(m.attemptsRemaining == 3)
    #expect(m.digits == "")
    #expect(m.helperText.contains("3 tries left"))
}

@Test func pinLockedDisablesSubmit() {
    var m = PinEntryModel()
    m.setDigits("000000")
    let locked = m.apply(error: .pinLocked)
    #expect(locked)
    #expect(m.isLocked)
    #expect(!m.canSubmit)
}

@Test func pinMismatchZeroRemainingLocks() {
    var m = PinEntryModel()
    m.setDigits("000000")
    let locked = m.apply(error: .pinMismatch(remaining: 0))
    #expect(locked)
    #expect(m.isLocked)
}

@Test func pinSingularTryCopy() {
    var m = PinEntryModel()
    m.apply(error: .pinMismatch(remaining: 1))
    #expect(m.helperText.contains("1 try left"))
}
