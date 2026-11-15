import Foundation
import Observation

/// Every side-effecting service the app uses. `live()` wires the real
/// implementations; the integrator's tests inject fakes per protocol.
public struct AppDependencies: Sendable {
    public var configurationStore: AppConfigurationStore
    public var sessionStore: AuthSessionStore
    public var queueStore: UploadQueueStore
    public var capture: any ScreenCaptureServiceProtocol
    public var permissions: any PermissionPreflightProtocol
    public var mediaInspector: any MediaInspectorProtocol
    public var storeKit: any StoreKitClientProtocol
    public var urlSession: URLSession

    public init(
        configurationStore: AppConfigurationStore,
        sessionStore: AuthSessionStore,
        queueStore: UploadQueueStore,
        capture: any ScreenCaptureServiceProtocol,
        permissions: any PermissionPreflightProtocol,
        mediaInspector: any MediaInspectorProtocol,
        storeKit: any StoreKitClientProtocol,
        urlSession: URLSession
    ) {
        self.configurationStore = configurationStore
        self.sessionStore = sessionStore
        self.queueStore = queueStore
        self.capture = capture
        self.permissions = permissions
        self.mediaInspector = mediaInspector
        self.storeKit = storeKit
        self.urlSession = urlSession
    }

    public static func live() -> AppDependencies {
        AppDependencies(
            configurationStore: AppConfigurationStore(),
            sessionStore: AuthSessionStore(),
            queueStore: UploadQueueStore(),
            capture: ScreenCaptureEngine(),
            permissions: PermissionPreflight(),
            mediaInspector: VideoFileValidator(),
            storeKit: StoreKit2Client(),
            urlSession: ScreenscribeCloudClient.makeDefaultSession()
        )
    }
}

/// Modal surfaces owned by the app model.
public enum AppSheet: String, Identifiable, Sendable, Equatable {
    case signIn
    case paywall

    public var id: String { rawValue }
}

/// Root state: configuration, the Band 2 client, the signed-in profile and
/// the four feature view models. Lives for the whole process.
@MainActor
@Observable
public final class AppModel {
    public let dependencies: AppDependencies
    public let storeKit: StoreKitManager
    public let auth: AuthViewModel
    public let paywall: PaywallViewModel
    public let recorder: RecorderViewModel
    public let workspace: WorkspaceViewModel

    /// Changes go through `updateConfiguration(_:)` (persisted) or, for the
    /// Cloud URL, `applyCloudURL(_:)` (which also re-scopes the session).
    public private(set) var configuration: AppConfiguration
    public private(set) var client: ScreenscribeCloudClient
    public private(set) var profile: UserProfile?
    public private(set) var isSignedIn = false
    public private(set) var hasStarted = false
    public var presentedSheet: AppSheet?
    /// One-line banner for non-modal problems (offline, sync pending).
    public var bannerMessage: String?

    public init(dependencies: AppDependencies = .live()) {
        self.dependencies = dependencies
        let configuration = dependencies.configurationStore.load()
        self.configuration = configuration
        self.client = ScreenscribeCloudClient(
            environment: CloudEnvironment.resolved(from: configuration.cloudBaseURL),
            session: dependencies.urlSession,
            sessionStore: dependencies.sessionStore
        )
        self.storeKit = StoreKitManager(client: dependencies.storeKit)
        self.auth = AuthViewModel()
        self.paywall = PaywallViewModel()
        self.recorder = RecorderViewModel(capture: dependencies.capture, permissions: dependencies.permissions)
        self.workspace = WorkspaceViewModel(queueStore: dependencies.queueStore, mediaInspector: dependencies.mediaInspector)
        auth.attach(to: self)
        paywall.attach(to: self)
        recorder.attach(to: self)
        workspace.attach(to: self)
    }

    public var environment: CloudEnvironment {
        client.environment
    }

    public var subscription: SubscriptionSnapshot {
        profile?.subscription ?? .inactive
    }

    /// Edits and persists recording/review defaults. The Cloud URL is
    /// ignored here on purpose — use `applyCloudURL(_:)`.
    public func updateConfiguration(_ mutate: (inout AppConfiguration) -> Void) {
        var updated = configuration
        mutate(&updated)
        updated.cloudBaseURL = configuration.cloudBaseURL
        updated.countdownSeconds = min(10, max(0, updated.countdownSeconds))
        updated.framesPerSecond = min(60, max(1, updated.framesPerSecond))
        guard updated != configuration else { return }
        configuration = updated
        dependencies.configurationStore.save(updated)
    }

