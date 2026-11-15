import AppKit
@preconcurrency import AVFoundation
import CoreGraphics
import Foundation

public enum PermissionState: String, Sendable, Equatable {
    case granted
    case denied
    case notDetermined
    case restricted

    public var isGranted: Bool { self == .granted }
}

/// TCC state for everything the recorder needs.
public struct PermissionSnapshot: Sendable, Equatable {
    public var screenRecording: PermissionState
    public var microphone: PermissionState

    public init(screenRecording: PermissionState, microphone: PermissionState) {
        self.screenRecording = screenRecording
        self.microphone = microphone
    }

    public var canRecordScreen: Bool {
        screenRecording.isGranted
    }

    /// Microphone is optional; only a blocker when the user enabled it.
    public func blocksRecording(capturesMicrophone: Bool) -> Bool {
        !canRecordScreen || (capturesMicrophone && (microphone == .denied || microphone == .restricted))
    }
}

public protocol PermissionPreflightProtocol: Sendable {
    func snapshot() -> PermissionSnapshot
    /// Shows the system Screen Recording prompt the first time; afterwards the
    /// user must enable access in System Settings.
    func requestScreenRecording() -> Bool
    func requestMicrophone() async -> Bool
}

/// `CGPreflightScreenCaptureAccess` + `AVCaptureDevice` audio authorization.
public struct PermissionPreflight: PermissionPreflightProtocol {
    public static let screenRecordingSettingsURL = URL(
        string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
    )!
    public static let microphoneSettingsURL = URL(
        string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
    )!

    public init() {}

    public func snapshot() -> PermissionSnapshot {
        PermissionSnapshot(
            screenRecording: CGPreflightScreenCaptureAccess() ? .granted : .denied,
            microphone: Self.microphoneState()
        )
    }

    public func requestScreenRecording() -> Bool {
        CGRequestScreenCaptureAccess()
    }

    public func requestMicrophone() async -> Bool {
        await AVCaptureDevice.requestAccess(for: .audio)
    }

    static func microphoneState() -> PermissionState {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return .granted
        case .denied: return .denied
        case .restricted: return .restricted
        case .notDetermined: return .notDetermined
        @unknown default: return .denied
        }
    }

    /// Deep link into System Settings › Privacy & Security.
    @MainActor
    public static func openSettings(_ url: URL) {
        NSWorkspace.shared.open(url)
    }
}
