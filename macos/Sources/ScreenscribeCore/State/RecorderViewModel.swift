import CoreGraphics
import Foundation
import Observation

/// What the source picker targets.
public enum CaptureSourceKind: String, CaseIterable, Identifiable, Sendable {
    case display
    case window
    case application

    public var id: String { rawValue }

    public var localizedTitle: String {
        switch self {
        case .display: return L10n.string("recorder.source.display")
        case .window: return L10n.string("recorder.source.window")
        case .application: return L10n.string("recorder.source.application")
        }
    }
}

/// Recorder HUD + record pane state: permissions, source picker, countdown,
/// pause/resume and the elapsed clock.
@MainActor
@Observable
public final class RecorderViewModel {
    public private(set) var state: CaptureState = .idle
    public private(set) var catalog: CaptureSourceCatalog = .empty
    public private(set) var microphones: [MicrophoneSource] = []
    public private(set) var permissions = PermissionSnapshot(screenRecording: .notDetermined, microphone: .notDetermined)
    public private(set) var isLoadingSources = false
    public private(set) var elapsed: TimeInterval = 0
    public private(set) var errorMessage: String?
    public private(set) var lastRecording: RecordingResult?

    public var sourceKind: CaptureSourceKind = .display
    public var selectedDisplayID: CGDirectDisplayID?
    public var selectedWindowID: CGWindowID?
    public var selectedApplicationID: pid_t?

    @ObservationIgnored private let capture: any ScreenCaptureServiceProtocol
    @ObservationIgnored private let permissionService: any PermissionPreflightProtocol
    @ObservationIgnored private weak var app: AppModel?
    @ObservationIgnored private var countdownTask: Task<Void, Never>?
    @ObservationIgnored private var clockTask: Task<Void, Never>?
    @ObservationIgnored private var eventsTask: Task<Void, Never>?
    @ObservationIgnored private var segmentStartedAt: Date?
    @ObservationIgnored private var accumulated: TimeInterval = 0
    @ObservationIgnored private var pendingAction: PendingAction = .nothing

    private enum PendingAction {
        case nothing
        case stop
        case discard
    }

    public init(capture: any ScreenCaptureServiceProtocol, permissions: any PermissionPreflightProtocol) {
        self.capture = capture
        self.permissionService = permissions
    }

    func attach(to app: AppModel) {
        self.app = app
    }

    /// Subscribes to engine events and reads permissions (launch).
    func start() {
        refreshPermissions()
        guard eventsTask == nil else { return }
        let events = capture.events()
        eventsTask = Task { [weak self] in
            for await event in events {
                self?.handle(event)
            }
        }
    }

    // MARK: Derived state

    public var selectedSource: CaptureSource? {
        switch sourceKind {
        case .display:
            let display = catalog.displays.first { $0.id == selectedDisplayID } ?? catalog.displays.first
            return display.map(CaptureSource.display)
        case .window:
            return catalog.windows.first { $0.id == selectedWindowID }.map(CaptureSource.window)
        case .application:
            guard let application = catalog.applications.first(where: { $0.id == selectedApplicationID }),
                  let display = catalog.displays.first(where: { $0.id == selectedDisplayID }) ?? catalog.displays.first
            else {
                return nil
            }
            return .application(application, display: display)
        }
    }

    public var canStart: Bool {
        state == .idle && selectedSource != nil
    }

    public var isRecordingOrPaused: Bool {
        state.isCapturing
    }

    public var countdownRemaining: Int? {
        if case .countingDown(let remaining) = state { return remaining }
        return nil
    }

    public var elapsedText: String {
        DisplayFormat.timecode(elapsed)
    }

    // MARK: Permissions & sources

    public func refreshPermissions() {
        permissions = permissionService.snapshot()
    }

    /// Triggers the system prompt the first time; afterwards only System
    /// Settings can grant access (the view offers the deep link).
    public func requestScreenRecordingAccess() {
        if !permissionService.requestScreenRecording() {
            errorMessage = L10n.string("recorder.permission.screen.settings")
        }
        refreshPermissions()
    }

    public func requestMicrophoneAccess() async {
        _ = await permissionService.requestMicrophone()
        refreshPermissions()
    }

