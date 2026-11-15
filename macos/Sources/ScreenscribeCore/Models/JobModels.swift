import Foundation

// MARK: - Wire coding

/// JSON coders shared by every Screenscribe Cloud call and the local journal.
public enum WireCoding {
    /// Decoder that accepts ISO-8601 dates with or without fractional seconds,
    /// naive timestamps (treated as UTC) and epoch seconds.
    public static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            if let text = try? container.decode(String.self), let date = WireDate.parse(text) {
                return date
            }
            if let seconds = try? container.decode(Double.self) {
                return Date(timeIntervalSince1970: seconds)
            }
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unrecognized date value")
        }
        return decoder
    }

    public static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }
}

/// Tolerant ISO-8601 parsing for server timestamps.
public enum WireDate {
    public static func parse(_ raw: String) -> Date? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count >= 10 else { return nil }

        // `2026-11-15 12:00:00` → `2026-11-15T12:00:00`
        let separatorIndex = text.index(text.startIndex, offsetBy: 10)
        if separatorIndex < text.endIndex, text[separatorIndex] == " " {
            text.replaceSubrange(separatorIndex...separatorIndex, with: "T")
        }

        guard let timeStart = text.firstIndex(of: "T") else {
            let dateOnly = ISO8601DateFormatter()
            dateOnly.formatOptions = [.withFullDate]
            return dateOnly.date(from: text)
        }

        // Python `datetime.isoformat()` without tzinfo carries no designator.
        let timePart = text[text.index(after: timeStart)...]
        let hasZone = timePart.hasSuffix("Z") || timePart.contains("+") || timePart.contains("-")

        // Normalize fractional seconds (Python emits microseconds) to millis.
        // Indices are not reused after this mutation.
        var hasFraction = false
        if let dot = text[timeStart...].firstIndex(of: ".") {
            let digitsStart = text.index(after: dot)
            var digitsEnd = digitsStart
            while digitsEnd < text.endIndex, text[digitsEnd].isASCII, text[digitsEnd].isNumber {
                digitsEnd = text.index(after: digitsEnd)
            }
            let digits = String(text[digitsStart..<digitsEnd])
            if digits.isEmpty {
                text.remove(at: dot)
            } else {
                let millis = String((digits + "000").prefix(3))
                text.replaceSubrange(digitsStart..<digitsEnd, with: millis)
                hasFraction = true
            }
        }

        if !hasZone {
            text += "Z"
        }

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = hasFraction
            ? [.withInternetDateTime, .withFractionalSeconds]
            : [.withInternetDateTime]
        return formatter.date(from: text)
    }

    public static func format(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }
}

// MARK: - Lossy decoding helpers

extension KeyedDecodingContainer {
    func lossyString(_ key: Key) -> String? {
        if let value = try? decodeIfPresent(String.self, forKey: key) {
            return value
        }
        if let value = try? decodeIfPresent(Int.self, forKey: key) {
            return String(value)
        }
        if let value = try? decodeIfPresent(Double.self, forKey: key) {
            return String(value)
        }
        return nil
    }

    func lossyNonEmptyString(_ key: Key) -> String? {
        guard let value = lossyString(key)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty
        else {
            return nil
        }
        return value
    }

    func lossyDouble(_ key: Key) -> Double? {
        if let value = try? decodeIfPresent(Double.self, forKey: key) {
            return value
        }
        if let value = try? decodeIfPresent(Int.self, forKey: key) {
            return Double(value)
        }
        if let value = try? decodeIfPresent(String.self, forKey: key) {
            return Double(value.trimmingCharacters(in: .whitespaces))
        }
        return nil
    }

    func lossyInt(_ key: Key) -> Int? {
        if let value = try? decodeIfPresent(Int.self, forKey: key) {
            return value
        }
        if let value = lossyDouble(key), value.isFinite {
            return Int(value.rounded())
        }
        return nil
    }

    func lossyBool(_ key: Key) -> Bool? {
        if let value = try? decodeIfPresent(Bool.self, forKey: key) {
            return value
        }
        if let value = try? decodeIfPresent(Int.self, forKey: key) {
            return value != 0
        }
        if let value = try? decodeIfPresent(String.self, forKey: key) {
            switch value.lowercased() {
            case "true", "yes", "1": return true
            case "false", "no", "0": return false
            default: return nil
            }
        }
        return nil
    }

    func lossyDate(_ key: Key) -> Date? {
        if let text = try? decodeIfPresent(String.self, forKey: key) {
            return WireDate.parse(text)
        }
        if let seconds = try? decodeIfPresent(Double.self, forKey: key) {
            return Date(timeIntervalSince1970: seconds)
        }
        return nil
    }

