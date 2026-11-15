import Foundation

/// Localized string lookup for every Screenscribe surface.
///
/// `Bundle.module` is internal to `ScreenscribeCore`, so the app target (and the
/// integrator's tests) resolve strings exclusively through this public API.
/// Keys live in `Resources/{en,pl}.lproj/Localizable.strings` with 1:1 parity.
public enum L10n {
    /// Localizations shipped inside the `ScreenscribeCore` resource bundle.
    public static let supportedLocalizations: [String] = ["en", "pl"]

    /// Resolves `key` for the user's preferred language (falls back to English).
    public static func string(_ key: String) -> String {
        Bundle.module.localizedString(forKey: key, value: nil, table: nil)
    }

    /// Resolves `key` and substitutes printf-style arguments (`%@`, `%d`, `%.1f`).
    public static func format(_ key: String, _ arguments: any CVarArg...) -> String {
        String(format: string(key), locale: Locale.current, arguments: arguments)
    }

    /// Resolves `key` for an explicit localization (`"en"` or `"pl"`).
    /// Returns `nil` when the key is missing from that table.
    public static func string(_ key: String, localization: String) -> String? {
        table(for: localization)[key]
    }

    /// Every key defined in the `Localizable.strings` table of `localization`.
    public static func keys(localization: String) -> Set<String> {
        Set(table(for: localization).keys)
    }

    /// Full key/value table for `localization`, read straight from the bundle.
    public static func table(for localization: String) -> [String: String] {
        guard
            let path = Bundle.module.path(
                forResource: "Localizable",
                ofType: "strings",
                inDirectory: nil,
                forLocalization: localization
            ),
            let dictionary = NSDictionary(contentsOfFile: path) as? [String: String]
        else {
            return [:]
        }
        return dictionary
    }
}

/// Display helpers shared by the HUD, workspace and settings.
public enum DisplayFormat {
    /// `HH:MM:SS` timecode (monospaced in the UI).
    public static func timecode(_ interval: TimeInterval) -> String {
        let total = max(0, Int(interval.rounded(.down)))
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let seconds = total % 60
        return String(format: "%02d:%02d:%02d", hours, minutes, seconds)
    }

    /// Human readable byte count (`1.2 GB`).
    public static func bytes(_ count: Int64) -> String {
        let formatter = ByteCountFormatter()
        formatter.countStyle = .file
        formatter.allowedUnits = [.useKB, .useMB, .useGB]
        return formatter.string(fromByteCount: count)
    }

    /// Minutes with one decimal place (`42.5`).
    public static func minutes(_ value: Double) -> String {
        String(format: "%.1f", locale: Locale.current, value)
    }

    /// Whole percent (`65%`).
    public static func percent(_ value: Double) -> String {
        "\(Int(max(0, min(100, value)).rounded()))%"
    }

    /// Short date + time for history rows.
    public static func dateTime(_ date: Date) -> String {
        date.formatted(date: .abbreviated, time: .shortened)
    }
}
