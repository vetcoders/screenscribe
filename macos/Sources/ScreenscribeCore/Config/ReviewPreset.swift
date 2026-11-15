import Foundation

/// Review domain presets, mirrored 1:1 from `screenscribe/presets/*.yaml`.
/// The raw value is the exact `preset` form field the hosted pipeline accepts.
public enum ReviewPreset: String, CaseIterable, Codable, Sendable, Identifiable {
    case programming
    case casual
    case medical
    case veterinary

    public static let fallback: ReviewPreset = .programming

    public var id: String { rawValue }

    /// Finding categories the hosted pipeline emits for this preset.
    public var categories: [String] {
        switch self {
        case .programming, .casual:
            return ["bug", "change", "ui", "performance", "accessibility", "other"]
        case .medical, .veterinary:
            return ["finding", "observation", "risk", "followup", "other"]
        }
    }

    public var localizedTitle: String {
        switch self {
        case .programming: return L10n.string("preset.programming.title")
        case .casual: return L10n.string("preset.casual.title")
        case .medical: return L10n.string("preset.medical.title")
        case .veterinary: return L10n.string("preset.veterinary.title")
        }
    }

    public var localizedDetail: String {
        switch self {
        case .programming: return L10n.string("preset.programming.detail")
        case .casual: return L10n.string("preset.casual.detail")
        case .medical: return L10n.string("preset.medical.detail")
        case .veterinary: return L10n.string("preset.veterinary.detail")
        }
    }
}

/// Spoken language hint sent as the `language` form field.
public enum RecordingLanguage: String, CaseIterable, Codable, Sendable, Identifiable {
    case auto
    case en
    case pl

    public var id: String { rawValue }

    public var localizedTitle: String {
        switch self {
        case .auto: return L10n.string("language.auto")
        case .en: return L10n.string("language.en")
        case .pl: return L10n.string("language.pl")
        }
    }
}

/// Visibility of the hosted review page (`visibility` form field).
public enum ReviewVisibility: String, CaseIterable, Codable, Sendable, Identifiable {
    case privateOnly = "private"
    case unlisted
    case team

    public var id: String { rawValue }

    public var localizedTitle: String {
        switch self {
        case .privateOnly: return L10n.string("visibility.private")
        case .unlisted: return L10n.string("visibility.unlisted")
        case .team: return L10n.string("visibility.team")
        }
    }
}
