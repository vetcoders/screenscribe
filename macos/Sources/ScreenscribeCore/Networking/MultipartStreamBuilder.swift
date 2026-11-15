import Foundation
import UniformTypeIdentifiers

/// A text part of a `multipart/form-data` body.
public struct MultipartFormField: Sendable, Equatable {
    public var name: String
    public var value: String

    public init(name: String, value: String) {
        self.name = name
        self.value = value
    }
}

public enum MultipartStreamError: Error, Equatable, Sendable, LocalizedError {
    case sourceUnreadable(String)
    case destinationUnwritable(String)
    case emptySource

    public var errorDescription: String? {
        switch self {
        case .sourceUnreadable(let name): return L10n.format("error.upload.unreadable", name)
        case .destinationUnwritable: return L10n.string("error.upload.scratch")
        case .emptySource: return L10n.string("error.upload.empty")
        }
    }
}

/// Builds `multipart/form-data` bodies on disk, never in memory.
///
/// A 2 GB screen recording is copied in 256 KB chunks into a scratch file
/// that `URLSession.uploadTask(with:fromFile:)` then streams, so memory stays
/// flat regardless of recording length.
public enum MultipartStreamBuilder {
    /// Name of the video part. See `docs/macos-appstore.md` (BOUNDARY).
    public static let defaultFileFieldName = "file"
    public static let chunkSize = 256 * 1024

    public static func makeBoundary() -> String {
        "Screenscribe-Boundary-" + UUID().uuidString.replacingOccurrences(of: "-", with: "")
    }

    public static func contentType(boundary: String) -> String {
        "multipart/form-data; boundary=\(boundary)"
    }

    /// Streams fields + file into `destination` and returns the body length.
    @discardableResult
    public static func writeBody(
        boundary: String,
        fields: [MultipartFormField],
        fileFieldName: String = defaultFileFieldName,
        fileURL: URL,
        fileName: String,
        mimeType: String,
        to destination: URL
    ) throws -> Int64 {
        let fileManager = FileManager.default
        guard fileManager.isReadableFile(atPath: fileURL.path) else {
            throw MultipartStreamError.sourceUnreadable(fileURL.lastPathComponent)
        }
        try? fileManager.removeItem(at: destination)
        guard fileManager.createFile(atPath: destination.path, contents: nil) else {
            throw MultipartStreamError.destinationUnwritable(destination.lastPathComponent)
        }

        let input: FileHandle
        let output: FileHandle
        do {
            input = try FileHandle(forReadingFrom: fileURL)
        } catch {
            throw MultipartStreamError.sourceUnreadable(fileURL.lastPathComponent)
        }
        defer { try? input.close() }
        do {
            output = try FileHandle(forWritingTo: destination)
        } catch {
            throw MultipartStreamError.destinationUnwritable(destination.lastPathComponent)
        }
        defer { try? output.close() }

        var written: Int64 = 0
        func emit(_ text: String) throws {
            let data = Data(text.utf8)
            try output.write(contentsOf: data)
            written += Int64(data.count)
        }

        for field in fields {
            try emit("--\(boundary)\r\n")
            try emit("Content-Disposition: form-data; name=\"\(headerSafe(field.name))\"\r\n\r\n")
            try emit(field.value)
            try emit("\r\n")
        }

        try emit("--\(boundary)\r\n")
        try emit(
            "Content-Disposition: form-data; name=\"\(headerSafe(fileFieldName))\"; filename=\"\(headerSafe(fileName))\"\r\n"
        )
        try emit("Content-Type: \(mimeType)\r\n\r\n")

        var fileBytes: Int64 = 0
        while true {
            let chunk: Data? = try autoreleasepool {
                try input.read(upToCount: chunkSize)
            }
            guard let chunk, !chunk.isEmpty else { break }
            try output.write(contentsOf: chunk)
            written += Int64(chunk.count)
            fileBytes += Int64(chunk.count)
        }
        guard fileBytes > 0 else {
            throw MultipartStreamError.emptySource
        }

        try emit("\r\n--\(boundary)--\r\n")
        return written
    }

    /// `video/mp4` for `.mp4`/`.m4v`, `video/quicktime` for `.mov`, otherwise
    /// the system UTType mapping.
    public static func mimeType(for url: URL) -> String {
        switch url.pathExtension.lowercased() {
        case "mp4", "m4v": return "video/mp4"
        case "mov": return "video/quicktime"
        default:
            return UTType(filenameExtension: url.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
        }
    }

    /// Header parameter value without quotes, backslashes, control or
    /// non-ASCII characters (Starlette decodes part headers as Latin-1).
    static func headerSafe(_ value: String) -> String {
        let folded = value.folding(options: [.diacriticInsensitive, .widthInsensitive], locale: Locale(identifier: "en_US_POSIX"))
        var result = ""
        for scalar in folded.unicodeScalars {
            switch scalar {
            case "\"", "\\":
                result.append("_")
            case _ where scalar.value < 0x20 || scalar.value >= 0x7F:
                result.append("_")
            default:
                result.unicodeScalars.append(scalar)
            }
        }
        return result.isEmpty ? "recording" : result
    }
}
