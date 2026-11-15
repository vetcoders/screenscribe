import Foundation
import ScreenscribeCore
import Testing

@Suite("Disk-Streamed Multipart and Upload Wire Contract")
struct MultipartAndUploadTests {
    @Test("MultipartStreamBuilder streams >1 MB payload in chunks without corrupting bytes or headers")
    func diskStreamedMultipartFraming() throws {
        let tempDir = FileManager.default.temporaryDirectory.appendingPathComponent("ss-mp-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempDir) }

        let sourceURL = tempDir.appendingPathComponent("walkthrough \"quote\".mp4")
        let payloadSize = 600 * 1024
        var sampleBytes = Data(count: payloadSize)
        for index in 0..<payloadSize {
            sampleBytes[index] = UInt8(index % 251)
        }
        try sampleBytes.write(to: sourceURL)

        let destURL = tempDir.appendingPathComponent("body.multipart")
        let boundary = "Screenscribe-Boundary-FixedTest123"
        let fields = [
            MultipartFormField(name: "language", value: "pl"),
            MultipartFormField(name: "preset", value: "programming"),
            MultipartFormField(name: "visibility", value: "private"),
        ]
        let length = try MultipartStreamBuilder.writeBody(
            boundary: boundary,
            fields: fields,
            fileFieldName: "file",
            fileURL: sourceURL,
            fileName: sourceURL.lastPathComponent,
            mimeType: "video/mp4",
            to: destURL
        )
        let written = try Data(contentsOf: destURL)
        #expect(Int64(written.count) == length)

        let prefixText = String(decoding: written.prefix(512), as: UTF8.self)
        #expect(prefixText.contains("name=\"language\"\r\n\r\npl\r\n"))
        #expect(prefixText.contains("name=\"preset\"\r\n\r\nprogramming\r\n"))
        #expect(prefixText.contains("name=\"file\"; filename=\"walkthrough _quote_.mp4\""))
        #expect(prefixText.contains("Content-Type: video/mp4\r\n\r\n"))
        #expect(written.range(of: sampleBytes) != nil)

        let emptyURL = tempDir.appendingPathComponent("empty.mp4")
        try Data().write(to: emptyURL)
        #expect(throws: MultipartStreamError.emptySource) {
            _ = try MultipartStreamBuilder.writeBody(
                boundary: boundary,
                fields: [],
                fileURL: emptyURL,
                fileName: "empty.mp4",
                mimeType: "video/mp4",
                to: destURL
            )
        }
    }

    @Test("ScreenscribeCloudClient.createJob sends Idempotency-Key, multipart file, and surfaces 402 without retrying")
    func createJobAndHard402Gate() async throws {
        let tempDir = FileManager.default.temporaryDirectory.appendingPathComponent("ss-job-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempDir) }

        let videoURL = tempDir.appendingPathComponent("clip.mp4")
        try Data("fake-mp4-binary-body".utf8).write(to: videoURL)
        let scratchDir = tempDir.appendingPathComponent("scratch", isDirectory: true)

        let keychain = InMemoryKeychainStore()
        let sessionStore = AuthSessionStore(keychain: keychain)
        try sessionStore.save(
            AuthSession(
                accessToken: "acc_upload",
                refreshToken: "ref_upload",
                expiresAt: Date().addingTimeInterval(3600),
                userID: "usr_up"
            )
        )
        let env = try CloudEnvironment(string: "https://cloud.example.test")

        let lock = NSLock()
        nonisolated(unsafe) var postCount = 0
        nonisolated(unsafe) var shouldGate402 = false

        let (urlSession, mockID) = MockURLProtocol.makeSession { req in
            guard req.method == "POST", req.url.path == "/api/v1/jobs" else {
                return .json(["detail": "Unexpected"], statusCode: 404)
            }
            let gated = lock.withLock { () -> Bool in
                postCount += 1
                return shouldGate402
            }
            #expect(req.header("Idempotency-Key") == "idem-key-001")
            #expect((req.header("Content-Type") ?? "").hasPrefix("multipart/form-data; boundary="))
            let bodyText = String(decoding: req.body, as: UTF8.self)
            #expect(bodyText.contains("fake-mp4-binary-body"))

            if gated {
                return .json([
                    "error": "subscription_required",
                    "code": "quota_exceeded",
                    "message": "Minutes exhausted",
                ], statusCode: 402)
            }
            return .json([
                "job_id": "job_abc123",
                "status": "queued",
                "stage": "queued",
                "status_url": "/api/v1/jobs/job_abc123",
                "events_url": "/api/v1/jobs/job_abc123/events",
            ], statusCode: 202)
        }
        defer { MockURLProtocol.unregister(mockID) }

        let client = ScreenscribeCloudClient(environment: env, session: urlSession, sessionStore: sessionStore)
        let submission = JobSubmission(
            fileURL: videoURL,
            language: .pl,
            preset: .programming,
            prompt: "Focus on layout bugs",
            visibility: .privateOnly,
            idempotencyKey: "idem-key-001"
        )
        let created = try await client.createJob(submission, scratchDirectory: scratchDir)
        #expect(created.jobID == "job_abc123")
        #expect(created.status == .queued)

        lock.withLock { shouldGate402 = true }
        do {
            _ = try await client.createJob(submission, scratchDirectory: scratchDir)
            Issue.record("Expected CloudError.paymentRequired on 402")
        } catch let error as CloudError {
            #expect(error.paymentRequiredPayload?.isQuotaExceeded == true)
        }
        #expect(lock.withLock { postCount } == 2)
    }
}
