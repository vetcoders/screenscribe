import Foundation
import ScreenscribeCore
import Testing

@Suite("Bilingual EN + PL Localization Parity")
struct LocalizationParityTests {
    @Test("en.lproj and pl.lproj have 1:1 non-empty key parity and matching printf placeholders")
    func englishAndPolishKeyAndPlaceholderParity() throws {
        let en = L10n.table(for: "en")
        let pl = L10n.table(for: "pl")

        #expect(en.count >= 200)
        #expect(Set(en.keys) == Set(pl.keys))

        let specifiers = try NSRegularExpression(pattern: "%(?:\\d+\\$)?[@dDuUxXoOfeEgGcCsSpaAF]|%%|%(?:\\d+\\$)?\\.\\d+[fFeEgG]")
        for (key, enValue) in en {
            let plValue = try #require(pl[key])
            #expect(!enValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, "Empty EN value for \(key)")
            #expect(!plValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, "Empty PL value for \(key)")

            let enTokens = Self.extractSpecifiers(enValue, regex: specifiers)
            let plTokens = Self.extractSpecifiers(plValue, regex: specifiers)
            #expect(enTokens.sorted() == plTokens.sorted(), "Placeholder mismatch on key \(key): EN=\(enTokens) PL=\(plTokens)")
        }
    }

    private static func extractSpecifiers(_ text: String, regex: NSRegularExpression) -> [String] {
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        return regex.matches(in: text, range: range).compactMap { match in
            guard let swiftRange = Range(match.range, in: text) else { return nil }
            let token = String(text[swiftRange])
            if token == "%%" { return "%" }
            // Strip positional index (`%1$@` -> `%@`) so reordered Polish grammar passes cleanly.
            return token.replacingOccurrences(of: #"^%\d+\$"#, with: "%", options: .regularExpression)
        }
    }
}
