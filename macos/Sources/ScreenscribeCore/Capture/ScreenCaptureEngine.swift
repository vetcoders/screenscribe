import AppKit
@preconcurrency import AVFoundation
import CoreGraphics
import CoreMedia
import Foundation
@preconcurrency import ScreenCaptureKit

/// Native screen + audio recorder. No ffmpeg, no helper processes: frames
/// come from ScreenCaptureKit and are encoded by `AVAssetWriter` in-process.
public protocol ScreenCaptureServiceProtocol: Sendable {
    /// Displays, windows and apps that can be recorded (own windows excluded).
    func availableSources() async throws -> CaptureSourceCatalog
    func microphones() -> [MicrophoneSource]
    func startRecording(_ request: CaptureRequest) async throws
    func pauseRecording() async
    func resumeRecording() async
    func stopRecording() async throws -> RecordingResult
    /// Stops and deletes the partial file.
    func cancelRecording() async
    func events() -> AsyncStream<CaptureEvent>
}

// MARK: - Writer

/// `AVAssetWriter` wrapper. Every method runs on the engine's capture queue;
/// that serial confinement is what makes the `@unchecked Sendable` sound.
final class RecordingWriter: @unchecked Sendable {
    let outputURL: URL
    let codec: VideoCodecChoice
    let width: Int
    let height: Int
    let hasMicrophone: Bool
    let hasSystemAudio: Bool

    private let writer: AVAssetWriter
    private let videoInput: AVAssetWriterInput
    private let microphoneInput: AVAssetWriterInput?
    private let systemAudioInput: AVAssetWriterInput?

    private var sessionStartTime: CMTime = .invalid
    private var lastVideoTime: CMTime = .invalid
    private var lastMediaTime: CMTime = .invalid
    private var pauseOffset: CMTime = .zero
    private var pauseStartedAt: CMTime?
    private var isFinished = false
    private(set) var framesWritten = 0

