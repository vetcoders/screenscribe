import Foundation
import ScreenscribeCore
import Testing

@Suite("Auth, Keychain, and Origin Guards")
struct AuthAndKeychainTests {
    @Test("CloudEnvironment enforces HTTPS for remote origins and allows HTTP only on loopback")
    func cloudEnvironmentValidation() throws {
        let prod = try CloudEnvironment(string: "https://screenscribe.vetcoders.io/")
        #expect(prod.baseURL.absoluteString == "https://screenscribe.vetcoders.io")
        #expect(!prod.isLoopback)

        let loopback = try CloudEnvironment(string: "http://127.0.0.1:8080/")
        #expect(loopback.isLoopback)
        #expect(loopback.endpoint(CloudEndpoint.authMe).absoluteString == "http://127.0.0.1:8080/api/v1/auth/me")

        #expect(throws: CloudEnvironmentError.insecureScheme) {
            _ = try CloudEnvironment(string: "http://example.com")
        }
        #expect(throws: CloudEnvironmentError.embeddedCredentials) {
            _ = try CloudEnvironment(string: "https://alice:secret@example.com")
        }

        let crossOrigin = URL(string: "https://attacker.example/api/v1/auth/me")!
        #expect(!prod.isSameOrigin(crossOrigin))
        #expect(throws: CloudError.self) {
            _ = try prod.requireSameOrigin("https://attacker.example/api/v1/jobs/job_1")
        }
    }

    @Test("AuthSessionStore persists to Keychain and purges corrupted bytes")
    func authSessionStoreRoundTripAndCorruptionPurge() throws {
        let keychain = InMemoryKeychainStore()
        let store = AuthSessionStore(keychain: keychain)
        let expiry = Date(timeIntervalSince1970: 1_900_000_000)
        let session = AuthSession(
            accessToken: "acc_1",
            refreshToken: "ref_1",
            expiresAt: expiry,
            userID: "usr_1",
            email: "dev@example.com"
        )
        try store.save(session)
        let loaded = try #require(store.load())
        #expect(loaded.accessToken == "acc_1")
        #expect(loaded.refreshToken == "ref_1")
        #expect(loaded.userID == "usr_1")

        try keychain.set(Data("not-json".utf8), for: AuthSessionStore.account)
        #expect(store.load() == nil)
        #expect(try keychain.data(for: AuthSessionStore.account) == nil)
    }

    @Test("TokenRefreshCoordinator coalesces concurrent 401 refreshes into one network call")
    func singleFlightRefreshCoalescing() async throws {
        let keychain = InMemoryKeychainStore()
        let store = AuthSessionStore(keychain: keychain)
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let initial = AuthSession(
            accessToken: "acc_old",
            refreshToken: "ref_initial",
            expiresAt: now.addingTimeInterval(3600),
            userID: "usr_42",
            email: "founder@example.com"
        )
        try store.save(initial)

        actor CallCounter {
            var count = 0
            func increment() -> Int {
                count += 1
                return count
            }
        }
        let counter = CallCounter()
        let coordinator = TokenRefreshCoordinator(
            store: store,
            now: { now },
            refresher: { refresh in
                #expect(refresh == "ref_initial")
                _ = await counter.increment()
                try await Task.sleep(for: .milliseconds(40))
                return AuthTokenResponse(
                    accessToken: "acc_new",
                    refreshToken: "ref_rotated",
                    expiresIn: 3600
                )
            }
        )

        async let first = coordinator.refreshedSession(rejectedAccessToken: "acc_old")
        async let second = coordinator.refreshedSession(rejectedAccessToken: "acc_old")
        async let third = coordinator.refreshedSession(rejectedAccessToken: "acc_old")
        let results = try await [first, second, third]
        for session in results {
            #expect(session.accessToken == "acc_new")
            #expect(session.refreshToken == "ref_rotated")
            #expect(session.userID == "usr_42")
        }
        #expect(await counter.count == 1)
        #expect(await coordinator.refreshCount == 1)
    }

    @Test("ScreenscribeCloudClient login, 401 refresh retry, RFC 8628 device poll, and cross-origin Bearer stripping")
    func cloudClientAuthAndDeviceFlow() async throws {
        let keychain = InMemoryKeychainStore()
        let sessionStore = AuthSessionStore(keychain: keychain)
        let env = try CloudEnvironment(string: "https://cloud.example.test")

        let lock = NSLock()
        nonisolated(unsafe) var pollAttempt = 0
        nonisolated(unsafe) var meAttempt = 0

        let (urlSession, mockID) = MockURLProtocol.makeSession { req in
            switch (req.method, req.url.path) {
            case ("POST", "/api/v1/auth/device/code"):
                return .json([
                    "device_code": "dev_code_1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://cloud.example.test/activate",
                    "verification_uri_complete": "https://cloud.example.test/activate?user_code=ABCD-EFGH",
                    "expires_in": 300,
                    "interval": 5,
                ])
            case ("POST", "/api/v1/auth/device/token"):
                let attempt = lock.withLock { () -> Int in
                    pollAttempt += 1
                    return pollAttempt
                }
                if attempt == 1 {
                    return .json(["error": "authorization_pending"], statusCode: 400)
                }
                if attempt == 2 {
                    return .json(["error": "slow_down"], statusCode: 400)
                }
                return .json([
                    "access_token": "acc_stale",
                    "refresh_token": "ref_live",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "user": ["user_id": "usr_dev", "email": "mac@example.test"],
                ])
            case ("GET", "/api/v1/auth/me"):
                let call = lock.withLock { () -> Int in
                    meAttempt += 1
                    return meAttempt
                }
                if call == 1 {
                    #expect(req.header("Authorization") == "Bearer acc_stale")
                    return .json(["detail": "Expired"], statusCode: 401)
                }
                #expect(req.header("Authorization") == "Bearer acc_fresh")
                return .json([
                    "user_id": "usr_dev",
                    "email": "mac@example.test",
                    "subscription": [
                        "active": true,
                        "plan": "pro",
                        "source": "app_store",
                        "minutes_limit": 300,
                        "minutes_used": 12.5,
                        "minutes_remaining": 287.5,
                    ],
                ])
            case ("POST", "/api/v1/auth/token/refresh"):
                return .json([
                    "access_token": "acc_fresh",
                    "refresh_token": "ref_live_2",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                ])
            case ("POST", "/api/v1/auth/logout"):
                return .json(["ok": true])
            default:
                return .json(["detail": "Not found"], statusCode: 404)
            }
        }
        defer { MockURLProtocol.unregister(mockID) }

        let client = ScreenscribeCloudClient(environment: env, session: urlSession, sessionStore: sessionStore)
        let code = try await client.requestDeviceCode()
        #expect(code.userCode == "ABCD-EFGH")

        let session = try await client.completeDeviceAuthorization(code) { _ in }
        #expect(session.accessToken == "acc_stale")

        let profile = try await client.fetchProfile()
        #expect(profile.userID == "usr_dev")
        #expect(profile.subscription.active)
        #expect(profile.subscription.source == .appStore)
        #expect(profile.subscription.minutesRemaining == 287.5)

        let sameOriginReq = await client.shareRequest(for: URL(string: "https://cloud.example.test/r/shr_1")!)
        #expect(sameOriginReq.value(forHTTPHeaderField: "Authorization") == "Bearer acc_fresh")

        let thirdPartyReq = await client.shareRequest(for: URL(string: "https://other.example.test/r/shr_1")!)
        #expect(thirdPartyReq.value(forHTTPHeaderField: "Authorization") == nil)

        await client.logout()
        #expect(!(await client.isSignedIn()))
    }
}