    func lossyStringArray(_ key: Key) -> [String] {
        if let values = try? decodeIfPresent([String].self, forKey: key) {
            return values
        }
        if let single = lossyNonEmptyString(key) {
            return [single]
        }
        return []
    }
}

/// Coding key that accepts any string — used for tolerant nested lookups.
struct AnyCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init(_ string: String) {
        self.stringValue = string
        self.intValue = nil
    }

    init?(stringValue: String) {
        self.stringValue = stringValue
        self.intValue = nil
    }

    init?(intValue: Int) {
        self.stringValue = String(intValue)
        self.intValue = intValue
    }
}

// MARK: - Job stage & status

/// Server pipeline stage (`GET /api/v1/jobs/{id}` → `stage`).
public enum JobStage: Sendable, Equatable, Hashable, Codable {
    case queued
    case audio
    case transcribe
    case detect
    case screenshots
    case analyze
    case report
    case done
    case unknown(String)

    /// The six visible stages rendered by `PipelineProgressView`.
    public static let pipeline: [JobStage] = [.audio, .transcribe, .detect, .screenshots, .analyze, .report]

    public init(wireValue: String) {
        switch wireValue.trimmingCharacters(in: .whitespaces).lowercased() {
        case "queued", "pending", "": self = .queued
        case "audio", "extract_audio": self = .audio
        case "transcribe", "transcription": self = .transcribe
        case "detect", "detection": self = .detect
        case "screenshots", "screenshot": self = .screenshots
        case "analyze", "analysis", "unified": self = .analyze
        case "report", "render": self = .report
        case "done", "completed", "complete": self = .done
        default: self = .unknown(wireValue)
        }
    }

    public var wireValue: String {
        switch self {
        case .queued: return "queued"
        case .audio: return "audio"
        case .transcribe: return "transcribe"
        case .detect: return "detect"
        case .screenshots: return "screenshots"
        case .analyze: return "analyze"
        case .report: return "report"
        case .done: return "done"
        case .unknown(let raw): return raw
        }
    }

    /// Position inside `pipeline`; `queued` is before, `done` after.
    public var pipelineIndex: Int {
        switch self {
        case .queued, .unknown: return -1
        case .done: return JobStage.pipeline.count
        default: return JobStage.pipeline.firstIndex(of: self) ?? -1
        }
    }

    public var localizedTitle: String {
        switch self {
        case .queued: return L10n.string("stage.queued")
        case .audio: return L10n.string("stage.audio")
        case .transcribe: return L10n.string("stage.transcribe")
        case .detect: return L10n.string("stage.detect")
        case .screenshots: return L10n.string("stage.screenshots")
        case .analyze: return L10n.string("stage.analyze")
        case .report: return L10n.string("stage.report")
        case .done: return L10n.string("stage.done")
        case .unknown: return L10n.string("stage.unknown")
        }
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(wireValue: (try? container.decode(String.self)) ?? "")
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(wireValue)
    }
}

/// Server job lifecycle (`status`).
public enum JobStatus: String, Sendable, Equatable, Codable {
    case queued
    case processing
    case completed
    case failed

    public init(wireValue: String) {
        switch wireValue.trimmingCharacters(in: .whitespaces).lowercased() {
        case "queued", "pending", "accepted": self = .queued
        case "completed", "complete", "succeeded", "success", "done": self = .completed
        case "failed", "failure", "error", "cancelled", "canceled": self = .failed
        default: self = .processing
        }
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(wireValue: (try? container.decode(String.self)) ?? "")
    }

    public var isTerminal: Bool {
        self == .completed || self == .failed
    }
}

// MARK: - Job submission (`POST /api/v1/jobs`)

/// Everything needed to create one hosted review job from a local video.
public struct JobSubmission: Sendable, Equatable {
    public var fileURL: URL
    public var fileName: String
    public var mimeType: String
    public var language: RecordingLanguage?
    public var preset: ReviewPreset?
    public var prompt: String?
    public var visibility: ReviewVisibility?
    /// Sent as the `Idempotency-Key` header so a retried upload never creates
    /// a second job (or a second quota charge) server-side.
    public var idempotencyKey: String

    public init(
        fileURL: URL,
        fileName: String? = nil,
        mimeType: String? = nil,
        language: RecordingLanguage? = nil,
        preset: ReviewPreset? = nil,
        prompt: String? = nil,
        visibility: ReviewVisibility? = nil,
        idempotencyKey: String = UUID().uuidString.lowercased()
    ) {
        self.fileURL = fileURL
        self.fileName = fileName ?? fileURL.lastPathComponent
        self.mimeType = mimeType ?? MultipartStreamBuilder.mimeType(for: fileURL)
        self.language = language
        self.preset = preset
        self.prompt = prompt
        self.visibility = visibility
        self.idempotencyKey = idempotencyKey
    }

