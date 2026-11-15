import Foundation

/// Keychain persistence of the signed-in `AuthSession`.
public struct AuthSessionStore: Sendable {
    public static let account = "auth.session"

    public let keychain: any KeychainStoreProtocol

    public init(keychain: any KeychainStoreProtocol = SecurityKeychainStore()) {
        self.keychain = keychain
    }

    /// The stored session, or `nil`. A corrupt item is purged.
    public func load() -> AuthSession? {
        guard let data = try? keychain.data(for: AuthSessionStore.account) else { return nil }
        do {
            return try WireCoding.makeDecoder().decode(AuthSession.self, from: data)
        } catch {
            try? keychain.remove(AuthSessionStore.account)
            return nil
        }
    }

    public func save(_ session: AuthSession) throws {
        let data = try WireCoding.makeEncoder().encode(session)
        try keychain.set(data, for: AuthSessionStore.account)
    }

    /// Removes every credential this app stored (sign-out purge).
    public func clear() throws {
        try keychain.removeAll()
    }
}

/// Owns the access/refresh token pair and serializes refreshes.
///
/// Many concurrent requests can hit `401` at once (queue drain + SSE + profile).
/// Only the first one performs `POST /api/v1/auth/token/refresh`; the rest
/// await the same in-flight task, so a rotating refresh token is never spent
/// twice.
public actor TokenRefreshCoordinator {
    public typealias Refresher = @Sendable (_ refreshToken: String) async throws -> AuthTokenResponse

    private let store: AuthSessionStore
    private let now: @Sendable () -> Date
    private var refresher: Refresher?
    private var cached: AuthSession?
    private var loaded = false
    private var inFlight: Task<AuthSession, any Error>?

    /// Number of refreshes that completed successfully (diagnostics / tests).
    public private(set) var refreshCount = 0

    public init(
        store: AuthSessionStore,
        now: @escaping @Sendable () -> Date = { Date() },
        refresher: Refresher? = nil
    ) {
        self.store = store
        self.now = now
        self.refresher = refresher
    }

    public func setRefresher(_ refresher: @escaping Refresher) {
        self.refresher = refresher
    }

    public func currentSession() -> AuthSession? {
        if !loaded {
            cached = store.load()
            loaded = true
        }
        return cached
    }

    public var isSignedIn: Bool {
        currentSession() != nil
    }

    public func install(_ session: AuthSession) throws {
        inFlight = nil
        try store.save(session)
        cached = session
        loaded = true
    }

    /// Drops the session from memory and the Keychain.
    public func invalidate() {
        inFlight = nil
        cached = nil
        loaded = true
        try? store.clear()
    }

    /// A non-expired access token, refreshing proactively when it is about to
    /// expire. Throws `CloudError.notAuthenticated` when signed out.
    public func validAccessToken() async throws -> String {
        guard let session = currentSession() else {
            throw CloudError.notAuthenticated
        }
        if session.isExpired(at: now()), session.refreshToken != nil {
            return try await refreshedSession(rejectedAccessToken: session.accessToken).accessToken
        }
        return session.accessToken
    }

    /// Single-flight refresh after the server rejected `rejectedAccessToken`.
    /// When another caller already rotated the token, the current session is
    /// returned without a network call.
    public func refreshedSession(rejectedAccessToken: String?) async throws -> AuthSession {
        if let inFlight {
            return try await inFlight.value
        }
        guard let session = currentSession() else {
            throw CloudError.notAuthenticated
        }
        if let rejectedAccessToken, rejectedAccessToken != session.accessToken, !session.isExpired(at: now()) {
            return session
        }
        guard let refreshToken = session.refreshToken else {
            invalidate()
            throw CloudError.notAuthenticated
        }
        let task = Task { try await self.runRefresh(refreshToken: refreshToken, previous: session) }
        inFlight = task
        defer {
            if inFlight == task {
                inFlight = nil
            }
        }
        return try await task.value
    }

    private func runRefresh(refreshToken: String, previous: AuthSession) async throws -> AuthSession {
        guard let refresher else {
            throw CloudError.notAuthenticated
        }
        let response: AuthTokenResponse
        do {
            response = try await refresher(refreshToken)
        } catch {
            let cloudError = CloudError.from(error)
            if cloudError.isTerminalAuthFailure {
                if cached?.accessToken == previous.accessToken {
                    invalidate()
                }
                throw CloudError.notAuthenticated
            }
            throw cloudError
        }
        // Signed out (or signed into another account) while refreshing:
        // never resurrect the old session.
        guard let current = cached, current.accessToken == previous.accessToken else {
            throw CloudError.notAuthenticated
        }
        let refreshed = AuthSession(response: response, issuedAt: now(), previous: previous)
        do {
            try store.save(refreshed)
        } catch {
            // Keep the in-memory session usable; the next launch re-authenticates.
        }
        cached = refreshed
        refreshCount += 1
        return refreshed
    }
}
