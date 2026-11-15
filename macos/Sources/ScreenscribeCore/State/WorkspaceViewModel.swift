import Foundation
import Observation

/// Sidebar selection of the main workspace window.
public enum WorkspaceSelection: Hashable, Sendable {
    case record
    case upload(UUID)
    case review(String)
}

/// Forwards at most ~2 upload progress updates per second (or every 0.5 %),
/// so a multi-gigabyte upload does not flood the main actor.
final class ProgressThrottle: @unchecked Sendable {
    private let lock = NSLock()
    private var lastFraction: Double = -1
    private var lastForward = Date.distantPast

    func shouldForward(_ progress: UploadProgress, now: Date = Date()) -> Bool {
        lock.withLock {
            let fraction = progress.fraction
            guard fraction >= 1 || fraction - lastFraction >= 0.005 || now.timeIntervalSince(lastForward) >= 0.5 else {
                return false
            }
            lastFraction = fraction
            lastForward = now
            return true
        }
    }
}

/// Upload queue, job tracking and review history.
///
/// Drain rules: one upload at a time, oldest first. Retryable failures back
/// off (5 s → 5 min, 6 attempts). `402` stops the drain, parks the item as
/// `billingBlocked` and opens the paywall — it is never retried blindly.
/// Expired auth stops the drain and asks the user to sign in again.
@MainActor
@Observable
public final class WorkspaceViewModel {
    public private(set) var items: [UploadQueueItem] = []
    public private(set) var serverReviews: [ReviewHistoryItem] = []
    public private(set) var isRefreshingReviews = false
    public private(set) var isImporting = false
    public private(set) var activeUploadID: UUID?
    public private(set) var errorMessage: String?
    public private(set) var reviewsError: String?
    public var selection: WorkspaceSelection? = .record

    @ObservationIgnored let queueStore: UploadQueueStore
    @ObservationIgnored private let mediaInspector: any MediaInspectorProtocol
    @ObservationIgnored private weak var app: AppModel?
    @ObservationIgnored private var drainTask: Task<Void, Never>?
    /// Bumped whenever a drain is abandoned (sign-out), so a finishing
    /// cancelled drain cannot clear the reference to a newer one.
    @ObservationIgnored private var drainGeneration = 0
    /// `drain()` arrived while a drain was finishing: run once more.
    @ObservationIgnored private var drainAgain = false
    /// The single in-flight upload; cancelling it leaves the drain running.
    @ObservationIgnored private var activeUpload: Task<JobCreateResponse, any Error>?
    @ObservationIgnored private var retryTask: Task<Void, Never>?
    @ObservationIgnored private var trackingTasks: [UUID: Task<Void, Never>] = [:]
    @ObservationIgnored private var userCancelled: Set<UUID> = []
    @ObservationIgnored private var liveProgress: [UUID: UploadProgress] = [:]

    private enum DrainStep {
        case proceed
        case halt
    }

    public init(queueStore: UploadQueueStore, mediaInspector: any MediaInspectorProtocol) {
        self.queueStore = queueStore
        self.mediaInspector = mediaInspector
    }

    func attach(to app: AppModel) {
        self.app = app
    }

    private var isSignedIn: Bool {
        app?.isSignedIn ?? false
    }

    // MARK: Derived

    public func item(id: UUID) -> UploadQueueItem? {
        items.first { $0.id == id }
    }

    /// Server history merged with completed local uploads, newest first, so
    /// the list is useful offline and right after a job completes.
    public var recentReviews: [ReviewHistoryItem] {
        var merged: [String: ReviewHistoryItem] = [:]
        for item in items where item.state == .completed {
            guard let shareID = item.shareID else { continue }
            merged[shareID] = ReviewHistoryItem(
                shareID: shareID,
                shareURL: item.shareURL,
                title: item.displayName,
                createdAt: item.completedAt ?? item.updatedAt,
                status: JobStatus.completed.rawValue,
                visibility: item.visibility.rawValue,
                jobID: item.jobID
            )
        }
        for review in serverReviews {
            merged[review.shareID] = review
        }
        return merged.values.sorted { ($0.createdAt ?? .distantPast) > ($1.createdAt ?? .distantPast) }
    }

