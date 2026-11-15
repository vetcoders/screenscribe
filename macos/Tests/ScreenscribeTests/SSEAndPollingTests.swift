import Foundation
import ScreenscribeCore
import Testing

@Suite("SSE Event Parser and Polling Fallback")
struct SSEAndPollingTests {
    @Test("SSEEventParser handles split multibyte UTF-8, BOM, comments, CRLF, and terminal JobEvent mapping")
    func sseParserFramingAndSplitUTF8() {
        var parser = SSEEventParser()
        let bom: [UInt8] = [0xEF, 0xBB, 0xBF]
        let frame = ": keepalive\r\nevent: job_progress\r\nid: 7\r\nretry: 1500\r\ndata: {\"job_id\":\"job_1\",\"status\":\"processing\",\"stage\":\"transcribe\",\"progress_pct\":42,\"message\":\"Zażółć gęślą jaźń\"}\r\n\r\n"
        var raw = Data(bom)
        raw.append(Data(frame.utf8))

        // Split right inside multi-byte Polish characters by feeding 5 bytes at a time.
        var emitted: [SSEEvent] = []
        var offset = 0
        while offset < raw.count {
            let end = min(raw.count, offset + 5)
            emitted.append(contentsOf: parser.consume(raw.subdata(in: offset..<end)))
            offset = end
        }
        #expect(parser.keepaliveCount == 1)
        #expect(emitted.count == 1)
        let first = emitted[0]
        #expect(first.event == "job_progress")
        #expect(first.id == "7")
        #expect(first.retry == 1500)

        let jobEvent = JobEvent(sseEvent: first, jobID: "job_1")
        #expect(jobEvent?.isTerminal == false)
        #expect(jobEvent?.snapshot.stage == .transcribe)
        #expect(jobEvent?.snapshot.progressPct == 42)
        #expect(jobEvent?.snapshot.message == "Zażółć gęślą jaźń")
    }

    @Test("ScreenscribeCloudClient.trackJob falls back to polling status_url when SSE disconnects early")
    func trackJobFallbackToPolling() async throws {
        let keychain = InMemoryKeychainStore()
        let sessionStore = AuthSessionStore(keychain: keychain)
        try sessionStore.save(
            AuthSession(
                accessToken: "acc_sse",
                refreshToken: "ref_sse",
                expiresAt: Date().addingTimeInterval(3600),
                userID: "usr_sse"
            )
        )
        let env = try CloudEnvironment(string: "https://cloud.example.test")

        let lock = NSLock()
        nonisolated(unsafe) var statusPoll = 0

        let (urlSession, mockID) = MockURLProtocol.makeSession { req in
            switch (req.method, req.url.path) {
            case ("GET", "/api/v1/jobs/job_77/events"):
                let sse = "event: job_progress\ndata: {\"job_id\":\"job_77\",\"status\":\"processing\",\"stage\":\"audio\",\"progress_pct\":10}\n\n"
                return MockURLProtocol.StubResponse(
                    statusCode: 200,
                    headers: ["Content-Type": "text/event-stream"],
                    chunks: [Data(sse.utf8)]
                )
            case ("GET", "/api/v1/jobs/job_77"):
                let count = lock.withLock { () -> Int in
                    statusPoll += 1
                    return statusPoll
                }
                if count == 1 {
                    return .json([
                        "job_id": "job_77",
                        "status": "processing",
                        "stage": "analyze",
                        "progress_pct": 75,
                    ])
                }
                return .json([
                    "job_id": "job_77",
                    "status": "completed",
                    "stage": "done",
                    "progress_pct": 100,
                    "share_id": "shr_done77",
                    "share_url": "https://cloud.example.test/r/shr_done77",
                    "report_json_url": "/api/v1/reviews/shr_done77/report.json",
                ])
            default:
                return .json(["detail": "Not found"], statusCode: 404)
            }
        }
        defer { MockURLProtocol.unregister(mockID) }

        let client = ScreenscribeCloudClient(environment: env, session: urlSession, sessionStore: sessionStore)
        var stages: [JobStage] = []
        var terminalShare: String?

        let stream = client.trackJob(
            jobID: "job_77",
            statusReference: "/api/v1/jobs/job_77",
            eventsReference: "/api/v1/jobs/job_77/events",
            pollInterval: .milliseconds(5),
            sleep: { _ in }
        )
        for try await event in stream {
            stages.append(event.snapshot.stage)
            if case .completed(let snap) = event {
                terminalShare = snap.shareID
            }
        }
        #expect(stages == [.audio, .analyze, .done])
        #expect(terminalShare == "shr_done77")
    }
}
