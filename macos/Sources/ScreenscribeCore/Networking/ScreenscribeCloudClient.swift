import Foundation

// MARK: - Errors

/// Every failure the Cloud client surfaces to the UI.
public enum CloudError: Error, Sendable, Equatable, LocalizedError {
    case notAuthenticated
    case unauthorized
    /// `402` hard subscription gate — present the StoreKit paywall.
    case paymentRequired(PaymentRequiredPayload)
    case http(status: Int, message: String?)
    case invalidResponse
    case decoding(String)
    case invalidURL(String)
    /// A server reference pointed at another origin; no Bearer was sent.
    case crossOrigin(String)
    case transport(String)
    case deviceAuthorizationExpired
    case deviceAuthorizationDenied
    case cancelled

    /// Transient: worth retrying with backoff.
    public var isRetryable: Bool {
        switch self {
        case .transport, .invalidResponse:
            return true
        case .http(let status, _):
            return status == 408 || status == 425 || status == 429 || status >= 500
        default:
            return false
        }
    }

    /// The credentials themselves were rejected (sign-in required).
    public var isTerminalAuthFailure: Bool {
        switch self {
        case .notAuthenticated, .unauthorized:
            return true
        case .http(let status, _):
            return status == 400 || status == 401 || status == 403
        default:
            return false
        }
    }

    public var paymentRequiredPayload: PaymentRequiredPayload? {
        if case .paymentRequired(let payload) = self {
            return payload
        }
        return nil
    }

    public var localizedMessage: String {
        switch self {
        case .notAuthenticated, .unauthorized:
            return L10n.string("error.auth.required")
        case .paymentRequired(let payload):
            return payload.localizedMessage
        case .http(let status, let message):
            if let message, !message.isEmpty {
                return L10n.format("error.http.message", status, message)
            }
            return L10n.format("error.http.status", status)
        case .invalidResponse:
            return L10n.string("error.response.invalid")
        case .decoding:
            return L10n.string("error.response.decoding")
        case .invalidURL:
            return L10n.string("error.url.invalid")
        case .crossOrigin:
            return L10n.string("error.url.crossorigin")
        case .transport(let message):
            return L10n.format("error.network", message)
        case .deviceAuthorizationExpired:
            return L10n.string("signin.device.expired")
        case .deviceAuthorizationDenied:
            return L10n.string("signin.device.denied")
        case .cancelled:
            return L10n.string("error.cancelled")
        }
    }

    public var errorDescription: String? {
        localizedMessage
    }

    public static func from(_ error: any Error) -> CloudError {
        if let cloudError = error as? CloudError {
            return cloudError
        }
        if error is CancellationError {
            return .cancelled
        }
        if let urlError = error as? URLError {
            if urlError.code == .cancelled {
                return .cancelled
            }
            return .transport(urlError.localizedDescription)
        }
        if error is DecodingError {
            return .decoding(String(describing: error))
        }
        if let localized = error as? LocalizedError, let description = localized.errorDescription {
            return .transport(description)
        }
        return .transport(error.localizedDescription)
    }
}

// MARK: - Endpoints

/// Canonical `v1` routes (`SHARED_WIRE_CONTRACT.md` §3).
public enum CloudEndpoint {
    public static let authApple = "/api/v1/auth/apple"
    public static let authRegister = "/api/v1/auth/register"
    public static let authLogin = "/api/v1/auth/login"
    public static let authLogout = "/api/v1/auth/logout"
    public static let authMe = "/api/v1/auth/me"
    public static let deviceCode = "/api/v1/auth/device/code"
    public static let deviceToken = "/api/v1/auth/device/token"
    public static let tokenRefresh = "/api/v1/auth/token/refresh"
    public static let appStoreSync = "/api/v1/billing/appstore/sync"
    public static let jobs = "/api/v1/jobs"
    public static let reviews = "/api/v1/reviews"
    /// Web account page (sign-in, data export, account deletion).
    public static let account = "/account"

    public static func job(_ jobID: String) -> String {
        "/api/v1/jobs/\(pathSegment(jobID))"
    }

    public static func jobEvents(_ jobID: String) -> String {
        "/api/v1/jobs/\(pathSegment(jobID))/events"
    }

    public static func reviewReport(_ shareID: String) -> String {
        "/api/v1/reviews/\(pathSegment(shareID))/report.json"
    }

