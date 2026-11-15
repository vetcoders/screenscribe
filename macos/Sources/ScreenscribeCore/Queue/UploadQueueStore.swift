import Foundation

/// Lifecycle of one local video on its way to a hosted review.
public enum UploadItemState: String, Codable, Sendable, Equatable {
    /// Saved locally; waits for the user to press Upload (auto-upload off,
    /// or an upload the user cancelled).
    case draft
    case pending
    case uploading
    case processing
    case completed
    case failed
    /// `402`: waits for a subscription; retried after purchase/restore.
    case billingBlocked = "billing_blocked"

    public var isActive: Bool {
        self == .uploading || self == .processing
    }

    /// States from which the user can (re)start an upload.
    public var canStartUpload: Bool {
        self == .draft || self == .failed || self == .billingBlocked
    }

    public var localizedTitle: String {
        switch self {
        case .draft: return L10n.string("queue.state.draft")
        case .pending: return L10n.string("queue.state.pending")
        case .uploading: return L10n.string("queue.state.uploading")
        case .processing: return L10n.string("queue.state.processing")
        case .completed: return L10n.string("queue.state.completed")
        case .failed: return L10n.string("queue.state.failed")
        case .billingBlocked: return L10n.string("queue.state.billing")
        }
    }
}

public enum UploadItemOrigin: String, Codable, Sendable, Equatable {
    case recording
    case imported
}

/// One persisted queue entry (`UploadQueue.json`).
public struct UploadQueueItem: Codable, Sendable, Equatable, Identifiable {
    public var id: UUID
    public var sourceFileURL: URL
    public var displayName: String
    public var origin: UploadItemOrigin
    public var language: RecordingLanguage
    public var preset: ReviewPreset
    public var prompt: String
    public var visibility: ReviewVisibility
    public var idempotencyKey: String
    public var state: UploadItemState
    public var jobID: String?
    public var stageRaw: String?
    public var progressPct: Double
    public var uploadedBytes: Int64
    public var totalBytes: Int64
    public var shareID: String?
    public var shareURL: String?
    public var reportJSONURL: String?
    public var statusURL: String?
    public var eventsURL: String?
    public var lastError: String?
    public var attemptCount: Int
    public var nextAttemptAt: Date?
    public var createdAt: Date
    public var updatedAt: Date
    public var completedAt: Date?

    public init(
        id: UUID = UUID(),
        sourceFileURL: URL,
        displayName: String? = nil,
        origin: UploadItemOrigin,
        language: RecordingLanguage,
        preset: ReviewPreset,
        prompt: String = "",
        visibility: ReviewVisibility,
        totalBytes: Int64 = 0,
        now: Date = Date()
    ) {
        self.id = id
        self.sourceFileURL = sourceFileURL
        self.displayName = displayName ?? sourceFileURL.lastPathComponent
        self.origin = origin
        self.language = language
        self.preset = preset
        self.prompt = prompt
        self.visibility = visibility
        self.idempotencyKey = UUID().uuidString.lowercased()
        self.state = .pending
        self.jobID = nil
        self.stageRaw = nil
        self.progressPct = 0
        self.uploadedBytes = 0
        self.totalBytes = totalBytes
        self.shareID = nil
        self.shareURL = nil
        self.reportJSONURL = nil
        self.statusURL = nil
        self.eventsURL = nil
        self.lastError = nil
        self.attemptCount = 0
        self.nextAttemptAt = nil
        self.createdAt = now
        self.updatedAt = now
        self.completedAt = nil
    }

    public var stage: JobStage {
        stageRaw.map(JobStage.init(wireValue:)) ?? .queued
    }

    /// The upload payload. The idempotency key is stable across retries.
    public var submission: JobSubmission {
        JobSubmission(
            fileURL: sourceFileURL,
            fileName: sourceFileURL.lastPathComponent,
            language: language,
            preset: preset,
            prompt: prompt,
            visibility: visibility,
            idempotencyKey: idempotencyKey
        )
    }

    /// Status + events references once the job exists server-side.
    public var jobReference: (status: String, events: String)? {
        guard let jobID else { return nil }
        return (statusURL ?? CloudEndpoint.job(jobID), eventsURL ?? CloudEndpoint.jobEvents(jobID))
    }

    public var uploadFraction: Double {
        guard totalBytes > 0 else { return 0 }
        return min(1, max(0, Double(uploadedBytes) / Double(totalBytes)))
    }

    public var hostedReviewAvailable: Bool {
        state == .completed && shareID != nil
    }

    public mutating func apply(created: JobCreateResponse, now: Date = Date()) {
        jobID = created.jobID
        statusURL = created.statusURL
        eventsURL = created.eventsURL
        stageRaw = created.stage.wireValue
        state = created.status.isTerminal ? (created.status == .completed ? .completed : .failed) : .processing
        uploadedBytes = max(uploadedBytes, totalBytes)
        lastError = nil
        nextAttemptAt = nil
        updatedAt = now
    }