    public var openItems: [UploadQueueItem] {
        items.filter { $0.state != .completed }
    }

    public var activeCount: Int {
        items.filter { $0.state.isActive || $0.state == .pending }.count
    }

    public var hasBillingBlockedItems: Bool {
        items.contains { $0.state == .billingBlocked }
    }

    public func reviewURL(for item: UploadQueueItem) -> URL? {
        guard let shareID = item.shareID, let app else { return nil }
        return app.reviewURL(shareID: shareID, shareURL: item.shareURL)
    }

    public func reviewURL(for review: ReviewHistoryItem) -> URL? {
        app?.reviewURL(shareID: review.shareID, shareURL: review.shareURL)
    }

    // MARK: Lifecycle

    func start() async {
        do {
            try await queueStore.recoverAfterLaunch()
        } catch {
            errorMessage = L10n.format("queue.error.persist", error.localizedDescription)
        }
        serverReviews = await queueStore.cachedRecentReviews()
        await adoptOrphanRecordings()
        await reloadItems()
    }

    func didSignIn() async {
        await reloadItems()
        resumeTracking()
        drain()
        await refreshReviews()
    }

    func didSignOut() async {
        activeUpload?.cancel()
        drainTask?.cancel()
        drainTask = nil
        drainGeneration += 1
        drainAgain = false
        retryTask?.cancel()
        retryTask = nil
        for task in trackingTasks.values {
            task.cancel()
        }
        trackingTasks.removeAll()
        serverReviews = []
        reviewsError = nil
        await queueStore.clearRecentReviews()
        await reloadItems()
        if case .review = selection {
            selection = .record
        }
    }

    // MARK: Enqueue

    /// A finished screen recording (already inside the container).
    func enqueueRecording(_ result: RecordingResult) async {
        guard let app else { return }
        let configuration = app.configuration
        var item = UploadQueueItem(
            sourceFileURL: result.fileURL,
            origin: .recording,
            language: configuration.defaultLanguage,
            preset: configuration.defaultPreset,
            prompt: configuration.additionalPrompt,
            visibility: configuration.defaultVisibility,
            totalBytes: result.fileSize
        )
        item.state = configuration.autoUploadRecordings ? .pending : .draft
        await insert(item)
        selection = .upload(item.id)
        if item.state == .pending {
            drain()
        }
    }

    /// User-selected movies (open panel or drag & drop). Each file is
    /// validated, then copied into the container so it survives relaunch.
    public func importFiles(_ urls: [URL]) async {
        guard let app, !urls.isEmpty else { return }
        let configuration = app.configuration
        isImporting = true
        defer { isImporting = false }
        var failures: [String] = []
        var lastID: UUID?
        for url in urls {
            do {
                let video = try await mediaInspector.importFile(at: url, into: queueStore.importsDirectory)
                var item = UploadQueueItem(
                    sourceFileURL: video.url,
                    displayName: video.displayName,
                    origin: .imported,
                    language: configuration.defaultLanguage,
                    preset: configuration.defaultPreset,
                    prompt: configuration.additionalPrompt,
                    visibility: configuration.defaultVisibility,
                    totalBytes: video.fileSize
                )
                item.state = .pending
                await insert(item)
                lastID = item.id
            } catch {
                failures.append(error.localizedDescription)
            }
        }
        if let lastID {
            selection = .upload(lastID)
        }
        errorMessage = failures.isEmpty ? nil : failures.joined(separator: "\n")
        drain()
    }