    public static func sharePage(_ shareID: String) -> String {
        "/r/\(pathSegment(shareID))"
    }

    /// Identifiers are single path segments: separators are dropped so a
    /// hostile id can never traverse to another route.
    static func pathSegment(_ value: String) -> String {
        value.replacingOccurrences(of: "/", with: "").replacingOccurrences(of: "..", with: "")
    }
}

/// Generic error body (`{"message": …}`, `{"detail": …}`, `{"error": …}`).
struct ServerErrorBody: Decodable {
    let message: String?

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        message = container.lossyNonEmptyString(AnyCodingKey("message"))
            ?? container.lossyNonEmptyString(AnyCodingKey("detail"))
            ?? container.lossyNonEmptyString(AnyCodingKey("error_description"))
            ?? container.lossyNonEmptyString(AnyCodingKey("error"))
    }

    static func message(from data: Data) -> String? {
        guard !data.isEmpty else { return nil }
        if let body = try? JSONDecoder().decode(ServerErrorBody.self, from: data), let message = body.message {
            return String(message.prefix(300))
        }
        return nil
    }
}

// MARK: - Upload plumbing

/// Forwards `didSendBodyData` as `UploadProgress`.
final class UploadProgressDelegate: NSObject, URLSessionTaskDelegate, Sendable {
    let handler: (@Sendable (UploadProgress) -> Void)?

    init(handler: (@Sendable (UploadProgress) -> Void)?) {
        self.handler = handler
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didSendBodyData bytesSent: Int64,
        totalBytesSent: Int64,
        totalBytesExpectedToSend: Int64
    ) {
        handler?(UploadProgress(sentBytes: totalBytesSent, totalBytes: totalBytesExpectedToSend))
    }
}

/// Lets Swift task cancellation cancel the underlying `URLSessionUploadTask`.
final class UploadTaskHandle: @unchecked Sendable {
    private let lock = NSLock()
    private var task: URLSessionTask?
    private var isCancelled = false

    /// Returns `false` when cancellation already happened.
    func attach(_ task: URLSessionTask) -> Bool {
        lock.withLock {
            guard !isCancelled else { return false }
            self.task = task
            return true
        }
    }

    func cancel() {
        let task: URLSessionTask? = lock.withLock {
            isCancelled = true
            return self.task
        }
        task?.cancel()
    }
}

// MARK: - Client

/// Typed client for the Band 2 hosted backend.
///
/// - Bearer tokens are attached only to same-origin requests.
/// - `401` triggers exactly one single-flight refresh and one retry.
/// - `402` surfaces as `CloudError.paymentRequired` and is never retried.
public final class ScreenscribeCloudClient: Sendable {
    /// RFC 8628 `client_id` of the macOS app (the CLI uses `screenscribe-cli`).
    public static let deviceClientID = "screenscribe-macos"
    public static let deviceGrantType = DeviceTokenRequest.grantType
    /// RFC 8628 §3.5: add 5 seconds to the interval on `slow_down`.
    public static let slowDownIncrement = 5

    public static let defaultUserAgent: String = {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
        let os = ProcessInfo.processInfo.operatingSystemVersion
        return "Screenscribe-macOS/\(version) (macOS \(os.majorVersion).\(os.minorVersion))"
    }()

    public let environment: CloudEnvironment
    public let session: URLSession
    public let sessionStore: AuthSessionStore
    public let tokens: TokenRefreshCoordinator
    public let uploadFieldName: String

    public init(
        environment: CloudEnvironment,
        session: URLSession = ScreenscribeCloudClient.makeDefaultSession(),
        sessionStore: AuthSessionStore = AuthSessionStore(),
        uploadFieldName: String = MultipartStreamBuilder.defaultFileFieldName
    ) {
        self.environment = environment
        self.session = session
        self.sessionStore = sessionStore
        self.uploadFieldName = uploadFieldName
        let refreshEnvironment = environment
        let refreshSession = session
        self.tokens = TokenRefreshCoordinator(
            store: sessionStore,
            refresher: { refreshToken in
                try await ScreenscribeCloudClient.performRefresh(
                    refreshToken: refreshToken,
                    environment: refreshEnvironment,
                    session: refreshSession
                )
            }
        )
    }

