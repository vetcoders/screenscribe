@preconcurrency import AVFoundation
import Foundation
import UniformTypeIdentifiers

/// A local movie that passed validation and can be uploaded.
public struct ValidatedVideo: Sendable, Equatable {
    public var url: URL
    public var displayName: String
    public var duration: TimeInterval
    public var fileSize: Int64
    public var width: Int
    public var height: Int
    public var audioTrackCount: Int

    public init(url: URL, displayName: String, duration: TimeInterval, fileSize: Int64, width: Int, height: Int, audioTrackCount: Int) {
        self.url = url
        self.displayName = displayName
        self.duration = duration
        self.fileSize = fileSize
        self.width = width
        self.height = height
        self.audioTrackCount = audioTrackCount
    }
}

public enum VideoValidationError: Error, Equatable, Sendable, LocalizedError {
    case unsupportedType(String)
    case unreadable(String)
    case emptyFile
    case noVideoTrack
    /// The hosted pipeline transcribes narration; a silent file has nothing to review.
    case noAudioTrack
    case tooShort
    case copyFailed(String)

    public var errorDescription: String? {
        switch self {
        case .unsupportedType(let ext): return L10n.format("import.error.type", ext)
        case .unreadable(let name): return L10n.format("import.error.unreadable", name)
        case .emptyFile: return L10n.string("import.error.empty")
        case .noVideoTrack: return L10n.string("import.error.novideo")
        case .noAudioTrack: return L10n.string("import.error.noaudio")
        case .tooShort: return L10n.string("import.error.short")
        case .copyFailed(let message): return L10n.format("import.error.copy", message)
        }
    }
}

public protocol MediaInspectorProtocol: Sendable {
    /// Reads tracks and duration with `AVURLAsset.loadTracks`.
    func inspect(_ url: URL) async throws -> ValidatedVideo
    /// Validates a user-selected file, then copies (APFS clone) it into the
    /// sandbox container so the queue can upload it after a relaunch.
    func importFile(at url: URL, into directory: URL) async throws -> ValidatedVideo
}

public struct VideoFileValidator: MediaInspectorProtocol {
    public static let supportedContentTypes: [UTType] = [.mpeg4Movie, .quickTimeMovie, .movie]
    public static let supportedExtensions: Set<String> = ["mp4", "mov", "m4v"]
    public static let minimumDuration: TimeInterval = 1

    public init() {}

    public static func isSupported(_ url: URL) -> Bool {
        supportedExtensions.contains(url.pathExtension.lowercased())
    }

    public func inspect(_ url: URL) async throws -> ValidatedVideo {
        guard Self.isSupported(url) else {
            throw VideoValidationError.unsupportedType(url.pathExtension.isEmpty ? "?" : url.pathExtension)
        }
        let scoped = url.startAccessingSecurityScopedResource()
        defer {
            if scoped { url.stopAccessingSecurityScopedResource() }
        }

        let fileSize: Int64
        do {
            let values = try url.resourceValues(forKeys: [.fileSizeKey, .isReadableKey])
            guard values.isReadable ?? true else {
                throw VideoValidationError.unreadable(url.lastPathComponent)
            }
            fileSize = Int64(values.fileSize ?? 0)
        } catch let error as VideoValidationError {
            throw error
        } catch {
            throw VideoValidationError.unreadable(url.lastPathComponent)
        }
        guard fileSize > 0 else {
            throw VideoValidationError.emptyFile
        }

        let asset = AVURLAsset(url: url)
        let videoTracks: [AVAssetTrack]
        let audioTracks: [AVAssetTrack]
        let duration: CMTime
        do {
            videoTracks = try await asset.loadTracks(withMediaType: .video)
            audioTracks = try await asset.loadTracks(withMediaType: .audio)
            duration = try await asset.load(.duration)
        } catch {
            throw VideoValidationError.unreadable(url.lastPathComponent)
        }
        guard let videoTrack = videoTracks.first else {
            throw VideoValidationError.noVideoTrack
        }
        guard !audioTracks.isEmpty else {
            throw VideoValidationError.noAudioTrack
        }
        let seconds = duration.isNumeric ? duration.seconds : 0
        guard seconds >= Self.minimumDuration else {
            throw VideoValidationError.tooShort
        }

        var width = 0
        var height = 0
        if let geometry = try? await videoTrack.load(.naturalSize, .preferredTransform) {
            let rect = CGRect(origin: .zero, size: geometry.0).applying(geometry.1)
            width = Int(abs(rect.width).rounded())
            height = Int(abs(rect.height).rounded())
        }

        return ValidatedVideo(
            url: url,
            displayName: url.lastPathComponent,
            duration: seconds,
            fileSize: fileSize,
            width: width,
            height: height,
            audioTrackCount: audioTracks.count
        )
    }

    public func importFile(at url: URL, into directory: URL) async throws -> ValidatedVideo {
        let scoped = url.startAccessingSecurityScopedResource()
        defer {
            if scoped { url.stopAccessingSecurityScopedResource() }
        }
        let original = try await inspect(url)

        let fileManager = FileManager.default
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        } catch {
            throw VideoValidationError.copyFailed(error.localizedDescription)
        }
        let stem = url.deletingPathExtension().lastPathComponent
        let ext = url.pathExtension.lowercased()
        var destination = directory.appendingPathComponent("\(stem).\(ext)")
        var suffix = 2
        while fileManager.fileExists(atPath: destination.path) {
            destination = directory.appendingPathComponent("\(stem)_\(suffix).\(ext)")
            suffix += 1
        }
        do {
            try fileManager.copyItem(at: url, to: destination)
        } catch {
            throw VideoValidationError.copyFailed(error.localizedDescription)
        }

        var imported = original
        imported.url = destination
        imported.displayName = url.lastPathComponent
        return imported
    }
}
