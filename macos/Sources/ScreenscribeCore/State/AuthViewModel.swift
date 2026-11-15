import AuthenticationServices
import Foundation
import Observation

/// Sign in with Apple, email + password, and the RFC 8628 device flow
/// (approve in the browser — useful when the Mac has no Apple ID session).
@MainActor
@Observable
public final class AuthViewModel {
    public enum Mode: String, CaseIterable, Identifiable, Sendable {
        case signIn
        case register

        public var id: String { rawValue }

        public var localizedTitle: String {
            switch self {
            case .signIn: return L10n.string("signin.mode.signin")
            case .register: return L10n.string("signin.mode.register")
            }
        }
    }

    public static let minimumCredentialLength = 8

    public var mode: Mode = .signIn
    public var email = ""
    /// The account password, held only while the sheet is open.
    public var credential = ""
    public private(set) var isBusy = false
    public private(set) var errorMessage: String?
    /// Context shown above the form (e.g. "session expired").
    public var notice: String?
    public private(set) var deviceCode: DeviceCodeResponse?
    public private(set) var deviceExpiresAt: Date?

    @ObservationIgnored private weak var app: AppModel?
    @ObservationIgnored private var deviceTask: Task<Void, Never>?
    @ObservationIgnored private var deviceFlowID: UUID?

    public init() {}

    func attach(to app: AppModel) {
        self.app = app
    }

    public var canSubmitEmail: Bool {
        !isBusy && Self.isPlausibleEmail(email) && credential.count >= (mode == .register ? Self.minimumCredentialLength : 1)
    }

    public var isDeviceFlowActive: Bool {
        deviceCode != nil
    }

    // MARK: Sign in with Apple

    /// Scopes requested from `SignInWithAppleButton`.
    public static let appleScopes: [ASAuthorization.Scope] = [.email, .fullName]

    /// Completion of `SignInWithAppleButton`. Cancellation is silent.
    public func handleAppleAuthorization(_ result: Result<ASAuthorization, any Error>) {
        switch result {
        case .failure(let error):
            if let authorizationError = error as? ASAuthorizationError, authorizationError.code == .canceled {
                return
            }
            errorMessage = L10n.format("signin.apple.failed", error.localizedDescription)
        case .success(let authorization):
            guard let appleCredential = authorization.credential as? ASAuthorizationAppleIDCredential,
                  let tokenData = appleCredential.identityToken,
                  let identityToken = String(data: tokenData, encoding: .utf8),
                  !identityToken.isEmpty
            else {
                errorMessage = L10n.string("signin.apple.notoken")
                return
            }
            // Apple shares email and name only on the first authorization.
            let appleEmail = appleCredential.email
            let appleName = appleCredential.fullName.flatMap { components -> String? in
                let formatted = PersonNameComponentsFormatter.localizedString(from: components, style: .default)
                return formatted.isEmpty ? nil : formatted
            }
            run { client in
                try await client.signInWithApple(identityToken: identityToken, email: appleEmail, fullName: appleName)
            }
        }
    }

    // MARK: Email

    public func submitEmail() {
        guard canSubmitEmail else {
            errorMessage = mode == .register
                ? L10n.format("signin.error.credential.short", Self.minimumCredentialLength)
                : L10n.string("signin.error.invalid")
            return
        }
        let accountEmail = self.email.trimmingCharacters(in: .whitespacesAndNewlines)
        let secret = self.credential
        let selectedMode = self.mode
        run { client in
            switch selectedMode {
            case .signIn:
                return try await client.login(email: accountEmail, password: secret)
            case .register:
                return try await client.register(email: accountEmail, password: secret)
            }
        }
    }

    // MARK: Device flow (RFC 8628)

    /// Requests a user code, then polls until the user approves it in the
    /// browser. The view opens `verificationURL` when `deviceCode` appears.
    public func startDeviceFlow() {
        guard let app, deviceTask == nil else { return }
        let client = app.client
        let flowID = UUID()
        deviceFlowID = flowID
        errorMessage = nil
        isBusy = true
        deviceTask = Task { [weak self] in
            do {
                let code = try await client.requestDeviceCode()
                guard let self, self.deviceFlowID == flowID else { return }
                self.deviceCode = code
                self.deviceExpiresAt = Date().addingTimeInterval(TimeInterval(code.expiresIn))
                self.isBusy = false
                let session = try await client.completeDeviceAuthorization(code)
                // Tokens are already in the Keychain: adopt them even if the
                // user cancelled in the last instant.
                if self.deviceFlowID == flowID {
                    self.deviceTask = nil
                    self.deviceFlowID = nil
                }
                await self.finishSignIn(session)
            } catch {
                // A cancelled or superseded flow must not touch the new one.
                guard let self, self.deviceFlowID == flowID else { return }
                let cloudError = CloudError.from(error)
                if cloudError != .cancelled {
                    self.errorMessage = cloudError.localizedMessage
                }
                self.deviceCode = nil
                self.deviceExpiresAt = nil
                self.isBusy = false
                self.deviceTask = nil
                self.deviceFlowID = nil
            }
        }
    }

    public func cancelDeviceFlow() {
        deviceTask?.cancel()
        deviceTask = nil
        deviceFlowID = nil
        deviceCode = nil
        deviceExpiresAt = nil
        isBusy = false
    }

    // MARK: Lifecycle

    /// Clears the form (sign-out, sheet dismissal). Keeps `notice`.
    public func reset() {
        cancelDeviceFlow()
        email = ""
        credential = ""
        errorMessage = nil
        mode = .signIn
    }

    public func clearError() {
        errorMessage = nil
    }

    private func run(_ operation: @escaping @Sendable (ScreenscribeCloudClient) async throws -> AuthSession) {
        guard let app, !isBusy else { return }
        let client = app.client
        errorMessage = nil
        isBusy = true
        Task { [weak self] in
            do {
                let session = try await operation(client)
                await self?.finishSignIn(session)
            } catch {
                self?.errorMessage = Self.message(for: error)
            }
            self?.isBusy = false
        }
    }

    private func finishSignIn(_ session: AuthSession) async {
        credential = ""
        notice = nil
        errorMessage = nil
        deviceCode = nil
        deviceExpiresAt = nil
        isBusy = false
        await app?.didSignIn(session)
    }

    static func message(for error: any Error) -> String {
        let cloudError = CloudError.from(error)
        switch cloudError {
        case .unauthorized, .notAuthenticated:
            return L10n.string("signin.error.rejected")
        case .http(let status, let message) where status == 409:
            return message ?? L10n.string("signin.error.exists")
        default:
            return cloudError.localizedMessage
        }
    }

    static func isPlausibleEmail(_ value: String) -> Bool {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let at = trimmed.firstIndex(of: "@"), at != trimmed.startIndex else { return false }
        let domain = trimmed[trimmed.index(after: at)...]
        return domain.contains(".") && !domain.hasPrefix(".") && !domain.hasSuffix(".") && !trimmed.contains(" ")
    }
}
