import Foundation
import ScreenscribeCore
import Testing

@Suite("Upload Queue Crash Recovery and VideoFileValidator")
struct QueueAndCaptureTests {
    @Test("UploadQueueStore recovers mid-upload items, preserves idempotencyKey, releases billingBlocked, and finds orphans")
    func queueCrashRecoveryAndBillingRelease() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("ss-queue-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let store = UploadQueueStore(rootDirectory: root)
        try await store.prepare()

        let recA = await store.recordingsDirectory.appendingPathComponent("recA.mp4")
        let recB = await store.recordingsDirectory.appendingPathComponent("recB.mp4")
        let orphan = await store.recordingsDirectory.appendingPathComponent("orphan_crash.mp4")
        try Data("a".utf8).write(to: recA)
        try Data("b".utf8).write(to: recB)
        try Data("c".utf8).write(to: orphan)

        let scratchLeftover = await store.scratchDirectory.appendingPathComponent("stale.multipart")
        try Data("stale".utf8).write(to: scratchLeftover)

        var itemUploadingNoJob = UploadQueueItem(
            sourceFileURL: recA,
            origin: .recording,
            language: .en,
            preset: .programming,
            visibility: .privateOnly,
            totalBytes: 1000
        )
        let originalIdempotencyKey = itemUploadingNoJob.idempotencyKey
        itemUploadingNoJob.state = .uploading
        itemUploadingNoJob.uploadedBytes = 500

        var itemBlocked = UploadQueueItem(
            sourceFileURL: recB,
            origin: .imported,
            language: .pl,
            preset: .veterinary,
            visibility: .unlisted,
            totalBytes: 2000
        )
        itemBlocked.state = .billingBlocked
        itemBlocked.lastError = "No active subscription"

        try await store.upsert(itemUploadingNoJob)
        try await store.upsert(itemBlocked)

        // Simulate app relaunch with a fresh store instance pointed at the same directory.
        let relaunched = UploadQueueStore(rootDirectory: root)
        let recovered = try await relaunched.recoverAfterLaunch()
        #expect(recovered.count == 2)
        #expect(!FileManager.default.fileExists(atPath: scratchLeftover.path))

        let reloadedA = try #require(await relaunched.item(id: itemUploadingNoJob.id))
        #expect(reloadedA.state == .pending)
        #expect(reloadedA.idempotencyKey == originalIdempotencyKey)
        #expect(reloadedA.uploadedBytes == 0)

        let orphans = await relaunched.orphanedRecordings()
        #expect(orphans.map(\.lastPathComponent) == ["orphan_crash.mp4"])

        let released = try await relaunched.releaseBillingBlocked()
        #expect(released == 1)
        let reloadedB = try #require(await relaunched.item(id: itemBlocked.id))
        #expect(reloadedB.state == .pending)
    }

    @Test("VideoFileValidator inspects real AVAssetWriter MP4 with dual audio tracks and rejects silent or unsupported files")
    func videoValidatorChecks() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("ss-vid-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let validator = VideoFileValidator()

        let badExt = dir.appendingPathComponent("notes.txt")
        try Data("hello".utf8).write(to: badExt)
        await #expect(throws: VideoValidationError.unsupportedType("txt")) {
            _ = try await validator.inspect(badExt)
        }

        let silentMP4 = dir.appendingPathComponent("silent.mp4")
        try await SyntheticVideoGenerator.writeMovie(
            to: silentMP4,
            durationSeconds: 1.4,
            includeVideo: true,
            audioTrackCount: 0
        )
        await #expect(throws: VideoValidationError.noAudioTrack) {
            _ = try await validator.inspect(silentMP4)
        }

        let validMP4 = dir.appendingPathComponent("dual_audio.mp4")
        try await SyntheticVideoGenerator.writeMovie(
            to: validMP4,
            durationSeconds: 1.5,
            includeVideo: true,
            audioTrackCount: 2
        )
        let inspected = try await validator.inspect(validMP4)
        #expect(inspected.width == 160)
        #expect(inspected.height == 120)
        #expect(inspected.audioTrackCount == 2)
        #expect(inspected.duration >= 1.0)

        let importsDir = dir.appendingPathComponent("Imports", isDirectory: true)
        let imported = try await validator.importFile(at: validMP4, into: importsDir)
        #expect(imported.url.deletingLastPathComponent().standardizedFileURL == importsDir.standardizedFileURL)
        #expect(FileManager.default.fileExists(atPath: imported.url.path))
    }
}