    public static func makeDefaultSession() -> URLSession {
        let configuration = URLSessionConfiguration.default
        configuration.waitsForConnectivity = false
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 6 * 60 * 60
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        var headers: [String: String] = ["User-Agent": defaultUserAgent]
        if let language = Locale.preferredLanguages.first {
            headers["Accept-Language"] = language
        }
        configuration.httpAdditionalHeaders = headers
        return URLSession(configuration: configuration)
    }

    // MARK: Session state

    public func currentSession() async -> AuthSession? {
        await tokens.currentSession()
    }

    public func isSignedIn() async -> Bool {
        await tokens.isSignedIn
    }

    // MARK: Authentication

    public func signInWithApple(identityToken: String, email: String?, fullName: String?) async throws -> AuthSession {
        let body = AppleSignInRequest(identityToken: identityToken, email: email, fullName: fullName)
        let request = try jsonRequest(CloudEndpoint.authApple, body: body)
        let (data, http) = try await send(request)
        try Self.validate(data, http)
        return try await establish(Self.decode(AuthTokenResponse.self, from: data))
    }

    public func login(email: String, password: String) async throws -> AuthSession {
        let request = try jsonRequest(CloudEndpoint.authLogin, body: EmailCredentialsRequest(email: email, password: password))
        let (data, http) = try await send(request)
        try Self.validate(data, http)
        return try await establish(Self.decode(AuthTokenResponse.self, from: data))
    }

    /// Registers, then signs in when the server returns the user without tokens.
    public func register(email: String, password: String) async throws -> AuthSession {
        let request = try jsonRequest(CloudEndpoint.authRegister, body: EmailCredentialsRequest(email: email, password: password))
        let (data, http) = try await send(request)
        try Self.validate(data, http)
        if let response = try? WireCoding.makeDecoder().decode(AuthTokenResponse.self, from: data) {
            return try await establish(response)
        }
        return try await login(email: email, password: password)
    }

    /// Purges the Keychain first, then tells the server (best effort): a
    /// network failure never leaves credentials behind.
    public func logout() async {
        let previous = await tokens.currentSession()
        await tokens.invalidate()
        guard let previous else { return }
        do {
            var request = try jsonRequest(CloudEndpoint.authLogout, body: LogoutRequest(refreshToken: previous.refreshToken))
            request.timeoutInterval = 10
            try attachBearer(previous.accessToken, to: &request)
            _ = try await send(request)
        } catch {
            // Local sign-out already happened; server-side revocation is best effort.
        }
    }

    // MARK: Device authorization (RFC 8628)

    public func requestDeviceCode() async throws -> DeviceCodeResponse {
        let request = try jsonRequest(CloudEndpoint.deviceCode, body: DeviceCodeRequest(clientID: Self.deviceClientID))
        let (data, http) = try await send(request)
        try Self.validate(data, http)
        return try Self.decode(DeviceCodeResponse.self, from: data)
    }

    public func pollDeviceToken(deviceCode: String) async throws -> DeviceTokenPollResult {
        let body = DeviceTokenRequest(deviceCode: deviceCode, clientID: Self.deviceClientID)
        let request = try jsonRequest(CloudEndpoint.deviceToken, body: body)
        let (data, http) = try await send(request)
        if (200..<300).contains(http.statusCode) {
            return try .authorized(Self.decode(AuthTokenResponse.self, from: data))
        }
        if (400..<500).contains(http.statusCode),
           let error = try? JSONDecoder().decode(OAuthErrorBody.self, from: data),
           let result = error.pollResult {
            return result
        }
        try Self.validate(data, http)
        throw CloudError.invalidResponse
    }