    /// Re-queues recordings left behind by a crash or forced quit as drafts.
    /// Unreadable leftovers are listed as failed so the user can remove them.
    private func adoptOrphanRecordings() async {
        let orphans = await queueStore.orphanedRecordings()
        guard let app, !orphans.isEmpty else { return }
        let configuration = app.configuration
        for url in orphans {
            var item = UploadQueueItem(
                sourceFileURL: url,
                origin: .recording,
                language: configuration.defaultLanguage,
                preset: configuration.defaultPreset,
                prompt: configuration.additionalPrompt,
                visibility: configuration.defaultVisibility
            )
            do {
                let video = try await mediaInspector.inspect(url)
                item.totalBytes = video.fileSize
                item.state = .draft
                item.lastError = L10n.string("queue.recovered")
            } catch {
                item.state = .failed
                item.lastError = error.localizedDescription
            }
            do {
                try await queueStore.upsert(item)
            } catch {
                errorMessage = L10n.format("queue.error.persist", error.localizedDescription)
                return
            }
        }
    }

    // MARK: User actions

    /// Starts (or restarts) an upload the user asked for.
    public func upload(_ id: UUID) async {
        guard let app, let current = item(id: id), current.canStartUploadNow else { return }
        guard app.isSignedIn else {
            app.presentSignIn()
            return
        }
        userCancelled.remove(id)
        await mutate(id) { item in
            item.state = item.jobID == nil ? .pending : .processing
            item.attemptCount = 0
            item.nextAttemptAt = nil
            item.lastError = nil
        }
        if self.item(id: id)?.state == .processing {
            startTracking(id)
        }
        drain()
    }

    /// Stops the running upload (becomes a draft) or un-queues a pending one.
    public func cancelUpload(_ id: UUID) async {
        guard let current = item(id: id) else { return }
        if activeUploadID == id {
            userCancelled.insert(id)
            activeUpload?.cancel()
        } else if current.state == .pending {
            await mutate(id) { item in
                item.state = .draft
                item.nextAttemptAt = nil
            }
        }
    }

    /// Removes the entry and deletes the container copy of the movie.
    /// A job already running server-side keeps going; its review stays in
    /// the history.
    public func remove(_ id: UUID) async {
        if activeUploadID == id {
            userCancelled.insert(id)
            activeUpload?.cancel()
        }
        trackingTasks[id]?.cancel()
        trackingTasks[id] = nil
        liveProgress[id] = nil
        do {
            try await queueStore.remove(id: id, deleteOwnedFile: true)
        } catch {
            errorMessage = L10n.format("queue.error.persist", error.localizedDescription)
        }
        await reloadItems()
        if selection == .upload(id) {
            selection = .record
        }
    }

    public func removeCompleted() async {
        for item in items where item.state == .completed {
            await remove(item.id)
        }
    }

    /// After purchase or restore: billing-blocked items go back to the queue.
    func releaseBillingBlocked() async {
        do {
            let released = try await queueStore.releaseBillingBlocked()
            guard released > 0 else { return }
        } catch {
            errorMessage = L10n.format("queue.error.persist", error.localizedDescription)
            return
        }
        await reloadItems()
        resumeTracking()
        drain()
    }

    public func clearError() {
        errorMessage = nil
    }

    // MARK: Drain

    /// Uploads ready items one by one until the queue is empty or blocked.
    public func drain() {
        guard isSignedIn else { return }
        guard drainTask == nil else {
            drainAgain = true
            return
        }
        retryTask?.cancel()
        retryTask = nil
        drainGeneration += 1
        let generation = drainGeneration
        drainTask = Task { [weak self] in
            await self?.runDrain()
            guard let self, self.drainGeneration == generation else { return }
            self.drainTask = nil
            if self.drainAgain {
                self.drainAgain = false
                self.drain()
            } else {
                await self.scheduleRetry()
            }
        }
    }

    private func runDrain() async {
        while !Task.isCancelled, isSignedIn {
            guard let next = await queueStore.nextReadyItem() else { return }
            if await process(next) == .halt {
                return
            }
        }
    }