    public mutating func apply(snapshot: JobStatusResponse, now: Date = Date()) {
        stageRaw = snapshot.stage.wireValue
        progressPct = snapshot.progressPct
        if let shareID = snapshot.shareID { self.shareID = shareID }
        if let shareURL = snapshot.shareURL { self.shareURL = shareURL }
        if let reportJSONURL = snapshot.reportJSONURL { self.reportJSONURL = reportJSONURL }
        switch snapshot.status {
        case .queued, .processing:
            state = .processing
        case .completed:
            state = .completed
            progressPct = 100
            completedAt = now
            lastError = nil
        case .failed:
            state = .failed
            lastError = snapshot.failureMessage
        }
        updatedAt = now
    }
}

/// Crash-safe journal of uploads and the Recent Reviews cache, stored in the
/// sandbox container under `Application Support/Screenscribe/`.
public actor UploadQueueStore {
    public static let maxAttempts = 6

    public nonisolated let rootDirectory: URL
    private var items: [UploadQueueItem] = []
    private var loaded = false
    private var reviews: [ReviewHistoryItem]?

    public init(rootDirectory: URL = UploadQueueStore.defaultRootDirectory()) {
        self.rootDirectory = rootDirectory
    }

    public static func defaultRootDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("Screenscribe", isDirectory: true)
    }

    public nonisolated var journalURL: URL {
        rootDirectory.appendingPathComponent("UploadQueue.json")
    }

    /// Multipart bodies being uploaded; wiped on launch and after each upload.
    public nonisolated var scratchDirectory: URL {
        rootDirectory.appendingPathComponent("UploadScratch", isDirectory: true)
    }

    public nonisolated var recordingsDirectory: URL {
        rootDirectory.appendingPathComponent("Recordings", isDirectory: true)
    }

    /// Container copies of user-selected files (survive relaunch without
    /// security-scoped bookmarks).
    public nonisolated var importsDirectory: URL {
        rootDirectory.appendingPathComponent("Imports", isDirectory: true)
    }

    public nonisolated var reviewsCacheURL: URL {
        rootDirectory.appendingPathComponent("RecentReviews.json")
    }

    /// Exponential backoff: 5 s, 10 s, 20 s … capped at 5 minutes.
    public static func retryDelay(forAttempt attempt: Int) -> TimeInterval {
        let exponent = Double(max(0, min(attempt, 10)))
        return min(300, 5 * pow(2, exponent))
    }

    // MARK: Lifecycle

    public func prepare() throws {
        let fileManager = FileManager.default
        for directory in [rootDirectory, scratchDirectory, recordingsDirectory, importsDirectory] {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        loadIfNeeded()
    }

    /// After a crash or quit mid-upload: interrupted uploads go back to
    /// `pending` (the idempotency key makes the retry safe), processing jobs
    /// keep tracking, and scratch bodies are deleted.
    @discardableResult
    public func recoverAfterLaunch(now: Date = Date()) throws -> [UploadQueueItem] {
        try prepare()
        for index in items.indices where items[index].state == .uploading {
            items[index].state = items[index].jobID == nil ? .pending : .processing
            items[index].uploadedBytes = 0
            items[index].nextAttemptAt = nil
            items[index].updatedAt = now
        }
        cleanupScratch()
        try persist()
        return sortedItems()
    }

    public func cleanupScratch() {
        let fileManager = FileManager.default
        guard let contents = try? fileManager.contentsOfDirectory(at: scratchDirectory, includingPropertiesForKeys: nil) else {
            return
        }
        for url in contents {
            try? fileManager.removeItem(at: url)
        }
    }

    // MARK: Items

    public func allItems() -> [UploadQueueItem] {
        loadIfNeeded()
        return sortedItems()
    }

    public func item(id: UUID) -> UploadQueueItem? {
        loadIfNeeded()
        return items.first { $0.id == id }
    }

    public func upsert(_ item: UploadQueueItem) throws {
        loadIfNeeded()
        if let index = items.firstIndex(where: { $0.id == item.id }) {
            items[index] = item
        } else {
            items.append(item)
        }
        try persist()
    }

    @discardableResult
    public func update(id: UUID, _ mutate: @Sendable (inout UploadQueueItem) -> Void) throws -> UploadQueueItem? {
        loadIfNeeded()
        guard let index = items.firstIndex(where: { $0.id == id }) else { return nil }
        mutate(&items[index])
        items[index].updatedAt = Date()
        try persist()
        return items[index]
    }

    /// Removes the entry; recordings/imports owned by the container are
    /// deleted with it when `deleteOwnedFile` is set.
    public func remove(id: UUID, deleteOwnedFile: Bool = true) throws {
        loadIfNeeded()
        guard let index = items.firstIndex(where: { $0.id == id }) else { return }
        let removed = items.remove(at: index)
        if deleteOwnedFile, isOwned(removed.sourceFileURL) {
            try? FileManager.default.removeItem(at: removed.sourceFileURL)
        }
        try persist()
    }

    /// Oldest `pending` item whose backoff elapsed.
    public func nextReadyItem(now: Date = Date()) -> UploadQueueItem? {
        loadIfNeeded()
        return items
            .filter { $0.state == .pending && ($0.nextAttemptAt.map { $0 <= now } ?? true) }
            .min { $0.createdAt < $1.createdAt }
    }

    /// Earliest scheduled retry, used to arm the drain timer.
    public func nextRetryDate() -> Date? {
        loadIfNeeded()
        return items
            .filter { $0.state == .pending }
            .compactMap(\.nextAttemptAt)
            .min()
    }

    /// Moves `billingBlocked` items back to `pending` (after purchase/restore).
    @discardableResult
    public func releaseBillingBlocked(now: Date = Date()) throws -> Int {
        loadIfNeeded()
        var released = 0
        for index in items.indices where items[index].state == .billingBlocked {
            items[index].state = items[index].jobID == nil ? .pending : .processing
            items[index].lastError = nil
            items[index].nextAttemptAt = nil
            items[index].updatedAt = now
            released += 1
        }
        if released > 0 {
            try persist()
        }
        return released
    }

    /// Destination for a new screen recording inside the container.
    public func makeRecordingURL(fileExtension: String, now: Date = Date()) throws -> URL {
        try prepare()
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd'_'HH-mm-ss"
        let base = "Screenscribe_\(formatter.string(from: now))"
        var candidate = recordingsDirectory.appendingPathComponent("\(base).\(fileExtension)")
        var suffix = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = recordingsDirectory.appendingPathComponent("\(base)_\(suffix).\(fileExtension)")
            suffix += 1
        }
        return candidate
    }

    /// Movie files in `Recordings/` that no queue entry references — left
    /// behind when the app quit or crashed mid-recording. The writer emits
    /// fragmented movies, so these are usually playable and get re-queued.
    public func orphanedRecordings() -> [URL] {
        loadIfNeeded()
        let known = Set(items.map { $0.sourceFileURL.standardizedFileURL.path })
        let contents = (try? FileManager.default.contentsOfDirectory(
            at: recordingsDirectory,
            includingPropertiesForKeys: [.isRegularFileKey, .creationDateKey],
            options: [.skipsHiddenFiles]
        )) ?? []
        return contents
            .filter { VideoFileValidator.isSupported($0) && !known.contains($0.standardizedFileURL.path) }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    public nonisolated func isOwned(_ url: URL) -> Bool {
        let path = url.standardizedFileURL.path
        return path.hasPrefix(recordingsDirectory.standardizedFileURL.path + "/")
            || path.hasPrefix(importsDirectory.standardizedFileURL.path + "/")
    }

    // MARK: Recent reviews

    /// `GET /api/v1/reviews`, cached to disk for offline launch.
    public func refreshRecentReviews(using client: ScreenscribeCloudClient, limit: Int = 50) async throws -> [ReviewHistoryItem] {
        let fetched = try await client.recentReviews(limit: limit)
        let sorted = fetched.sorted { ($0.createdAt ?? .distantPast) > ($1.createdAt ?? .distantPast) }
        reviews = sorted
        if let data = try? WireCoding.makeEncoder().encode(sorted) {
            try? data.write(to: reviewsCacheURL, options: [.atomic])
        }
        return sorted
    }

    public func cachedRecentReviews() -> [ReviewHistoryItem] {
        if let reviews { return reviews }
        guard let data = try? Data(contentsOf: reviewsCacheURL),
              let decoded = try? WireCoding.makeDecoder().decode([ReviewHistoryItem].self, from: data)
        else {
            reviews = []
            return []
        }
        reviews = decoded
        return decoded
    }

    /// Sign-out: forget the cached history of the previous account.
    public func clearRecentReviews() {
        reviews = []
        try? FileManager.default.removeItem(at: reviewsCacheURL)
    }

    // MARK: Persistence

    private func sortedItems() -> [UploadQueueItem] {
        items.sorted { $0.createdAt > $1.createdAt }
    }

    private func loadIfNeeded() {
        guard !loaded else { return }
        loaded = true
        guard let data = try? Data(contentsOf: journalURL) else {
            items = []
            return
        }
        do {
            items = try WireCoding.makeDecoder().decode([UploadQueueItem].self, from: data)
        } catch {
            // Keep the unreadable journal for diagnosis instead of overwriting it silently.
            let backup = journalURL.deletingPathExtension().appendingPathExtension("corrupt.json")
            try? FileManager.default.removeItem(at: backup)
            try? FileManager.default.moveItem(at: journalURL, to: backup)
            items = []
        }
    }

    private func persist() throws {
        try FileManager.default.createDirectory(at: rootDirectory, withIntermediateDirectories: true)
        let data = try WireCoding.makeEncoder().encode(items)
        try data.write(to: journalURL, options: [.atomic])
    }
}