    /// Polls until the user approves on the web, honouring `interval`,
    /// `slow_down` (+5 s) and `expires_in`. Transient network errors keep polling.
    public func completeDeviceAuthorization(
        _ code: DeviceCodeResponse,
        sleep: @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) async throws -> AuthSession {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(code.expiresIn))
        var interval = max(DeviceCodeResponse.minimumInterval, code.interval)
        while true {
            try Task.checkCancellation()
            do {
                try await sleep(.seconds(interval))
            } catch {
                throw CloudError.cancelled
            }
            guard clock.now < deadline else {
                throw CloudError.deviceAuthorizationExpired
            }
            let result: DeviceTokenPollResult
            do {
                result = try await pollDeviceToken(deviceCode: code.deviceCode)
            } catch {
                let cloudError = CloudError.from(error)
                if cloudError.isRetryable {
                    continue
                }
                throw cloudError
            }
            switch result {
            case .authorized(let response):
                return try await establish(response)
            case .pending:
                continue
            case .slowDown:
                interval += Self.slowDownIncrement
            case .expired:
                throw CloudError.deviceAuthorizationExpired
            case .denied:
                throw CloudError.deviceAuthorizationDenied
            }
        }
    }

    // MARK: Account & billing

    public func fetchProfile() async throws -> UserProfile {
        let (data, http) = try await authorized { try self.request(CloudEndpoint.authMe) }
        try Self.validate(data, http)
        return try Self.decode(UserProfile.self, from: data)
    }

    /// Posts a verified StoreKit 2 JWS so Band 2 can upsert the entitlement.
    /// Must succeed before the transaction is finished.
    public func syncAppStoreTransaction(_ transaction: VerifiedTransaction) async throws -> UserProfile {
        let body = AppStoreSyncRequest(transaction: transaction)
        let (data, http) = try await authorized { try self.jsonRequest(CloudEndpoint.appStoreSync, body: body) }
        try Self.validate(data, http)
        return try Self.decode(UserProfile.self, from: data)
    }

    public var accountManagementURL: URL {
        environment.endpoint(CloudEndpoint.account)
    }

    // MARK: Jobs

    /// Streams `submission` as multipart into `scratchDirectory`, uploads it
    /// with `uploadTask(with:fromFile:)` and expects `202 Accepted`.
    public func createJob(
        _ submission: JobSubmission,
        scratchDirectory: URL,
        progress: (@Sendable (UploadProgress) -> Void)? = nil
    ) async throws -> JobCreateResponse {
        let boundary = MultipartStreamBuilder.makeBoundary()
        let fieldName = uploadFieldName
        let bodyURL = scratchDirectory.appendingPathComponent("upload-\(submission.idempotencyKey).multipart")
        defer { try? FileManager.default.removeItem(at: bodyURL) }

        let bodyLength = try await Self.runBlocking {
            try FileManager.default.createDirectory(at: scratchDirectory, withIntermediateDirectories: true)
            let scoped = submission.fileURL.startAccessingSecurityScopedResource()
            defer {
                if scoped { submission.fileURL.stopAccessingSecurityScopedResource() }
            }
            return try MultipartStreamBuilder.writeBody(
                boundary: boundary,
                fields: submission.formFields,
                fileFieldName: fieldName,
                fileURL: submission.fileURL,
                fileName: submission.fileName,
                mimeType: submission.mimeType,
                to: bodyURL
            )
        }
        try Task.checkCancellation()

        let build: @Sendable () throws -> URLRequest = { [environment] in
            var request = URLRequest(url: environment.endpoint(CloudEndpoint.jobs))
            request.httpMethod = "POST"
            request.timeoutInterval = 120
            request.setValue(MultipartStreamBuilder.contentType(boundary: boundary), forHTTPHeaderField: "Content-Type")
            request.setValue(String(bodyLength), forHTTPHeaderField: "Content-Length")
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            request.setValue(submission.idempotencyKey, forHTTPHeaderField: "Idempotency-Key")
            return request
        }

        var token = try await tokens.validAccessToken()
        var request = try build()
        try attachBearer(token, to: &request)
        var (data, http) = try await performUpload(request, fromFile: bodyURL, progress: progress)
        if http.statusCode == 401 {
            let refreshed = try await tokens.refreshedSession(rejectedAccessToken: token)
            token = refreshed.accessToken
            request = try build()
            try attachBearer(token, to: &request)
            (data, http) = try await performUpload(request, fromFile: bodyURL, progress: progress)
            if http.statusCode == 401 {
                await tokens.invalidate()
                throw CloudError.notAuthenticated
            }
        }
        try Self.validate(data, http)
        return try Self.decode(JobCreateResponse.self, from: data)
    }

    public func jobStatus(reference: String) async throws -> JobStatusResponse {
        let url = try environment.requireSameOrigin(reference)
        let (data, http) = try await authorized { try self.request(url: url) }
        try Self.validate(data, http)
        return try Self.decode(JobStatusResponse.self, from: data)
    }

    public func jobStatus(jobID: String) async throws -> JobStatusResponse {
        try await jobStatus(reference: CloudEndpoint.job(jobID))
    }

    /// Server-sent events for one job via `URLSession.bytes(for:)`. Finishes
    /// after the first terminal event or when the server closes the stream.
    public func jobEvents(reference: String, jobID: String) -> AsyncThrowingStream<JobEvent, any Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let url = try self.environment.requireSameOrigin(reference)
                    let token = try await self.tokens.validAccessToken()
                    var request = try self.request(url: url)
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
                    request.timeoutInterval = 300
                    try self.attachBearer(token, to: &request)

                    let (bytes, response) = try await self.session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw CloudError.invalidResponse
                    }
                    if http.statusCode == 401 {
                        _ = try? await self.tokens.refreshedSession(rejectedAccessToken: token)
                        throw CloudError.unauthorized
                    }
                    try Self.validate(Data(), http)

                    var parser = SSEEventParser()
                    for try await byte in bytes {
                        guard let event = parser.consume(byte: byte),
                              let jobEvent = JobEvent(sseEvent: event, jobID: jobID)
                        else {
                            continue
                        }
                        continuation.yield(jobEvent)
                        if jobEvent.isTerminal {
                            continuation.finish()
                            return
                        }
                    }
                    if let event = parser.finish(dispatchIncomplete: true),
                       let jobEvent = JobEvent(sseEvent: event, jobID: jobID) {
                        continuation.yield(jobEvent)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: CloudError.from(error))
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    /// SSE first; if the stream fails or ends early, falls back to polling
    /// `status_url` until a terminal state. `402` and auth failures stop it.
    public func trackJob(
        jobID: String,
        statusReference: String,
        eventsReference: String,
        pollInterval: Duration = .seconds(3),
        sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) -> AsyncThrowingStream<JobEvent, any Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    for try await event in self.jobEvents(reference: eventsReference, jobID: jobID) {
                        continuation.yield(event)
                        if event.isTerminal {
                            continuation.finish()
                            return
                        }
                    }
                } catch {
                    let cloudError = CloudError.from(error)
                    switch cloudError {
                    case .cancelled, .paymentRequired, .notAuthenticated, .crossOrigin, .invalidURL:
                        continuation.finish(throwing: cloudError)
                        return
                    default:
                        break
                    }
                }

                var failures = 0
                var lastSnapshot: JobStatusResponse?
                while !Task.isCancelled {
                    do {
                        let status = try await self.jobStatus(reference: statusReference)
                        failures = 0
                        if status != lastSnapshot {
                            lastSnapshot = status
                            let event = JobEvent(snapshot: status)
                            continuation.yield(event)
                            if event.isTerminal {
                                continuation.finish()
                                return
                            }
                        }
                    } catch {
                        let cloudError = CloudError.from(error)
                        guard cloudError.isRetryable, failures < 6 else {
                            continuation.finish(throwing: cloudError)
                            return
                        }
                        failures += 1
                    }
                    do {
                        try await sleep(pollInterval * (failures + 1))
                    } catch {
                        break
                    }
                }
                continuation.finish(throwing: CloudError.cancelled)
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    // MARK: Reviews

    public func recentReviews(limit: Int = 50) async throws -> [ReviewHistoryItem] {
        let query = [URLQueryItem(name: "limit", value: String(max(1, limit)))]
        let (data, http) = try await authorized { try self.request(CloudEndpoint.reviews, query: query) }
        try Self.validate(data, http)
        return try Self.decode(ReviewListResponse.self, from: data).items
    }

    public func reviewReport(reference: String) async throws -> ReviewReportSummary {
        let url = try environment.requireSameOrigin(reference)
        let (data, http) = try await authorized { try self.request(url: url) }
        try Self.validate(data, http)
        return try Self.decode(ReviewReportSummary.self, from: data)
    }

    /// Hosted review page: the server `share_url` when present, else `/r/{share_id}`.
    public func shareURL(shareID: String, shareURL: String? = nil) -> URL {
        if let shareURL, let url = environment.resolve(shareURL), url.scheme == "https" || environment.isSameOrigin(url) {
            return url
        }
        return environment.endpoint(CloudEndpoint.sharePage(shareID))
    }

    /// Request for `WKWebView`. The Bearer is attached only when `url` is on
    /// the configured origin — never to a third-party host.
    public func shareRequest(for url: URL) async -> URLRequest {
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadRevalidatingCacheData
        if environment.isSameOrigin(url), let token = try? await tokens.validAccessToken() {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    // MARK: - Internals

    func request(_ path: String, method: String = "GET", query: [URLQueryItem] = []) throws -> URLRequest {
        try request(url: environment.endpoint(path, query: query), method: method)
    }

    func request(url: URL, method: String = "GET") throws -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    func jsonRequest<Body: Encodable>(_ path: String, method: String = "POST", body: Body) throws -> URLRequest {
        var request = try self.request(path, method: method)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            request.httpBody = try WireCoding.makeEncoder().encode(body)
        } catch {
            throw CloudError.decoding(String(describing: error))
        }
        return request
    }

    func attachBearer(_ token: String, to request: inout URLRequest) throws {
        guard let url = request.url, environment.isSameOrigin(url) else {
            throw CloudError.crossOrigin(request.url?.absoluteString ?? "")
        }
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw CloudError.invalidResponse
            }
            return (data, http)
        } catch {
            throw CloudError.from(error)
        }
    }

    /// Sends with a Bearer; on `401` refreshes once (single-flight) and retries.
    func authorized(_ build: () throws -> URLRequest) async throws -> (Data, HTTPURLResponse) {
        let token = try await tokens.validAccessToken()
        var request = try build()
        try attachBearer(token, to: &request)
        let (data, http) = try await send(request)
        guard http.statusCode == 401 else {
            return (data, http)
        }
        let refreshed = try await tokens.refreshedSession(rejectedAccessToken: token)
        var retry = try build()
        try attachBearer(refreshed.accessToken, to: &retry)
        let (retryData, retryHTTP) = try await send(retry)
        if retryHTTP.statusCode == 401 {
            await tokens.invalidate()
            throw CloudError.notAuthenticated
        }
        return (retryData, retryHTTP)
    }

    func performUpload(
        _ request: URLRequest,
        fromFile bodyURL: URL,
        progress: (@Sendable (UploadProgress) -> Void)?
    ) async throws -> (Data, HTTPURLResponse) {
        let handle = UploadTaskHandle()
        let delegate = UploadProgressDelegate(handler: progress)
        let session = self.session
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<(Data, HTTPURLResponse), any Error>) in
                let task = session.uploadTask(with: request, fromFile: bodyURL) { data, response, error in
                    if let error {
                        continuation.resume(throwing: CloudError.from(error))
                        return
                    }
                    guard let http = response as? HTTPURLResponse else {
                        continuation.resume(throwing: CloudError.invalidResponse)
                        return
                    }
                    continuation.resume(returning: (data ?? Data(), http))
                }
                task.delegate = delegate
                if handle.attach(task) {
                    task.resume()
                } else {
                    task.cancel()
                }
            }
        } onCancel: {
            handle.cancel()
        }
    }

    func establish(_ response: AuthTokenResponse) async throws -> AuthSession {
        let session = AuthSession(response: response)
        do {
            try await tokens.install(session)
        } catch {
            throw CloudError.from(error)
        }
        return session
    }

    static func validate(_ data: Data, _ http: HTTPURLResponse) throws {
        switch http.statusCode {
        case 200..<300:
            return
        case 401:
            throw CloudError.unauthorized
        case 402:
            throw CloudError.paymentRequired(PaymentRequiredPayload.decode(from: data))
        default:
            throw CloudError.http(status: http.statusCode, message: ServerErrorBody.message(from: data))
        }
    }

    static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try WireCoding.makeDecoder().decode(type, from: data)
        } catch {
            throw CloudError.decoding(String(describing: error))
        }
    }

    /// Runs blocking file I/O off the Swift cooperative pool.
    static func runBlocking<T: Sendable>(_ work: @escaping @Sendable () throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                continuation.resume(with: Result { try work() })
            }
        }
    }

    /// `POST /api/v1/auth/token/refresh` — deliberately outside `authorized`
    /// so a refresh can never recurse into another refresh.
    static func performRefresh(
        refreshToken: String,
        environment: CloudEnvironment,
        session: URLSession
    ) async throws -> AuthTokenResponse {
        var request = URLRequest(url: environment.endpoint(CloudEndpoint.tokenRefresh))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try WireCoding.makeEncoder().encode(RefreshTokenRequest(refreshToken: refreshToken))
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw CloudError.from(error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw CloudError.invalidResponse
        }
        try validate(data, http)
        return try decode(AuthTokenResponse.self, from: data)
    }
}