    init(
        outputURL: URL,
        fileType: AVFileType,
        codec: VideoCodecChoice,
        width: Int,
        height: Int,
        framesPerSecond: Int,
        includeMicrophone: Bool,
        includeSystemAudio: Bool
    ) throws {
        let assetWriter: AVAssetWriter
        do {
            assetWriter = try AVAssetWriter(outputURL: outputURL, fileType: fileType)
        } catch {
            throw CaptureError.writerFailed(error.localizedDescription)
        }
        // Fragments every 10 s: a crash or power loss leaves a playable file.
        assetWriter.movieFragmentInterval = CMTime(seconds: 10, preferredTimescale: 600)
        assetWriter.shouldOptimizeForNetworkUse = true

        let fps = max(1, framesPerSecond)
        let pixelRate = Double(width) * Double(height) * Double(fps)
        let bitsPerPixel = codec == .hevc ? 0.045 : 0.07
        let bitrate = Int(min(40_000_000, max(2_000_000, pixelRate * bitsPerPixel)))
        var compression: [String: Any] = [
            AVVideoAverageBitRateKey: bitrate,
            AVVideoExpectedSourceFrameRateKey: fps,
            AVVideoMaxKeyFrameIntervalKey: fps * 2,
            AVVideoAllowFrameReorderingKey: false,
        ]
        if codec == .h264 {
            compression[AVVideoProfileLevelKey] = AVVideoProfileLevelH264HighAutoLevel
        }
        let videoSettings: [String: Any] = [
            AVVideoCodecKey: codec == .hevc ? AVVideoCodecType.hevc : AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: compression,
            AVVideoColorPropertiesKey: [
                AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
                AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2,
            ],
        ]
        let video = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
        video.expectsMediaDataInRealTime = true
        guard assetWriter.canAdd(video) else {
            throw CaptureError.writerFailed("video input rejected")
        }
        assetWriter.add(video)

        // Both audio tracks are stereo AAC and the microphone track is added
        // first: ffmpeg's default stream selection (most channels, then
        // lowest index) then picks the narration for transcription.
        let audioSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey: 160_000,
        ]
        var microphone: AVAssetWriterInput?
        if includeMicrophone {
            let input = AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)
            input.expectsMediaDataInRealTime = true
            guard assetWriter.canAdd(input) else {
                throw CaptureError.writerFailed("microphone input rejected")
            }
            assetWriter.add(input)
            microphone = input
        }
        var systemAudio: AVAssetWriterInput?
        if includeSystemAudio {
            let input = AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)
            input.expectsMediaDataInRealTime = true
            guard assetWriter.canAdd(input) else {
                throw CaptureError.writerFailed("system audio input rejected")
            }
            assetWriter.add(input)
            systemAudio = input
        }

        guard assetWriter.startWriting() else {
            throw CaptureError.writerFailed(assetWriter.error?.localizedDescription ?? "startWriting failed")
        }

        self.outputURL = outputURL
        self.codec = codec
        self.width = width
        self.height = height
        self.hasMicrophone = includeMicrophone
        self.hasSystemAudio = includeSystemAudio
        self.writer = assetWriter
        self.videoInput = video
        self.microphoneInput = microphone
        self.systemAudioInput = systemAudio
    }

    var isPaused: Bool {
        pauseStartedAt != nil
    }

    /// `hostTime` is on the host clock, the same timebase as ScreenCaptureKit
    /// sample timestamps.
    func pause(at hostTime: CMTime) {
        guard !isFinished, pauseStartedAt == nil else { return }
        pauseStartedAt = hostTime
    }

    /// Accumulates the paused gap so timestamps after resume continue
    /// seamlessly from the last written frame.
    func resume(at hostTime: CMTime) {
        guard let started = pauseStartedAt else { return }
        let gap = hostTime - started
        if gap.isValid, gap > .zero {
            pauseOffset = pauseOffset + gap
        }
        pauseStartedAt = nil
    }

    func appendVideo(_ sampleBuffer: CMSampleBuffer) {
        guard !isFinished, !isPaused, writer.status == .writing else { return }
        guard CMSampleBufferIsValid(sampleBuffer), CMSampleBufferGetImageBuffer(sampleBuffer) != nil else { return }
        let sourceTime = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        guard sourceTime.isValid else { return }
        if !sessionStartTime.isValid {
            writer.startSession(atSourceTime: sourceTime)
            sessionStartTime = sourceTime
        }
        let time = sourceTime - pauseOffset
        if lastVideoTime.isValid, time <= lastVideoTime { return }
        guard videoInput.isReadyForMoreMediaData,
              let buffer = Self.retimed(sampleBuffer, by: pauseOffset)
        else {
            return
        }
        if videoInput.append(buffer) {
            lastVideoTime = time
            noteMediaTime(time)
            framesWritten += 1
        }
    }

    func appendAudio(_ sampleBuffer: CMSampleBuffer, microphone: Bool) {
        guard let input = microphone ? microphoneInput : systemAudioInput else { return }
        guard !isFinished, !isPaused, writer.status == .writing, sessionStartTime.isValid else { return }
        guard CMSampleBufferIsValid(sampleBuffer), CMSampleBufferDataIsReady(sampleBuffer) else { return }
        let sourceTime = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        guard sourceTime.isValid else { return }
        let time = sourceTime - pauseOffset
        guard time >= sessionStartTime,
              input.isReadyForMoreMediaData,
              let buffer = Self.retimed(sampleBuffer, by: pauseOffset)
        else {
            return
        }
        if input.append(buffer) {
            let duration = CMSampleBufferGetDuration(sampleBuffer)
            noteMediaTime(duration.isValid ? time + duration : time)
        }
    }

    private func noteMediaTime(_ time: CMTime) {
        if !lastMediaTime.isValid || time > lastMediaTime {
            lastMediaTime = time
        }
    }

    /// Finalizes the movie. `completion` runs exactly once.
    func finish(completion: @escaping @Sendable (Result<RecordingResult, any Error>) -> Void) {
        guard !isFinished else {
            completion(.failure(CaptureError.notRecording))
            return
        }
        isFinished = true
        if writer.status == .failed {
            let message = writer.error?.localizedDescription ?? "writer failed"
            try? FileManager.default.removeItem(at: outputURL)
            completion(.failure(CaptureError.writerFailed(message)))
            return
        }
        guard sessionStartTime.isValid, framesWritten > 0 else {
            if writer.status == .writing {
                writer.cancelWriting()
            }
            try? FileManager.default.removeItem(at: outputURL)
            completion(.failure(CaptureError.noFramesCaptured))
            return
        }
        videoInput.markAsFinished()
        microphoneInput?.markAsFinished()
        systemAudioInput?.markAsFinished()
        let endTime = lastMediaTime.isValid ? lastMediaTime : lastVideoTime
        writer.endSession(atSourceTime: endTime)
        let duration = max(0, (endTime - sessionStartTime).seconds)
        writer.finishWriting { [self] in
            self.completeFinish(duration: duration, completion: completion)
        }
    }

    private func completeFinish(duration: TimeInterval, completion: @Sendable (Result<RecordingResult, any Error>) -> Void) {
        guard writer.status == .completed else {
            completion(.failure(CaptureError.writerFailed(writer.error?.localizedDescription ?? "finishWriting failed")))
            return
        }
        let attributes = try? FileManager.default.attributesOfItem(atPath: outputURL.path)
        let size = (attributes?[.size] as? NSNumber)?.int64Value ?? 0
        completion(.success(RecordingResult(
            fileURL: outputURL,
            duration: duration,
            fileSize: size,
            width: width,
            height: height,
            codec: codec,
            hasSystemAudio: hasSystemAudio,
            hasMicrophone: hasMicrophone
        )))
    }

    /// Cancels and deletes the partial file.
    func discard() {
        isFinished = true
        if writer.status == .writing {
            writer.cancelWriting()
        }
        try? FileManager.default.removeItem(at: outputURL)
    }

    /// Shifts every timing entry back by `offset` (pause accounting).
    static func retimed(_ sampleBuffer: CMSampleBuffer, by offset: CMTime) -> CMSampleBuffer? {
        guard offset != .zero else { return sampleBuffer }
        var count: CMItemCount = 0
        guard CMSampleBufferGetSampleTimingInfoArray(sampleBuffer, entryCount: 0, arrayToFill: nil, entriesNeededOut: &count) == noErr,
              count > 0
        else {
            return nil
        }
        var timing = [CMSampleTimingInfo](repeating: CMSampleTimingInfo(), count: count)
        guard CMSampleBufferGetSampleTimingInfoArray(sampleBuffer, entryCount: count, arrayToFill: &timing, entriesNeededOut: &count) == noErr else {
            return nil
        }
        for index in timing.indices {
            if timing[index].presentationTimeStamp.isValid {
                timing[index].presentationTimeStamp = timing[index].presentationTimeStamp - offset
            }
            if timing[index].decodeTimeStamp.isValid {
                timing[index].decodeTimeStamp = timing[index].decodeTimeStamp - offset
            }
        }
        var copy: CMSampleBuffer?
        let status = CMSampleBufferCreateCopyWithNewTiming(
            allocator: kCFAllocatorDefault,
            sampleBuffer: sampleBuffer,
            sampleTimingEntryCount: count,
            sampleTimingArray: &timing,
            sampleBufferOut: &copy
        )
        return status == noErr ? copy : nil
    }
}