    /// Optional text parts that accompany the video part.
    public var formFields: [MultipartFormField] {
        var fields: [MultipartFormField] = []
        if let language {
            fields.append(MultipartFormField(name: "language", value: language.rawValue))
        }
        if let preset {
            fields.append(MultipartFormField(name: "preset", value: preset.rawValue))
        }
        if let prompt = prompt?.trimmingCharacters(in: .whitespacesAndNewlines), !prompt.isEmpty {
            fields.append(MultipartFormField(name: "prompt", value: prompt))
        }
        if let visibility {
            fields.append(MultipartFormField(name: "visibility", value: visibility.rawValue))
        }
        return fields
    }
}

/// `202 Accepted` body of `POST /api/v1/jobs`.
public struct JobCreateResponse: Decodable, Sendable, Equatable {
    public var jobID: String
    public var status: JobStatus
    public var stage: JobStage
    public var createdAt: Date?
    public var statusURL: String?
    public var eventsURL: String?

    public init(
        jobID: String,
        status: JobStatus = .queued,
        stage: JobStage = .queued,
        createdAt: Date? = nil,
        statusURL: String? = nil,
        eventsURL: String? = nil
    ) {
        self.jobID = jobID
        self.status = status
        self.stage = stage
        self.createdAt = createdAt
        self.statusURL = statusURL
        self.eventsURL = eventsURL
    }

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case id
        case status
        case stage
        case createdAt = "created_at"
        case statusURL = "status_url"
        case eventsURL = "events_url"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard let jobID = container.lossyNonEmptyString(.jobID) ?? container.lossyNonEmptyString(.id) else {
            throw DecodingError.keyNotFound(
                CodingKeys.jobID,
                DecodingError.Context(codingPath: decoder.codingPath, debugDescription: "job_id missing")
            )
        }
        self.jobID = jobID
        status = container.lossyString(.status).map(JobStatus.init(wireValue:)) ?? .queued
        stage = container.lossyString(.stage).map(JobStage.init(wireValue:)) ?? .queued
        createdAt = container.lossyDate(.createdAt)
        statusURL = container.lossyNonEmptyString(.statusURL)
        eventsURL = container.lossyNonEmptyString(.eventsURL)
    }

    /// Server-provided status path, or the canonical `/api/v1/jobs/{id}`.
    public var statusReference: String {
        statusURL ?? CloudEndpoint.job(jobID)
    }

    /// Server-provided SSE path, or the canonical `/api/v1/jobs/{id}/events`.
    public var eventsReference: String {
        eventsURL ?? CloudEndpoint.jobEvents(jobID)
    }
}

/// Error object carried by a failed job (`error`). Accepts a bare string too.
public struct JobErrorDetail: Codable, Sendable, Equatable {
    public var message: String
    public var code: String?

    public init(message: String, code: String? = nil) {
        self.message = message
        self.code = code
    }

    public init(from decoder: any Decoder) throws {
        if let single = try? decoder.singleValueContainer(), let text = try? single.decode(String.self) {
            message = text
            code = nil
            return
        }
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        message = container.lossyNonEmptyString(AnyCodingKey("message"))
            ?? container.lossyNonEmptyString(AnyCodingKey("detail"))
            ?? container.lossyNonEmptyString(AnyCodingKey("error"))
            ?? ""
        code = container.lossyNonEmptyString(AnyCodingKey("code"))
            ?? container.lossyNonEmptyString(AnyCodingKey("error"))
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: AnyCodingKey.self)
        try container.encode(message, forKey: AnyCodingKey("message"))
        try container.encodeIfPresent(code, forKey: AnyCodingKey("code"))
    }
}

/// `GET /api/v1/jobs/{id}` body — also the payload of every SSE event.
public struct JobStatusResponse: Decodable, Sendable, Equatable {
    public var jobID: String
    public var status: JobStatus
    public var stage: JobStage
    /// 0…100.
    public var progressPct: Double
    public var shareID: String?
    public var shareURL: String?
    public var reportJSONURL: String?
    public var error: JobErrorDetail?
    public var message: String?

