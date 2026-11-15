import ScreenscribeCore
import SwiftUI

/// Screenscribe design tokens (dark, monospace-forward).
///
/// Content surfaces use these tokens; system chrome (sidebar, toolbar,
/// sheets) keeps the native material so Liquid Glass renders untouched.
enum ScreenscribeTheme {
    static let canvas = Color(hex: 0x0A0B0D)
    static let surface = Color(hex: 0x101216)
    static let elevated = Color(hex: 0x15181D)
    static let textPrimary = Color(hex: 0xF2F4F7)
    static let textSecondary = Color(hex: 0xC8CCD2)
    static let hairline = Color(hex: 0xF2F4F7).opacity(0.08)

    /// Semantic accents only: recording, success, warning, failure.
    static let recording = Color.red
    static let success = Color.green
    static let warning = Color.orange
    static let failure = Color.red

    static let cornerRadius: CGFloat = 12
    static let compactCornerRadius: CGFloat = 8
    static let spacing: CGFloat = 16

    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    static let monoBody = mono(13)
    static let monoCaption = mono(11)
    static let monoTitle = mono(20, weight: .semibold)
    static let timer = mono(28, weight: .medium)
}

extension Color {
    /// `0xRRGGBB` in the sRGB space.
    init(hex: UInt32, opacity: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: opacity
        )
    }
}

extension View {
    /// Dark content canvas for detail panes and the HUD.
    func screenscribeCanvas() -> some View {
        background(ScreenscribeTheme.canvas)
            .foregroundStyle(ScreenscribeTheme.textPrimary)
    }

    /// A card: Liquid Glass on macOS 26 (built with the Swift 6.2 SDK),
    /// the elevated token surface everywhere else.
    @ViewBuilder
    func screenscribeCard(cornerRadius: CGFloat = ScreenscribeTheme.cornerRadius) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            self
                .padding(ScreenscribeTheme.spacing)
                .glassEffect(.regular, in: .rect(cornerRadius: cornerRadius))
        } else {
            screenscribeTokenCard(cornerRadius: cornerRadius)
        }
        #else
        screenscribeTokenCard(cornerRadius: cornerRadius)
        #endif
    }

    private func screenscribeTokenCard(cornerRadius: CGFloat) -> some View {
        padding(ScreenscribeTheme.spacing)
            .background(ScreenscribeTheme.elevated, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .strokeBorder(ScreenscribeTheme.hairline)
            )
    }

    /// Primary action: system glass-prominent on macOS 26, bordered-prominent before.
    @ViewBuilder
    func screenscribePrimaryButton() -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            buttonStyle(.glassProminent)
        } else {
            buttonStyle(.borderedProminent)
        }
        #else
        buttonStyle(.borderedProminent)
        #endif
    }

    /// Secondary action: system glass on macOS 26, bordered before.
    @ViewBuilder
    func screenscribeSecondaryButton() -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            buttonStyle(.glass)
        } else {
            buttonStyle(.bordered)
        }
        #else
        buttonStyle(.bordered)
        #endif
    }

    /// Groups neighbouring glass cards so they sample each other correctly.
    @ViewBuilder
    func screenscribeGlassGroup(spacing: CGFloat = ScreenscribeTheme.spacing) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            GlassEffectContainer(spacing: spacing) { self }
        } else {
            self
        }
        #else
        self
        #endif
    }
}

/// Small rounded status label (queue state, plan).
struct StatusBadge: View {
    let text: String
    let tint: Color

    var body: some View {
        Text(text)
            .font(ScreenscribeTheme.monoCaption)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .foregroundStyle(tint)
            .background(tint.opacity(0.14), in: Capsule())
    }
}

extension UploadItemState {
    /// Semantic badge tint for the queue state.
    var tint: Color {
        switch self {
        case .draft, .pending: return ScreenscribeTheme.textSecondary
        case .uploading, .processing: return .accentColor
        case .completed: return ScreenscribeTheme.success
        case .failed: return ScreenscribeTheme.failure
        case .billingBlocked: return ScreenscribeTheme.warning
        }
    }

    var symbolName: String {
        switch self {
        case .draft: return "doc"
        case .pending: return "clock"
        case .uploading: return "arrow.up.circle"
        case .processing: return "gearshape.2"
        case .completed: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.triangle.fill"
        case .billingBlocked: return "creditcard"
        }
    }
}
