import Foundation

// MARK: - Recent reviews (`GET /api/v1/reviews`)

/// One hosted review in the user's history.
public struct ReviewHistoryItem: Codable, Sendable, Equatable, Hashable, Identifiable {
    public var shareID: String
    public var shareURL: String?
    public var title: String?
    public var createdAt: Date?
    public var status: String?
    public var visibility: String?
    public var durationSeconds: Double?
    public var findingsCount: Int?
    public var jobID: String?

    public init(
        shareID: String,
        shareURL: String? = nil,
        title: String? = nil,
        createdAt: Date? = nil,
        status: String? = nil,
        visibility: String? = nil,
        durationSeconds: Double? = nil,
        findingsCount: Int? = nil,
        jobID: String? = nil
    ) {
        self.shareID = shareID
        self.shareURL = shareURL
        self.title = title
        self.createdAt = createdAt
        self.status = status
        self.visibility = visibility
        self.durationSeconds = durationSeconds
        self.findingsCount = findingsCount
        self.jobID = jobID
    }

    public var id: String { shareID }

    public var displayTitle: String {
        if let title, !title.isEmpty { return title }
        return shareID
    }

    enum CodingKeys: String, CodingKey {
        case shareID = "share_id"
        case shareURL = "share_url"
        case title
        case createdAt = "created_at"
        case status
        case visibility
        case durationSeconds = "duration_seconds"
        case findingsCount = "findings_count"
        case jobID = "job_id"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        guard let shareID = container.lossyNonEmptyString(AnyCodingKey("share_id"))
            ?? container.lossyNonEmptyString(AnyCodingKey("id"))
        else {
            throw DecodingError.dataCorrupted(
                DecodingError.Context(codingPath: decoder.codingPath, debugDescription: "share_id missing")
            )
        }
        self.shareID = shareID
        shareURL = container.lossyNonEmptyString(AnyCodingKey("share_url"))
        title = container.lossyNonEmptyString(AnyCodingKey("title"))
            ?? container.lossyNonEmptyString(AnyCodingKey("video_name"))
            ?? container.lossyNonEmptyString(AnyCodingKey("video"))
        createdAt = container.lossyDate(AnyCodingKey("created_at"))
        status = container.lossyNonEmptyString(AnyCodingKey("status"))
        visibility = container.lossyNonEmptyString(AnyCodingKey("visibility"))
        durationSeconds = container.lossyDouble(AnyCodingKey("duration_seconds"))
        findingsCount = container.lossyInt(AnyCodingKey("findings_count"))
        jobID = container.lossyNonEmptyString(AnyCodingKey("job_id"))
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(shareID, forKey: .shareID)
        try container.encodeIfPresent(shareURL, forKey: .shareURL)
        try container.encodeIfPresent(title, forKey: .title)
        try container.encodeIfPresent(createdAt, forKey: .createdAt)
        try container.encodeIfPresent(status, forKey: .status)
        try container.encodeIfPresent(visibility, forKey: .visibility)
        try container.encodeIfPresent(durationSeconds, forKey: .durationSeconds)
        try container.encodeIfPresent(findingsCount, forKey: .findingsCount)
        try container.encodeIfPresent(jobID, forKey: .jobID)
    }
}

/// Accepts a bare array or `{"reviews": […]}` / `{"items": […]}` /
/// `{"data": […]}`; malformed rows are skipped, not fatal.
public struct ReviewListResponse: Decodable, Sendable, Equatable {
    public var items: [ReviewHistoryItem]

    public init(items: [ReviewHistoryItem]) {
        self.items = items
    }

    public init(from decoder: any Decoder) throws {
        if var unkeyed = try? decoder.unkeyedContainer() {
            items = ReviewListResponse.decodeRows(&unkeyed)
            return
        }
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        for key in ["reviews", "items", "data", "results"] {
            if var unkeyed = try? container.nestedUnkeyedContainer(forKey: AnyCodingKey(key)) {
                items = ReviewListResponse.decodeRows(&unkeyed)
                return
            }
        }
        items = []
    }

    private struct SkippedRow: Decodable {
        init(from decoder: any Decoder) throws {}
    }

    private static func decodeRows(_ container: inout any UnkeyedDecodingContainer) -> [ReviewHistoryItem] {
        var rows: [ReviewHistoryItem] = []
        while !container.isAtEnd {
            if let row = try? container.decode(ReviewHistoryItem.self) {
                rows.append(row)
            } else if (try? container.decode(SkippedRow.self)) == nil {
                break
            }
        }
        return rows
    }
}

// MARK: - Hosted report (`report_json_url`)

/// `summary` block of `report.json`.
public struct ReviewSummaryCounts: Decodable, Sendable, Equatable {
    public var total: Int
    public var bugs: Int
    public var changes: Int
    public var ui: Int