    public init(
        jobID: String,
        status: JobStatus,
        stage: JobStage,
        progressPct: Double = 0,
        shareID: String? = nil,
        shareURL: String? = nil,
        reportJSONURL: String? = nil,
        error: JobErrorDetail? = nil,
        message: String? = nil
    ) {
        self.jobID = jobID
        self.status = status
        self.stage = stage
        self.progressPct = progressPct
        self.shareID = shareID
        self.shareURL = shareURL
        self.reportJSONURL = reportJSONURL
        self.error = error
        self.message = message
    }

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case id
        case status
        case stage
        case progressPct = "progress_pct"
        case progress
        case shareID = "share_id"
        case shareURL = "share_url"
        case reportJSONURL = "report_json_url"
        case error
        case message
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        jobID = container.lossyNonEmptyString(.jobID) ?? container.lossyNonEmptyString(.id) ?? ""
        let decodedStage = container.lossyString(.stage).map(JobStage.init(wireValue:)) ?? .queued
        stage = decodedStage
        if let rawStatus = container.lossyString(.status) {
            status = JobStatus(wireValue: rawStatus)
        } else {
            status = decodedStage == .done ? .completed : (decodedStage == .queued ? .queued : .processing)
        }
        var pct: Double = 0
        if let explicit = container.lossyDouble(.progressPct) {
            pct = explicit
        } else if let fraction = container.lossyDouble(.progress) {
            pct = fraction <= 1 ? fraction * 100 : fraction
        }
        progressPct = min(100, max(0, pct.isFinite ? pct : 0))
        shareID = container.lossyNonEmptyString(.shareID)
        shareURL = container.lossyNonEmptyString(.shareURL)
        reportJSONURL = container.lossyNonEmptyString(.reportJSONURL)
        error = try? container.decodeIfPresent(JobErrorDetail.self, forKey: .error)
        message = container.lossyNonEmptyString(.message)
    }

    public var isTerminal: Bool { status.isTerminal }

    /// Human-readable failure reason for the UI.
    public var failureMessage: String {
        if let text = error?.message, !text.isEmpty { return text }
        if let message, !message.isEmpty { return message }
        return L10n.string("job.error.generic")
    }
}

// MARK: - Job events (SSE)

/// A decoded `GET /api/v1/jobs/{id}/events` message.
public enum JobEvent: Sendable, Equatable {
    case progress(JobStatusResponse)
    case completed(JobStatusResponse)
    case failed(JobStatusResponse)

    public static let progressEventName = "job_progress"
    public static let completedEventName = "job_completed"
    public static let failedEventName = "job_failed"

    /// Maps an SSE frame to a job event. Unnamed frames are classified by the
    /// payload `status`; unrelated event names return `nil`.
    public init?(sseEvent: SSEEvent, jobID: String) {
        let payload = sseEvent.data.trimmingCharacters(in: .whitespacesAndNewlines)
        var snapshot: JobStatusResponse
        if let data = payload.data(using: .utf8),
           let decoded = try? WireCoding.makeDecoder().decode(JobStatusResponse.self, from: data) {
            snapshot = decoded
        } else {
            snapshot = JobStatusResponse(jobID: jobID, status: .processing, stage: .queued, message: payload.isEmpty ? nil : payload)
        }
        if snapshot.jobID.isEmpty {
            snapshot.jobID = jobID
        }

        switch sseEvent.event ?? "message" {
        case JobEvent.completedEventName:
            snapshot.status = .completed
            snapshot.progressPct = 100
            if snapshot.stage != .done { snapshot.stage = .done }
            self = .completed(snapshot)
        case JobEvent.failedEventName:
            snapshot.status = .failed
            self = .failed(snapshot)
        case JobEvent.progressEventName:
            if snapshot.status == .queued, snapshot.stage != .queued {
                snapshot.status = .processing
            }
            self = JobEvent.classify(snapshot)
        case "message":
            guard !payload.isEmpty else { return nil }
            self = JobEvent.classify(snapshot)
        default:
            return nil
        }
    }

    public init(snapshot: JobStatusResponse) {
        self = JobEvent.classify(snapshot)
    }

    private static func classify(_ snapshot: JobStatusResponse) -> JobEvent {
        switch snapshot.status {
        case .completed: return .completed(snapshot)
        case .failed: return .failed(snapshot)
        case .queued, .processing: return .progress(snapshot)
        }
    }

    public var snapshot: JobStatusResponse {
        switch self {
        case .progress(let value), .completed(let value), .failed(let value):
            return value
        }
    }

    public var isTerminal: Bool {
        switch self {
        case .progress: return false
        case .completed, .failed: return true
        }
    }
}

// MARK: - Upload progress

/// Byte-level progress of a streamed multipart upload.
public struct UploadProgress: Sendable, Equatable {
    public var sentBytes: Int64
    public var totalBytes: Int64

    public init(sentBytes: Int64, totalBytes: Int64) {
        self.sentBytes = sentBytes
        self.totalBytes = totalBytes
    }

    public var fraction: Double {
        guard totalBytes > 0 else { return 0 }
        return min(1, max(0, Double(sentBytes) / Double(totalBytes)))
    }
}