    /// Restores every default except the Cloud URL.
    public func resetConfiguration() {
        var defaults = AppConfiguration.standard
        defaults.cloudBaseURL = configuration.cloudBaseURL
        configuration = defaults
        dependencies.configurationStore.save(defaults)
    }

    // MARK: Launch

    /// Restores the session, starts the StoreKit listener and resumes the
    /// upload queue. Safe to call more than once.
    public func start() async {
        guard !hasStarted else { return }
        hasStarted = true

        await storeKit.startTransactionListener { [weak self] profile in
            await self?.profileUpdated(profile)
        }
        await workspace.start()
        recorder.start()

        if await client.isSignedIn() {
            isSignedIn = true
            await installSyncer()
            await refreshProfile()
            await workspace.didSignIn()
        }
        await paywall.loadProducts()
    }

    // MARK: Session

    /// Called by `AuthViewModel` once tokens are in the Keychain.
    func didSignIn(_ session: AuthSession) async {
        isSignedIn = true
        presentedSheet = nil
        bannerMessage = nil
        await installSyncer()
        await refreshProfile()
        if let profile = await storeKit.retryPendingSync() {
            profileUpdated(profile)
        }
        await workspace.didSignIn()
        if paywall.resumeAfterSignIn {
            paywall.resumeAfterSignIn = false
            if !subscription.canCreateJobs {
                presentedSheet = .paywall
            }
        }
    }

    /// Local sign-out always succeeds; the server call is best effort.
    public func signOut() async {
        await client.logout()
        await clearSession()
    }

    /// A refresh failed terminally (revoked or expired refresh token).
    func handleSessionExpired() async {
        guard isSignedIn else { return }
        await clearSession()
        auth.notice = L10n.string("signin.expired")
        presentedSheet = .signIn
    }

    private func clearSession() async {
        isSignedIn = false
        profile = nil
        await storeKit.updateSyncer(nil)
        await workspace.didSignOut()
        auth.reset()
    }

    private func installSyncer() async {
        let client = client
        await storeKit.updateSyncer { transaction in
            try await client.syncAppStoreTransaction(transaction)
        }
    }

    public func refreshProfile() async {
        guard isSignedIn else { return }
        do {
            let fetched = try await client.fetchProfile()
            profileUpdated(fetched)
        } catch {
            let cloudError = CloudError.from(error)
            if cloudError.isTerminalAuthFailure {
                await handleSessionExpired()
            } else {
                bannerMessage = cloudError.localizedMessage
            }
        }
    }

    /// Any fresh profile (from `/auth/me`, an App Store sync, a renewal).
    func profileUpdated(_ updated: UserProfile) {
        profile = updated
        if updated.subscription.canCreateJobs {
            if presentedSheet == .paywall {
                presentedSheet = nil
            }
            paywall.reason = nil
            Task { await workspace.releaseBillingBlocked() }
        }
    }

    // MARK: Sheets

    public func presentSignIn() {
        auth.notice = nil
        presentedSheet = .signIn
    }

    /// `402`: never retried automatically; the paywall explains why.
    public func presentPaywall(reason: PaymentRequiredPayload?) {
        paywall.reason = reason
        presentedSheet = .paywall
    }

    public func dismissSheet() {
        if presentedSheet == .signIn {
            auth.cancelDeviceFlow()
        }
        presentedSheet = nil
    }

    // MARK: Cloud URL

    /// Validates and applies a new Band 2 origin. Tokens are origin-bound,
    /// so switching signs out first — a token is never sent to another host.
    @discardableResult
    public func applyCloudURL(_ string: String) async throws -> CloudEnvironment {
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        let candidate = try CloudEnvironment(string: trimmed.isEmpty ? AppConfiguration.defaultCloudBaseURL : trimmed)
        guard candidate != client.environment else { return candidate }
        if isSignedIn {
            await signOut()
        } else {
            await client.logout()
        }
        client = ScreenscribeCloudClient(
            environment: candidate,
            session: dependencies.urlSession,
            sessionStore: dependencies.sessionStore
        )
        configuration.cloudBaseURL = candidate.displayString
        dependencies.configurationStore.save(configuration)
        bannerMessage = nil
        return candidate
    }

    // MARK: Links

    /// Hosted review page for a share id (server `share_url` preferred).
    public func reviewURL(shareID: String, shareURL: String? = nil) -> URL {
        client.shareURL(shareID: shareID, shareURL: shareURL)
    }

    /// Band 2 account page: profile, data export and account deletion.
    public var accountManagementURL: URL {
        client.accountManagementURL
    }
}
