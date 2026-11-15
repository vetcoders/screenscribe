import CoreGraphics
import Foundation
import ScreenscribeCore
import Testing

final class FakeCaptureService: ScreenCaptureServiceProtocol, @unchecked Sendable {
    var catalog = CaptureSourceCatalog(
        displays: [DisplaySource(id: 1, name: "Built-in Retina Display", width: 1440, height: 900)]
    )
    var mics: [MicrophoneSource] = [MicrophoneSource(id: "mic-1", name: "Built-in Microphone")]
    private let lock = NSLock()
    private(set) var startedRequests: [CaptureRequest] = []
    private(set) var pausedCount = 0
    private(set) var resumedCount = 0

    func availableSources() async throws -> CaptureSourceCatalog { catalog }
    func microphones() -> [MicrophoneSource] { mics }

    func startRecording(_ request: CaptureRequest) async throws {
        lock.withLock { startedRequests.append(request) }
        try Data("recorded-mp4-bytes".utf8).write(to: request.outputURL)
    }

    func pauseRecording() async {
        lock.withLock { pausedCount += 1 }
    }

    func resumeRecording() async {
        lock.withLock { resumedCount += 1 }
    }

    func stopRecording() async throws -> RecordingResult {
        let req = lock.withLock { startedRequests.last }!
        return RecordingResult(
            fileURL: req.outputURL,
            duration: 3.2,
            fileSize: 18,
            width: 1440,
            height: 900,
            codec: req.options.codec,
            hasSystemAudio: req.options.capturesSystemAudio,
            hasMicrophone: req.options.capturesMicrophone
        )
    }

    func cancelRecording() async {}

    func events() -> AsyncStream<CaptureEvent> {
        AsyncStream { $0.finish() }
    }
}

struct FakePermissions: PermissionPreflightProtocol {
    var state = PermissionSnapshot(screenRecording: .granted, microphone: .granted)
    func snapshot() -> PermissionSnapshot { state }
    func requestScreenRecording() -> Bool { state.canRecordScreen }
    func requestMicrophone() async -> Bool { state.microphone.isGranted }
}

struct FakeMediaInspector: MediaInspectorProtocol {
    func inspect(_ url: URL) async throws -> ValidatedVideo {
        ValidatedVideo(url: url, displayName: url.lastPathComponent, duration: 2.0, fileSize: 128, width: 160, height: 120, audioTrackCount: 1)
    }

    func importFile(at url: URL, into directory: URL) async throws -> ValidatedVideo {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let dst = directory.appendingPathComponent(url.lastPathComponent)
        try? FileManager.default.removeItem(at: dst)
        try FileManager.default.copyItem(at: url, to: dst)
        return ValidatedVideo(url: dst, displayName: url.lastPathComponent, duration: 2.0, fileSize: 128, width: 160, height: 120, audioTrackCount: 1)
    }
}

@Suite("MainActor ViewModels and Cloud Origin Switching")
struct ViewModelStateTests {
    @MainActor
    @Test("AppModel records with 0s countdown, enqueues recording, parks on 402 paywall, and signs out on origin switch")
    func endToEndAppModelStateFlow() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("ss-vm-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let suiteName = "io.vetcoders.screenscribe.tests.\(UUID().uuidString)"
        let configStore = AppConfigurationStore(suiteName: suiteName)
        defer { UserDefaults.standard.removePersistentDomain(forName: suiteName) }

        var initialConfig = AppConfiguration.standard
        initialConfig.cloudBaseURL = "https://cloud.example.test"
        initialConfig.countdownSeconds = 0
        initialConfig.autoUploadRecordings = true
        configStore.save(initialConfig)

        let keychain = InMemoryKeychainStore()
        let sessionStore = AuthSessionStore(keychain: keychain)
        try sessionStore.save(
            AuthSession(
                accessToken: "acc_vm",
                refreshToken: "ref_vm",
                expiresAt: Date().addingTimeInterval(3600),
                userID: "usr_vm",
                email: "vm@example.test"
            )
        )

        let (urlSession, mockID) = MockURLProtocol.makeSession { req in
            switch (req.method, req.url.path) {
            case ("GET", "/api/v1/auth/me"):
                return .json([
                    "user_id": "usr_vm",
                    "email": "vm@example.test",
                    "subscription": ["active": false, "plan": "none", "minutes_limit": 0, "minutes_used": 0],
                ])
            case ("GET", "/api/v1/reviews"):
                return .json(["reviews": []])
            case ("POST", "/api/v1/jobs"):
                return .json([
                    "error": "subscription_required",
                    "code": "no_active_subscription",
                    "message": "Subscription required",
                ], statusCode: 402)
            case ("POST", "/api/v1/auth/logout"):
                return .json(["ok": true])
            default:
                return .json(["detail": "Not found"], statusCode: 404)
            }
        }
        defer { MockURLProtocol.unregister(mockID) }

        let capture = FakeCaptureService()
        let storeKit = FakeStoreKitClient()
        let deps = AppDependencies(
            configurationStore: configStore,
            sessionStore: sessionStore,
            queueStore: UploadQueueStore(rootDirectory: root),
            capture: capture,
            permissions: FakePermissions(),
            mediaInspector: FakeMediaInspector(),
            storeKit: storeKit,
            urlSession: urlSession
        )

        let app = AppModel(dependencies: deps)
        await app.start()
        #expect(app.isSignedIn)
        #expect(app.accountManagementURL.absoluteString == "https://cloud.example.test/account")

        await app.recorder.refreshSources()
        #expect(app.recorder.canStart)

        app.recorder.startRecording()
        for _ in 0..<40 where app.recorder.state != .recording {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(app.recorder.state == .recording)

        await app.recorder.pause()
        #expect(app.recorder.state == .paused)
        await app.recorder.resume()
        #expect(app.recorder.state == .recording)

        await app.recorder.stopRecording()
        #expect(app.recorder.state == .idle)

        for _ in 0..<60 where !app.workspace.hasBillingBlockedItems {
            try await Task.sleep(for: .milliseconds(15))
        }
        #expect(app.workspace.hasBillingBlockedItems)
        #expect(app.presentedSheet == .paywall)

        let switched = try await app.applyCloudURL("http://127.0.0.1:8787")
        #expect(switched.baseURL.absoluteString == "http://127.0.0.1:8787")
        #expect(!app.isSignedIn)
    }
}