    public func refreshSources() async {
        refreshPermissions()
        microphones = capture.microphones()
        guard permissions.canRecordScreen else {
            catalog = .empty
            return
        }
        isLoadingSources = true
        defer { isLoadingSources = false }
        do {
            let loaded = try await capture.availableSources()
            catalog = loaded
            if selectedDisplayID == nil || !loaded.displays.contains(where: { $0.id == selectedDisplayID }) {
                selectedDisplayID = loaded.displays.first?.id
            }
            if let windowID = selectedWindowID, !loaded.windows.contains(where: { $0.id == windowID }) {
                selectedWindowID = nil
            }
            if let applicationID = selectedApplicationID, !loaded.applications.contains(where: { $0.id == applicationID }) {
                selectedApplicationID = nil
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    // MARK: Recording

    /// Countdown (from settings), then start. The HUD and every other
    /// Screenscribe window are excluded from the capture by the engine.
    public func startRecording() {
        guard let app, state == .idle else { return }
        errorMessage = nil
        refreshPermissions()
        let configuration = app.configuration
        let options = CaptureOptions(configuration: configuration)
        guard permissions.canRecordScreen else {
            errorMessage = L10n.string("capture.error.permission")
            return
        }
        guard options.capturesAnyAudio else {
            // The hosted pipeline transcribes narration; silence has nothing to review.
            errorMessage = L10n.string("recorder.error.noaudio")
            return
        }
        guard let source = selectedSource else {
            errorMessage = L10n.string("capture.error.source")
            return
        }

        let seconds = configuration.countdownSeconds
        // Leave `.idle` synchronously so a double click cannot start twice.
        state = seconds > 0 ? .countingDown(seconds) : .preparing
        pendingAction = .nothing
        countdownTask = Task { [weak self] in
            guard let self else { return }
            // `cancelCountdown()` already reset the state; a cancelled task
            // must not write it again (a new countdown may be running).
            for remaining in stride(from: seconds, to: 0, by: -1) {
                guard !Task.isCancelled else { return }
                self.state = .countingDown(remaining)
                do {
                    try await Task.sleep(for: .seconds(1))
                } catch {
                    return
                }
            }
            guard !Task.isCancelled else { return }
            await self.beginCapture(source: source, options: options)
        }
    }

    private func beginCapture(source: CaptureSource, options: CaptureOptions) async {
        guard let app else {
            state = .idle
            return
        }
        state = .preparing
        do {
            let outputURL = try await app.workspace.queueStore.makeRecordingURL(fileExtension: options.container.fileExtension)
            let request = CaptureRequest(source: source, options: options, outputURL: outputURL)
            try await capture.startRecording(request)
            accumulated = 0
            elapsed = 0
            segmentStartedAt = Date()
            state = .recording
            startClock()
        } catch {
            state = .idle
            errorMessage = error.localizedDescription
            refreshPermissions()
        }
        countdownTask = nil

        // Stop/Discard pressed while the stream was still starting.
        let action = pendingAction
        pendingAction = .nothing
        switch action {
        case .nothing:
            break
        case .stop:
            await stopRecording()
        case .discard:
            await discardRecording()
        }
    }

    public func pause() async {
        guard state == .recording else { return }
        await capture.pauseRecording()
        if let segmentStartedAt {
            accumulated += Date().timeIntervalSince(segmentStartedAt)
        }
        segmentStartedAt = nil
        elapsed = accumulated
        state = .paused
    }

    public func resume() async {
        guard state == .paused else { return }
        await capture.resumeRecording()
        segmentStartedAt = Date()
        state = .recording
    }

    public func togglePause() async {
        if state == .paused {
            await resume()
        } else {
            await pause()
        }
    }

    /// Finalizes the movie and hands it to the upload queue. During the
    /// countdown it cancels; while the stream starts it stops right after.
    public func stopRecording() async {
        if case .countingDown = state {
            cancelCountdown()
            return
        }
        if state == .preparing {
            pendingAction = .stop
            return
        }
        guard state.isCapturing else { return }
        state = .finalizing
        stopClock()
        do {
            let result = try await capture.stopRecording()
            lastRecording = result
            state = .idle
            await app?.workspace.enqueueRecording(result)
        } catch {
            state = .idle
            errorMessage = error.localizedDescription
        }
        resetClock()
    }

    /// Stops and deletes the partial recording.
    public func discardRecording() async {
        if case .countingDown = state {
            cancelCountdown()
            return
        }
        if state == .preparing {
            pendingAction = .discard
            return
        }
        guard state.isCapturing else { return }
        state = .finalizing
        stopClock()
        await capture.cancelRecording()
        state = .idle
        resetClock()
    }

    public func cancelCountdown() {
        countdownTask?.cancel()
        countdownTask = nil
        if case .countingDown = state {
            state = .idle
        }
    }

    public func clearError() {
        errorMessage = nil
    }

    // MARK: Engine events

    private func handle(_ event: CaptureEvent) {
        switch event {
        case .stoppedUnexpectedly(let reason, let recording):
            stopClock()
            resetClock()
            state = .idle
            if let recording {
                lastRecording = recording
                errorMessage = L10n.format("recorder.stopped.saved", reason)
                Task { await app?.workspace.enqueueRecording(recording) }
            } else {
                errorMessage = L10n.format("recorder.stopped.lost", reason)
            }
        }
    }

    // MARK: Clock

    private func startClock() {
        clockTask?.cancel()
        clockTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if let segmentStartedAt = self.segmentStartedAt {
                    self.elapsed = self.accumulated + Date().timeIntervalSince(segmentStartedAt)
                }
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
    }

    private func stopClock() {
        if let segmentStartedAt {
            accumulated += Date().timeIntervalSince(segmentStartedAt)
        }
        segmentStartedAt = nil
        elapsed = accumulated
        clockTask?.cancel()
        clockTask = nil
    }

    private func resetClock() {
        accumulated = 0
        segmentStartedAt = nil
    }
}
