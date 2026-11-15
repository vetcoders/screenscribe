import CoreGraphics
import Foundation

/// A capturable display (`SCDisplay`), sized in points.
public struct DisplaySource: Sendable, Equatable, Hashable, Identifiable {
    public var id: CGDirectDisplayID
    public var name: String
    public var width: Int
    public var height: Int

    public init(id: CGDirectDisplayID, name: String, width: Int, height: Int) {
        self.id = id
        self.name = name
        self.width = width
        self.height = height
    }
}

/// A capturable on-screen window (`SCWindow`).
public struct WindowSource: Sendable, Equatable, Hashable, Identifiable {
    public var id: CGWindowID
    public var title: String
    public var applicationName: String
    public var bundleIdentifier: String?
    public var width: Int
    public var height: Int

    public init(id: CGWindowID, title: String, applicationName: String, bundleIdentifier: String?, width: Int, height: Int) {
        self.id = id
        self.title = title
        self.applicationName = applicationName
        self.bundleIdentifier = bundleIdentifier
        self.width = width
        self.height = height
    }
}

/// A running application whose windows can be captured together.
public struct ApplicationSource: Sendable, Equatable, Hashable, Identifiable {
    public var id: pid_t
    public var name: String
    public var bundleIdentifier: String

    public init(id: pid_t, name: String, bundleIdentifier: String) {
        self.id = id
        self.name = name
        self.bundleIdentifier = bundleIdentifier
    }
}

/// What to record.
public enum CaptureSource: Sendable, Equatable, Hashable {
    case display(DisplaySource)
    case window(WindowSource)
    /// All windows of one app, composited on `display`.
    case application(ApplicationSource, display: DisplaySource)

    public var localizedTitle: String {
        switch self {
        case .display(let display):
            return display.name
        case .window(let window):
            return window.title.isEmpty ? window.applicationName : "\(window.applicationName) — \(window.title)"
        case .application(let application, _):
            return application.name
        }
    }
}

/// Snapshot of `SCShareableContent`, minus Screenscribe's own windows.
public struct CaptureSourceCatalog: Sendable, Equatable {
    public var displays: [DisplaySource]
    public var windows: [WindowSource]
    public var applications: [ApplicationSource]

    public init(displays: [DisplaySource] = [], windows: [WindowSource] = [], applications: [ApplicationSource] = []) {
        self.displays = displays
        self.windows = windows
        self.applications = applications
    }

    public static let empty = CaptureSourceCatalog()

    public var defaultSource: CaptureSource? {
        displays.first.map(CaptureSource.display)
    }
}

/// An audio input (`AVCaptureDevice.uniqueID`).
public struct MicrophoneSource: Sendable, Equatable, Hashable, Identifiable {
    public var id: String
    public var name: String

    public init(id: String, name: String) {
        self.id = id
        self.name = name
    }
}

/// Encoder and audio options for one recording.
public struct CaptureOptions: Sendable, Equatable {
    public var codec: VideoCodecChoice
    public var container: RecordingContainer
    public var capturesSystemAudio: Bool
    public var capturesMicrophone: Bool
    public var microphoneDeviceID: String?
    public var showsCursor: Bool
    public var framesPerSecond: Int

    public init(
        codec: VideoCodecChoice = .h264,
        container: RecordingContainer = .mp4,
        capturesSystemAudio: Bool = true,
        capturesMicrophone: Bool = true,
        microphoneDeviceID: String? = nil,
        showsCursor: Bool = true,
        framesPerSecond: Int = 30
    ) {
        self.codec = codec
        self.container = container
        self.capturesSystemAudio = capturesSystemAudio
        self.capturesMicrophone = capturesMicrophone
        self.microphoneDeviceID = microphoneDeviceID
        self.showsCursor = showsCursor
        self.framesPerSecond = min(60, max(1, framesPerSecond))
    }

    public init(configuration: AppConfiguration) {
        self.init(
            codec: configuration.videoCodec,
            container: configuration.container,
            capturesSystemAudio: configuration.capturesSystemAudio,
            capturesMicrophone: configuration.capturesMicrophone,
            microphoneDeviceID: configuration.microphoneDeviceID,
            showsCursor: configuration.showsCursor,
            framesPerSecond: configuration.framesPerSecond
        )
    }

    public var capturesAnyAudio: Bool {
        capturesSystemAudio || capturesMicrophone
    }
}

/// A fully specified recording job for the engine.
public struct CaptureRequest: Sendable, Equatable {
    public var source: CaptureSource
    public var options: CaptureOptions
    public var outputURL: URL
    /// Extra windows to hide from display capture. Every window of the
    /// Screenscribe process (HUD, workspace, sheets) is excluded regardless.
    public var excludedWindowIDs: [CGWindowID]

    public init(source: CaptureSource, options: CaptureOptions, outputURL: URL, excludedWindowIDs: [CGWindowID] = []) {
        self.source = source
        self.options = options
        self.outputURL = outputURL
        self.excludedWindowIDs = excludedWindowIDs
    }
}

/// A finalized movie file on disk.
public struct RecordingResult: Sendable, Equatable {
    public var fileURL: URL
    public var duration: TimeInterval
    public var fileSize: Int64
    public var width: Int
    public var height: Int
    public var codec: VideoCodecChoice
    public var hasSystemAudio: Bool
    public var hasMicrophone: Bool

    public init(
        fileURL: URL,
        duration: TimeInterval,
        fileSize: Int64,
        width: Int,
        height: Int,
        codec: VideoCodecChoice,
        hasSystemAudio: Bool,
        hasMicrophone: Bool
    ) {
        self.fileURL = fileURL
        self.duration = duration
        self.fileSize = fileSize
        self.width = width
        self.height = height
        self.codec = codec
        self.hasSystemAudio = hasSystemAudio
        self.hasMicrophone = hasMicrophone
    }
}

/// Recorder lifecycle as rendered by the HUD.
public enum CaptureState: Sendable, Equatable {
    case idle
    case preparing
    case countingDown(Int)
    case recording
    case paused
    case finalizing

    /// `true` while a recording exists that has not been finalized.
    public var isBusy: Bool {
        switch self {
        case .idle: return false
        default: return true
        }
    }

    public var isCapturing: Bool {
        self == .recording || self == .paused
    }
}

/// Out-of-band engine events.
public enum CaptureEvent: Sendable, Equatable {
    /// The stream ended without `stopRecording()` (display unplugged, the
    /// user clicked "Stop Sharing" in the menu bar, system error). The file
    /// written so far was finalized when possible.
    case stoppedUnexpectedly(reason: String, recording: RecordingResult?)
}

public enum CaptureError: Error, Equatable, Sendable, LocalizedError {
    case permissionDenied
    case microphonePermissionDenied
    case sourceUnavailable
    case alreadyRecording
    case notRecording
    case writerFailed(String)
    case streamFailed(String)
    case noFramesCaptured

    public var errorDescription: String? {
        switch self {
        case .permissionDenied: return L10n.string("capture.error.permission")
        case .microphonePermissionDenied: return L10n.string("capture.error.microphone")
        case .sourceUnavailable: return L10n.string("capture.error.source")
        case .alreadyRecording: return L10n.string("capture.error.busy")
        case .notRecording: return L10n.string("capture.error.idle")
        case .writerFailed(let message): return L10n.format("capture.error.writer", message)
        case .streamFailed(let message): return L10n.format("capture.error.stream", message)
        case .noFramesCaptured: return L10n.string("capture.error.noframes")
        }
    }
}
