import Foundation

/// Video codec used by the native `AVAssetWriter` encoder.
public enum VideoCodecChoice: String, CaseIterable, Codable, Sendable, Identifiable {
    case h264
    case hevc

    public var id: String { rawValue }

    /// Largest encodable edge. H.264 hardware encoders stop at 4096 px.
    public var maximumDimension: Int {
        switch self {
        case .h264: return 4096
        case .hevc: return 8192
        }
    }

    public var localizedTitle: String {
        switch self {
        case .h264: return L10n.string("codec.h264")
        case .hevc: return L10n.string("codec.hevc")
        }
    }
}

/// Container written to disk; both upload as `video/mp4` or `video/quicktime`.
public enum RecordingContainer: String, CaseIterable, Codable, Sendable, Identifiable {
    case mp4
    case mov

    public var id: String { rawValue }

    public var fileExtension: String { rawValue }

    public var mimeType: String {
        switch self {
        case .mp4: return "video/mp4"
        case .mov: return "video/quicktime"
        }
    }

    public var localizedTitle: String {
        switch self {
        case .mp4: return L10n.string("container.mp4")
        case .mov: return L10n.string("container.mov")
        }
    }
}

/// Non-sensitive user preferences. Tokens never live here — see `KeychainStore`.
/// There are deliberately no AI provider or API key fields: all processing runs
/// on the hosted Screenscribe backend under the user's subscription.
public struct AppConfiguration: Codable, Sendable, Equatable {
    public var cloudBaseURL: String
    public var defaultPreset: ReviewPreset
    public var defaultLanguage: RecordingLanguage
    public var defaultVisibility: ReviewVisibility
    public var additionalPrompt: String
    public var videoCodec: VideoCodecChoice
    public var container: RecordingContainer
    public var capturesSystemAudio: Bool
    public var capturesMicrophone: Bool
    public var microphoneDeviceID: String?
    public var showsCursor: Bool
    public var countdownSeconds: Int
    public var framesPerSecond: Int
    public var autoUploadRecordings: Bool

    /// Hosted Band 2 origin used when the user has not configured another one.
    public static let defaultCloudBaseURL = "https://screenscribe.vetcoders.io"
    /// Apple standard EULA, linked from the paywall (App Review 3.1.2).
    public static let termsOfUseURL = URL(string: "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/")!
    /// Privacy policy linked from the paywall and settings.
    public static let privacyPolicyURL = URL(string: "https://vetcoders.io/privacy")!
    /// App Store subscription management page.
    public static let manageSubscriptionsURL = URL(string: "https://apps.apple.com/account/subscriptions")!

    public static let allowedCountdowns: [Int] = [0, 3, 5, 10]
    public static let allowedFrameRates: [Int] = [15, 24, 30, 60]

    public init(
        cloudBaseURL: String = AppConfiguration.defaultCloudBaseURL,
        defaultPreset: ReviewPreset = .fallback,
        defaultLanguage: RecordingLanguage = .auto,
        defaultVisibility: ReviewVisibility = .unlisted,
        additionalPrompt: String = "",
        videoCodec: VideoCodecChoice = .h264,
        container: RecordingContainer = .mp4,
        capturesSystemAudio: Bool = true,
        capturesMicrophone: Bool = true,
        microphoneDeviceID: String? = nil,
        showsCursor: Bool = true,
        countdownSeconds: Int = 3,
        framesPerSecond: Int = 30,
        autoUploadRecordings: Bool = true
    ) {
        self.cloudBaseURL = cloudBaseURL
        self.defaultPreset = defaultPreset
        self.defaultLanguage = defaultLanguage
        self.defaultVisibility = defaultVisibility
        self.additionalPrompt = additionalPrompt
        self.videoCodec = videoCodec
        self.container = container
        self.capturesSystemAudio = capturesSystemAudio
        self.capturesMicrophone = capturesMicrophone
        self.microphoneDeviceID = microphoneDeviceID
        self.showsCursor = showsCursor
        self.countdownSeconds = countdownSeconds
        self.framesPerSecond = framesPerSecond
        self.autoUploadRecordings = autoUploadRecordings
    }

    public static let standard = AppConfiguration()

    enum CodingKeys: String, CodingKey {
        case cloudBaseURL
        case defaultPreset
        case defaultLanguage
        case defaultVisibility
        case additionalPrompt
        case videoCodec
        case container
        case capturesSystemAudio
        case capturesMicrophone
        case microphoneDeviceID
        case showsCursor
        case countdownSeconds
        case framesPerSecond
        case autoUploadRecordings
    }

    /// Tolerant decoding: preferences written by an older build keep working
    /// when new fields are added.
    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let fallback = AppConfiguration.standard
        cloudBaseURL = (try? container.decode(String.self, forKey: .cloudBaseURL)) ?? fallback.cloudBaseURL
        defaultPreset = (try? container.decode(ReviewPreset.self, forKey: .defaultPreset)) ?? fallback.defaultPreset
        defaultLanguage = (try? container.decode(RecordingLanguage.self, forKey: .defaultLanguage)) ?? fallback.defaultLanguage
        defaultVisibility = (try? container.decode(ReviewVisibility.self, forKey: .defaultVisibility)) ?? fallback.defaultVisibility
        additionalPrompt = (try? container.decode(String.self, forKey: .additionalPrompt)) ?? fallback.additionalPrompt
        videoCodec = (try? container.decode(VideoCodecChoice.self, forKey: .videoCodec)) ?? fallback.videoCodec
        self.container = (try? container.decode(RecordingContainer.self, forKey: .container)) ?? fallback.container
        capturesSystemAudio = (try? container.decode(Bool.self, forKey: .capturesSystemAudio)) ?? fallback.capturesSystemAudio
        capturesMicrophone = (try? container.decode(Bool.self, forKey: .capturesMicrophone)) ?? fallback.capturesMicrophone
        microphoneDeviceID = try? container.decodeIfPresent(String.self, forKey: .microphoneDeviceID)
        showsCursor = (try? container.decode(Bool.self, forKey: .showsCursor)) ?? fallback.showsCursor
        countdownSeconds = (try? container.decode(Int.self, forKey: .countdownSeconds)) ?? fallback.countdownSeconds
        framesPerSecond = (try? container.decode(Int.self, forKey: .framesPerSecond)) ?? fallback.framesPerSecond
        autoUploadRecordings = (try? container.decode(Bool.self, forKey: .autoUploadRecordings)) ?? fallback.autoUploadRecordings
    }
}

/// `UserDefaults`-backed persistence for `AppConfiguration`.
/// Holds only a suite name so the value stays `Sendable`.
public struct AppConfigurationStore: Sendable {
    public let suiteName: String?
    public let key: String

    public init(suiteName: String? = nil, key: String = "io.vetcoders.screenscribe.configuration") {
        self.suiteName = suiteName
        self.key = key
    }

    private var defaults: UserDefaults {
        if let suiteName, let suite = UserDefaults(suiteName: suiteName) {
            return suite
        }
        return UserDefaults.standard
    }

    public func load() -> AppConfiguration {
        guard let data = defaults.data(forKey: key),
              let configuration = try? JSONDecoder().decode(AppConfiguration.self, from: data)
        else {
            return AppConfiguration.standard
        }
        return configuration
    }

    public func save(_ configuration: AppConfiguration) {
        guard let data = try? JSONEncoder().encode(configuration) else { return }
        defaults.set(data, forKey: key)
    }

    public func reset() {
        defaults.removeObject(forKey: key)
    }
}