    public init(total: Int = 0, bugs: Int = 0, changes: Int = 0, ui: Int = 0) {
        self.total = total
        self.bugs = bugs
        self.changes = changes
        self.ui = ui
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        total = container.lossyInt(AnyCodingKey("total")) ?? 0
        bugs = container.lossyInt(AnyCodingKey("bugs")) ?? 0
        changes = container.lossyInt(AnyCodingKey("changes")) ?? 0
        ui = container.lossyInt(AnyCodingKey("ui")) ?? 0
    }
}

/// One finding of `report.json` (`findings[]`), flattened with the most
/// useful `unified_analysis` fields.
public struct ReviewFinding: Decodable, Sendable, Equatable, Identifiable {
    public var id: String
    public var category: String
    public var timestampStart: Double
    public var timestampEnd: Double?
    public var timestampFormatted: String?
    public var text: String
    public var severity: String?
    public var summary: String?
    public var suggestedFix: String?
    public var actionItems: [String]

    public init(
        id: String,
        category: String,
        timestampStart: Double,
        timestampEnd: Double? = nil,
        timestampFormatted: String? = nil,
        text: String,
        severity: String? = nil,
        summary: String? = nil,
        suggestedFix: String? = nil,
        actionItems: [String] = []
    ) {
        self.id = id
        self.category = category
        self.timestampStart = timestampStart
        self.timestampEnd = timestampEnd
        self.timestampFormatted = timestampFormatted
        self.text = text
        self.severity = severity
        self.summary = summary
        self.suggestedFix = suggestedFix
        self.actionItems = actionItems
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        id = container.lossyNonEmptyString(AnyCodingKey("id")) ?? UUID().uuidString
        category = container.lossyNonEmptyString(AnyCodingKey("category")) ?? "other"
        timestampStart = container.lossyDouble(AnyCodingKey("timestamp_start")) ?? 0
        timestampEnd = container.lossyDouble(AnyCodingKey("timestamp_end"))
        timestampFormatted = container.lossyNonEmptyString(AnyCodingKey("timestamp_formatted"))
        text = container.lossyString(AnyCodingKey("text")) ?? ""
        if let analysis = try? container.nestedContainer(keyedBy: AnyCodingKey.self, forKey: AnyCodingKey("unified_analysis")) {
            severity = analysis.lossyNonEmptyString(AnyCodingKey("severity"))
            summary = analysis.lossyNonEmptyString(AnyCodingKey("summary"))
            suggestedFix = analysis.lossyNonEmptyString(AnyCodingKey("suggested_fix"))
            actionItems = analysis.lossyStringArray(AnyCodingKey("action_items"))
        } else {
            severity = container.lossyNonEmptyString(AnyCodingKey("severity"))
            summary = nil
            suggestedFix = nil
            actionItems = []
        }
    }

    public var displayTimestamp: String {
        timestampFormatted ?? DisplayFormat.timecode(timestampStart)
    }
}

/// The parts of the hosted `report.json` the native client renders.
public struct ReviewReportSummary: Decodable, Sendable, Equatable {
    public var video: String?
    public var generatedAt: Date?
    public var executiveSummary: String?
    public var summary: ReviewSummaryCounts
    public var findings: [ReviewFinding]
    public var errors: [String]

    public init(
        video: String? = nil,
        generatedAt: Date? = nil,
        executiveSummary: String? = nil,
        summary: ReviewSummaryCounts = ReviewSummaryCounts(),
        findings: [ReviewFinding] = [],
        errors: [String] = []
    ) {
        self.video = video
        self.generatedAt = generatedAt
        self.executiveSummary = executiveSummary
        self.summary = summary
        self.findings = findings
        self.errors = errors
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        video = container.lossyNonEmptyString(AnyCodingKey("video"))
        generatedAt = container.lossyDate(AnyCodingKey("generated_at"))
        executiveSummary = container.lossyNonEmptyString(AnyCodingKey("executive_summary"))
        summary = (try? container.decodeIfPresent(ReviewSummaryCounts.self, forKey: AnyCodingKey("summary")))
            ?? ReviewSummaryCounts()
        findings = (try? container.decodeIfPresent([ReviewFinding].self, forKey: AnyCodingKey("findings"))) ?? []
        if let messages = try? container.decodeIfPresent([String].self, forKey: AnyCodingKey("errors")) {
            errors = messages
        } else if let details = try? container.decodeIfPresent([JobErrorDetail].self, forKey: AnyCodingKey("errors")) {
            errors = details.map(\.message).filter { !$0.isEmpty }
        } else {
            errors = []
        }
        if summary.total == 0, !findings.isEmpty {
            summary.total = findings.count
        }
    }

    /// Findings ordered by timeline position.
    public var orderedFindings: [ReviewFinding] {
        findings.sorted { $0.timestampStart < $1.timestampStart }
    }
}