    private func process(_ queued: UploadQueueItem) async -> DrainStep {
        guard let app else { return .halt }
        let client = app.client
        let id = queued.id
        let scratch = queueStore.scratchDirectory
        activeUploadID = id
        defer {
            activeUploadID = nil
            activeUpload = nil
            liveProgress[id] = nil
        }
        guard let started = await mutate(id, { item in
            item.state = .uploading
            item.attemptCount += 1
            item.uploadedBytes = 0
            item.lastError = nil
            item.nextAttemptAt = nil
        }) else {
            return .halt
        }

        let throttle = ProgressThrottle()
        let progress: @Sendable (UploadProgress) -> Void = { [weak self] value in
            guard throttle.shouldForward(value) else { return }
            Task { @MainActor [weak self] in
                self?.applyUploadProgress(id, value)
            }
        }

        let submission = started.submission
        let upload = Task {
            try await client.createJob(submission, scratchDirectory: scratch, progress: progress)
        }
        activeUpload = upload

        do {
            // Sign-out cancels the drain; forward that into the upload task.
            let created = try await withTaskCancellationHandler {
                try await upload.value
            } onCancel: {
                upload.cancel()
            }
            let now = Date()
            let updated = await mutate(id) { item in
                item.apply(created: created, now: now)
            }
            if updated?.state == .processing {
                startTracking(id)
            } else if updated?.state == .completed {
                await jobCompleted()
            }
            return Task.isCancelled ? .halt : .proceed
        } catch {
            return await handleUploadFailure(id: id, attempt: started.attemptCount, error: error)
        }
    }

    private func handleUploadFailure(id: UUID, attempt: Int, error: any Error) async -> DrainStep {
        let cloudError = CloudError.from(error)
        switch cloudError {
        case .cancelled:
            let byUser = userCancelled.remove(id) != nil
            await mutate(id) { item in
                item.state = byUser ? .draft : .pending
                item.attemptCount = max(0, item.attemptCount - 1)
                item.uploadedBytes = 0
            }
            // The user stopped one upload: the rest of the queue continues.
            return byUser && !Task.isCancelled ? .proceed : .halt
        case .paymentRequired(let payload):
            await mutate(id) { item in
                item.state = .billingBlocked
                item.uploadedBytes = 0
                item.lastError = payload.localizedMessage
            }
            app?.presentPaywall(reason: payload)
            return .halt
        case .notAuthenticated, .unauthorized:
            await mutate(id) { item in
                item.state = .pending
                item.attemptCount = max(0, item.attemptCount - 1)
                item.uploadedBytes = 0
            }
            await app?.handleSessionExpired()
            return .halt
        default:
            let message = cloudError.localizedMessage
            if cloudError.isRetryable, attempt < UploadQueueStore.maxAttempts {
                let retryAt = Date().addingTimeInterval(UploadQueueStore.retryDelay(forAttempt: attempt - 1))
                await mutate(id) { item in
                    item.state = .pending
                    item.uploadedBytes = 0
                    item.lastError = message
                    item.nextAttemptAt = retryAt
                }
            } else {
                await mutate(id) { item in
                    item.state = .failed
                    item.uploadedBytes = 0
                    item.lastError = message
                }
            }
            return .proceed
        }
    }

    private func scheduleRetry() async {
        retryTask?.cancel()
        retryTask = nil
        guard isSignedIn, let date = await queueStore.nextRetryDate() else { return }
        let delay = max(1, date.timeIntervalSinceNow)
        retryTask = Task { [weak self] in
            do {
                try await Task.sleep(for: .seconds(delay))
            } catch {
                return
            }
            self?.retryTask = nil
            self?.drain()
        }
    }

    private func applyUploadProgress(_ id: UUID, _ progress: UploadProgress) {
        guard activeUploadID == id, let index = items.firstIndex(where: { $0.id == id }) else { return }
        liveProgress[id] = progress
        items[index].uploadedBytes = progress.sentBytes
        if progress.totalBytes > 0 {
            items[index].totalBytes = progress.totalBytes
        }
    }

    // MARK: Job tracking

    private func resumeTracking() {
        for item in items where item.state == .processing {
            startTracking(item.id)
        }
    }