// MARK: - Engine

/// ScreenCaptureKit + AVAssetWriter recorder.
///
/// Concurrency: `stream`, `writer` and `stopRequested` are confined to
/// `queue` (also the sample handler queue); the event continuation is guarded
/// by `lock`. That confinement backs `@unchecked Sendable`.
public final class ScreenCaptureEngine: NSObject, ScreenCaptureServiceProtocol, @unchecked Sendable {
    private let queue = DispatchQueue(label: "io.vetcoders.screenscribe.capture", qos: .userInitiated)
    private var stream: SCStream?
    private var writer: RecordingWriter?
    private var stopRequested = false

    private let lock = NSLock()
    private var continuation: AsyncStream<CaptureEvent>.Continuation?

    override public init() {
        super.init()
    }

    // MARK: Events

    public func events() -> AsyncStream<CaptureEvent> {
        AsyncStream { continuation in
            let previous = lock.withLock { () -> AsyncStream<CaptureEvent>.Continuation? in
                let previous = self.continuation
                self.continuation = continuation
                return previous
            }
            previous?.finish()
        }
    }

    private func emit(_ event: CaptureEvent) {
        let continuation = lock.withLock { self.continuation }
        continuation?.yield(event)
    }

    // MARK: Sources

    public func availableSources() async throws -> CaptureSourceCatalog {
        let content = try await Self.shareableContent()
        let ownPID = ProcessInfo.processInfo.processIdentifier
        let names = await Self.displayNames()

        let displays = content.displays.enumerated().map { index, display in
            DisplaySource(
                id: display.displayID,
                name: names[display.displayID] ?? L10n.format("capture.display.fallback", index + 1),
                width: display.width,
                height: display.height
            )
        }
        let windows: [WindowSource] = content.windows.compactMap { window in
            guard window.isOnScreen,
                  window.windowLayer == 0,
                  window.frame.width >= 80,
                  window.frame.height >= 60,
                  let application = window.owningApplication,
                  application.processID != ownPID
            else {
                return nil
            }
            return WindowSource(
                id: window.windowID,
                title: window.title ?? "",
                applicationName: application.applicationName,
                bundleIdentifier: application.bundleIdentifier,
                width: Int(window.frame.width),
                height: Int(window.frame.height)
            )
        }
        var seenBundles = Set<String>()
        let applications: [ApplicationSource] = content.applications
            .filter { $0.processID != ownPID && !$0.applicationName.isEmpty && !$0.bundleIdentifier.isEmpty }
            .filter { seenBundles.insert($0.bundleIdentifier).inserted }
            .map { ApplicationSource(id: $0.processID, name: $0.applicationName, bundleIdentifier: $0.bundleIdentifier) }
            .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }

        return CaptureSourceCatalog(displays: displays, windows: windows, applications: applications)
    }

    public func microphones() -> [MicrophoneSource] {
        AVCaptureDevice.DiscoverySession(
            deviceTypes: [.microphone, .external],
            mediaType: .audio,
            position: .unspecified
        )
        .devices
        .map { MicrophoneSource(id: $0.uniqueID, name: $0.localizedName) }
    }

    // MARK: Recording

    public func startRecording(_ request: CaptureRequest) async throws {
        let busy = queue.sync { stream != nil || writer != nil }
        guard !busy else {
            throw CaptureError.alreadyRecording
        }
        guard CGPreflightScreenCaptureAccess() else {
            throw CaptureError.permissionDenied
        }
        let options = request.options
        if options.capturesMicrophone {
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized:
                break
            case .notDetermined:
                guard await AVCaptureDevice.requestAccess(for: .audio) else {
                    throw CaptureError.microphonePermissionDenied
                }
            default:
                throw CaptureError.microphonePermissionDenied
            }
        }

        let content = try await Self.shareableContent()
        let ownPID = ProcessInfo.processInfo.processIdentifier
        let excludedWindows = content.windows.filter { window in
            window.owningApplication?.processID == ownPID || request.excludedWindowIDs.contains(window.windowID)
        }

        let filter: SCContentFilter
        switch request.source {
        case .display(let source):
            guard let display = content.displays.first(where: { $0.displayID == source.id }) else {
                throw CaptureError.sourceUnavailable
            }
            filter = SCContentFilter(display: display, excludingWindows: excludedWindows)
        case .window(let source):
            guard let window = content.windows.first(where: { $0.windowID == source.id }) else {
                throw CaptureError.sourceUnavailable
            }
            filter = SCContentFilter(desktopIndependentWindow: window)
        case .application(let source, let displaySource):
            guard let display = content.displays.first(where: { $0.displayID == displaySource.id }),
                  let application = content.applications.first(where: { $0.processID == source.id })
            else {
                throw CaptureError.sourceUnavailable
            }
            filter = SCContentFilter(display: display, including: [application], exceptingWindows: [])
        }

        let scale = CGFloat(filter.pointPixelScale)
        let rect = filter.contentRect
        let size = Self.outputSize(
            width: Int((rect.width * scale).rounded()),
            height: Int((rect.height * scale).rounded()),
            maxDimension: options.codec.maximumDimension
        )

        let configuration = SCStreamConfiguration()
        configuration.width = size.width
        configuration.height = size.height
        configuration.scalesToFit = true
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(options.framesPerSecond))
        configuration.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        configuration.colorSpaceName = CGColorSpace.sRGB
        configuration.showsCursor = options.showsCursor
        configuration.queueDepth = 6
        configuration.capturesAudio = options.capturesSystemAudio
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 48_000
        configuration.channelCount = 2
        if options.capturesMicrophone {
            configuration.captureMicrophone = true
            if let deviceID = options.microphoneDeviceID, !deviceID.isEmpty {
                configuration.microphoneCaptureDeviceID = deviceID
            }
        }

        try? FileManager.default.createDirectory(
            at: request.outputURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try? FileManager.default.removeItem(at: request.outputURL)
        let writer = try RecordingWriter(
            outputURL: request.outputURL,
            fileType: options.container == .mov ? .mov : .mp4,
            codec: options.codec,
            width: size.width,
            height: size.height,
            framesPerSecond: options.framesPerSecond,
            includeMicrophone: options.capturesMicrophone,
            includeSystemAudio: options.capturesSystemAudio
        )

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        do {
            try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
            if options.capturesSystemAudio {
                try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
            }
            if options.capturesMicrophone {
                try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
            }
        } catch {
            queue.sync { writer.discard() }
            throw CaptureError.streamFailed(error.localizedDescription)
        }

        queue.sync {
            self.stream = stream
            self.writer = writer
            self.stopRequested = false
        }
        do {
            try await stream.startCapture()
        } catch {
            queue.sync {
                self.stream = nil
                self.writer = nil
                writer.discard()
            }
            throw Self.mapStreamError(error)
        }
    }

    public func pauseRecording() async {
        let now = CMClockGetTime(CMClockGetHostTimeClock())
        queue.sync { writer?.pause(at: now) }
    }

    public func resumeRecording() async {
        let now = CMClockGetTime(CMClockGetHostTimeClock())
        queue.sync { writer?.resume(at: now) }
    }

    public func stopRecording() async throws -> RecordingResult {
        let activeStream: SCStream? = queue.sync {
            stopRequested = true
            return stream
        }
        guard let activeStream else {
            throw CaptureError.notRecording
        }
        do {
            try await activeStream.stopCapture()
        } catch {
            // The stream may already be gone; the writer is still finalized.
        }
        return try await finalize()
    }

    public func cancelRecording() async {
        let activeStream: SCStream? = queue.sync {
            stopRequested = true
            return stream
        }
        if let activeStream {
            try? await activeStream.stopCapture()
        }
        queue.sync {
            writer?.discard()
            writer = nil
            stream = nil
        }
    }

    private func finalize() async throws -> RecordingResult {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<RecordingResult, any Error>) in
            queue.async {
                guard let writer = self.writer else {
                    continuation.resume(throwing: CaptureError.notRecording)
                    return
                }
                self.writer = nil
                self.stream = nil
                writer.finish { result in
                    continuation.resume(with: result)
                }
            }
        }
    }

    /// Runs on `queue`. Finalizes whatever was recorded so nothing is lost.
    private func handleUnexpectedStop(_ error: any Error) {
        guard !stopRequested, let writer else { return }
        self.writer = nil
        self.stream = nil
        let reason = error.localizedDescription
        writer.finish { [weak self] result in
            self?.emit(.stoppedUnexpectedly(reason: reason, recording: try? result.get()))
        }
    }

    // MARK: Helpers

    static func shareableContent() async throws -> SCShareableContent {
        do {
            return try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        } catch {
            throw mapStreamError(error)
        }
    }

    static func mapStreamError(_ error: any Error) -> CaptureError {
        if let captureError = error as? CaptureError {
            return captureError
        }
        if let streamError = error as? SCStreamError, streamError.code == .userDeclined {
            return .permissionDenied
        }
        if !CGPreflightScreenCaptureAccess() {
            return .permissionDenied
        }
        return .streamFailed(error.localizedDescription)
    }

    @MainActor
    static func displayNames() -> [CGDirectDisplayID: String] {
        var names: [CGDirectDisplayID: String] = [:]
        let key = NSDeviceDescriptionKey("NSScreenNumber")
        for screen in NSScreen.screens {
            if let number = screen.deviceDescription[key] as? NSNumber {
                names[CGDirectDisplayID(number.uint32Value)] = screen.localizedName
            }
        }
        return names
    }

    /// Native pixel size capped to the codec limit, aspect preserved, even.
    static func outputSize(width: Int, height: Int, maxDimension: Int) -> (width: Int, height: Int) {
        var scaledWidth = Double(max(2, width))
        var scaledHeight = Double(max(2, height))
        let longest = max(scaledWidth, scaledHeight)
        if longest > Double(maxDimension) {
            let factor = Double(maxDimension) / longest
            scaledWidth *= factor
            scaledHeight *= factor
        }
        let evenWidth = max(2, Int(scaledWidth.rounded(.down)) & ~1)
        let evenHeight = max(2, Int(scaledHeight.rounded(.down)) & ~1)
        return (evenWidth, evenHeight)
    }

    static func isCompleteFrame(_ sampleBuffer: CMSampleBuffer) -> Bool {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
            as? [[SCStreamFrameInfo: Any]],
            let info = attachments.first,
            let rawStatus = info[SCStreamFrameInfo.status] as? Int,
            let status = SCFrameStatus(rawValue: rawStatus)
        else {
            return false
        }
        return status == .complete
    }
}

// MARK: - ScreenCaptureKit callbacks

extension ScreenCaptureEngine: SCStreamOutput, SCStreamDelegate {
    /// Delivered on `queue`.
    public func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard let writer else { return }
        switch type {
        case .screen:
            guard Self.isCompleteFrame(sampleBuffer) else { return }
            writer.appendVideo(sampleBuffer)
        case .audio:
            writer.appendAudio(sampleBuffer, microphone: false)
        case .microphone:
            writer.appendAudio(sampleBuffer, microphone: true)
        @unknown default:
            break
        }
    }

    public func stream(_ stream: SCStream, didStopWithError error: any Error) {
        queue.async { [weak self] in
            self?.handleUnexpectedStop(error)
        }
    }
}