    /// SSE with polling fallback until the job completes or fails.
    private func startTracking(_ id: UUID) {
        guard isSignedIn, trackingTasks[id] == nil, let app,
              let current = item(id: id), let jobID = current.jobID, let reference = current.jobReference
        else {
            return
        }
        let client = app.client
        trackingTasks[id] = Task { [weak self] in
            var retryLater = false
            do {
                for try await event in client.trackJob(jobID: jobID, statusReference: reference.status, eventsReference: reference.events) {
                    guard let self else { return }
                    let snapshot = event.snapshot
                    let now = Date()
                    let updated = await self.mutate(id) { item in
                        item.apply(snapshot: snapshot, now: now)
                    }
                    if updated == nil {
                        // Removed by the user while tracking.
                        break
                    }
                    if event.isTerminal {
                        if case .completed = event {
                            await self.jobCompleted()
                        }
                        break
                    }
                }
            } catch {
                let cloudError = CloudError.from(error)
                switch cloudError {
                case .cancelled:
                    break
                case .notAuthenticated, .unauthorized:
                    await self?.app?.handleSessionExpired()
                case .paymentRequired(let payload):
                    await self?.mutate(id) { item in
                        item.state = .billingBlocked
                        item.lastError = payload.localizedMessage
                    }
                    self?.app?.presentPaywall(reason: payload)
                default:
                    let message = cloudError.localizedMessage
                    await self?.mutate(id) { item in
                        item.lastError = message
                    }
                    retryLater = true
                }
            }
            guard let self else { return }
            self.trackingTasks[id] = nil
            if retryLater {
                // The job keeps running server-side; look again shortly.
                try? await Task.sleep(for: .seconds(30))
                if !Task.isCancelled, self.item(id: id)?.state == .processing {
                    self.startTracking(id)
                }
            }
        }
    }

    private func jobCompleted() async {
        await app?.refreshProfile()
        await refreshReviews()
    }

    // MARK: Reviews

    /// `GET /api/v1/reviews`; on failure the cached list stays visible.
    public func refreshReviews() async {
        guard let app, app.isSignedIn, !isRefreshingReviews else { return }
        isRefreshingReviews = true
        defer { isRefreshingReviews = false }
        do {
            serverReviews = try await queueStore.refreshRecentReviews(using: app.client)
            reviewsError = nil
        } catch {
            let cloudError = CloudError.from(error)
            switch cloudError {
            case .notAuthenticated, .unauthorized:
                await app.handleSessionExpired()
            case .http(let status, _) where status == 404 || status == 405:
                // History endpoint not deployed: local completions still show.
                reviewsError = nil
            default:
                reviewsError = cloudError.localizedMessage
            }
        }
    }

    // MARK: Persistence helpers

    @discardableResult
    private func mutate(_ id: UUID, _ change: @Sendable (inout UploadQueueItem) -> Void) async -> UploadQueueItem? {
        do {
            let updated = try await queueStore.update(id: id, change)
            await reloadItems()
            return updated
        } catch {
            errorMessage = L10n.format("queue.error.persist", error.localizedDescription)
            return nil
        }
    }

    private func insert(_ item: UploadQueueItem) async {
        do {
            try await queueStore.upsert(item)
        } catch {
            errorMessage = L10n.format("queue.error.persist", error.localizedDescription)
        }
        await reloadItems()
    }

    private func reloadItems() async {
        var loaded = await queueStore.allItems()
        for index in loaded.indices {
            if let progress = liveProgress[loaded[index].id], loaded[index].state == .uploading {
                loaded[index].uploadedBytes = progress.sentBytes
                if progress.totalBytes > 0 {
                    loaded[index].totalBytes = progress.totalBytes
                }
            }
        }
        items = loaded
    }
}

extension UploadQueueItem {
    /// Whether an Upload/Retry button should be offered right now.
    public var canStartUploadNow: Bool {
        state.canStartUpload || (state == .pending && nextAttemptAt != nil)
    }
}
