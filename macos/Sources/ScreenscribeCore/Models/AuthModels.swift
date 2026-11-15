import Foundation

// MARK: - Subscription (`GET /api/v1/auth/me` → `subscription`)

/// Where the active entitlement was purchased.
public enum SubscriptionSource: String, Codable, Sendable, Equatable {
    case stripe
    case appStore = "app_store"
    case other

    public init(wireValue: String) {
        switch wireValue.trimmingCharacters(in: .whitespaces).lowercased() {
        case "stripe", "web": self = .stripe
        case "app_store", "appstore", "apple", "storekit": self = .appStore
        default: self = .other
        }
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(wireValue: (try? container.decode(String.self)) ?? "")
    }

    public var localizedTitle: String {
        switch self {
        case .stripe: return L10n.string("account.source.stripe")
        case .appStore: return L10n.string("account.source.appstore")
        case .other: return L10n.string("account.source.other")
        }
    }
}

/// Minute allowance and entitlement state from the single Band 2 ledger.
public struct SubscriptionSnapshot: Codable, Sendable, Equatable {
    public var active: Bool
    public var plan: String?
    public var source: SubscriptionSource?
    public var periodEnd: Date?
    public var minutesLimit: Double
    public var minutesUsed: Double
    public var minutesRemaining: Double

    public init(
        active: Bool,
        plan: String? = nil,
        source: SubscriptionSource? = nil,
        periodEnd: Date? = nil,
        minutesLimit: Double = 0,
        minutesUsed: Double = 0,
        minutesRemaining: Double? = nil
    ) {
        self.active = active
        self.plan = plan
        self.source = source
        self.periodEnd = periodEnd
        self.minutesLimit = minutesLimit
        self.minutesUsed = minutesUsed
        self.minutesRemaining = minutesRemaining ?? max(0, minutesLimit - minutesUsed)
    }

    public static let inactive = SubscriptionSnapshot(active: false)

    /// 0…1 share of the allowance already consumed.
    public var usageFraction: Double {
        guard minutesLimit > 0 else { return active ? 0 : 1 }
        return min(1, max(0, minutesUsed / minutesLimit))
    }

    /// `true` when a new job can be created without hitting `402`.
    public var canCreateJobs: Bool {
        active && minutesRemaining > 0
    }

    enum CodingKeys: String, CodingKey {
        case active
        case plan
        case source
        case periodEnd = "period_end"
        case minutesLimit = "minutes_limit"
        case minutesUsed = "minutes_used"
        case minutesRemaining = "minutes_remaining"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let limit = container.lossyDouble(.minutesLimit) ?? 0
        let used = container.lossyDouble(.minutesUsed) ?? 0
        self.init(
            active: container.lossyBool(.active) ?? false,
            plan: container.lossyNonEmptyString(.plan),
            source: container.lossyNonEmptyString(.source).map(SubscriptionSource.init(wireValue:)),
            periodEnd: container.lossyDate(.periodEnd),
            minutesLimit: limit,
            minutesUsed: used,
            minutesRemaining: container.lossyDouble(.minutesRemaining)
        )
    }
}

// MARK: - Profile

/// `GET /api/v1/auth/me` body (also returned by App Store sync).
public struct UserProfile: Codable, Sendable, Equatable {
    public var userID: String
    public var email: String?
    public var subscription: SubscriptionSnapshot

    public init(userID: String, email: String? = nil, subscription: SubscriptionSnapshot = .inactive) {
        self.userID = userID
        self.email = email
        self.subscription = subscription
    }

    enum CodingKeys: String, CodingKey {
        case userID = "user_id"
        case email
        case subscription
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        userID = container.lossyNonEmptyString(AnyCodingKey("user_id"))
            ?? container.lossyNonEmptyString(AnyCodingKey("id"))
            ?? ""
        email = container.lossyNonEmptyString(AnyCodingKey("email"))
        subscription = (try? container.decodeIfPresent(SubscriptionSnapshot.self, forKey: AnyCodingKey("subscription")))
            ?? .inactive
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(userID, forKey: .userID)
        try container.encodeIfPresent(email, forKey: .email)
        try container.encode(subscription, forKey: .subscription)
    }
}

// MARK: - Tokens

/// Token pair returned by Apple sign-in, login, register, device token and refresh.
public struct AuthTokenResponse: Decodable, Sendable, Equatable {
    public var accessToken: String
    public var refreshToken: String?
    public var tokenType: String
    public var expiresIn: Int
    public var user: UserProfile?

    public init(
        accessToken: String,
        refreshToken: String? = nil,
        tokenType: String = "Bearer",
        expiresIn: Int = 3600,
        user: UserProfile? = nil
    ) {
        self.accessToken = accessToken
        self.refreshToken = refreshToken
        self.tokenType = tokenType
        self.expiresIn = expiresIn
        self.user = user
    }

    enum CodingKeys: String, CodingKey {
        case accessToken = "access_token"
        case refreshToken = "refresh_token"
        case tokenType = "token_type"
        case expiresIn = "expires_in"
        case user
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard let access = container.lossyNonEmptyString(.accessToken) else {
            throw DecodingError.keyNotFound(
                CodingKeys.accessToken,
                DecodingError.Context(codingPath: decoder.codingPath, debugDescription: "access_token missing")
            )
        }
        accessToken = access
        refreshToken = container.lossyNonEmptyString(.refreshToken)
        tokenType = container.lossyNonEmptyString(.tokenType) ?? "Bearer"
        expiresIn = max(0, container.lossyInt(.expiresIn) ?? 3600)
        user = try? container.decodeIfPresent(UserProfile.self, forKey: .user)
    }
}

/// The persisted session (Keychain only — never `UserDefaults`).
public struct AuthSession: Codable, Sendable, Equatable {
    public var accessToken: String
    public var refreshToken: String?
    public var tokenType: String
    public var expiresAt: Date
    public var userID: String?
    public var email: String?

    public init(
        accessToken: String,
        refreshToken: String?,
        tokenType: String = "Bearer",
        expiresAt: Date,
        userID: String? = nil,
        email: String? = nil
    ) {
        self.accessToken = accessToken
        self.refreshToken = refreshToken
        self.tokenType = tokenType
        self.expiresAt = expiresAt
        self.userID = userID
        self.email = email
    }

    /// Builds a session from a token response. A refresh response without a
    /// new `refresh_token` keeps the previous one (`previous`).
    public init(response: AuthTokenResponse, issuedAt: Date = Date(), previous: AuthSession? = nil) {
        self.accessToken = response.accessToken
        self.refreshToken = response.refreshToken ?? previous?.refreshToken
        self.tokenType = response.tokenType.isEmpty ? "Bearer" : response.tokenType
        self.expiresAt = issuedAt.addingTimeInterval(TimeInterval(response.expiresIn))
        self.userID = response.user?.userID ?? previous?.userID
        self.email = response.user?.email ?? previous?.email
    }

    /// `true` when the access token expires within `leeway` seconds of `date`.
    public func isExpired(at date: Date = Date(), leeway: TimeInterval = 30) -> Bool {
        expiresAt.timeIntervalSince(date) <= leeway
    }

    public var authorizationHeaderValue: String {
        "Bearer \(accessToken)"
    }
}

// MARK: - Request bodies

/// `POST /api/v1/auth/apple`.
public struct AppleSignInRequest: Encodable, Sendable, Equatable {
    public var identityToken: String
    public var email: String?
    public var fullName: String?

    public init(identityToken: String, email: String? = nil, fullName: String? = nil) {
        self.identityToken = identityToken
        self.email = email
        self.fullName = fullName
    }

    enum CodingKeys: String, CodingKey {
        case identityToken = "identity_token"
        case email
        case fullName = "full_name"
    }
}

/// `POST /api/v1/auth/login` and `POST /api/v1/auth/register`.
public struct EmailCredentialsRequest: Encodable, Sendable, Equatable {
    public var email: String
    public var password: String

    public init(email: String, password: String) {
        self.email = email
        self.password = password
    }
}

/// `POST /api/v1/auth/token/refresh`.
public struct RefreshTokenRequest: Encodable, Sendable, Equatable {
    public var grantType: String
    public var refreshToken: String

    public init(refreshToken: String) {
        self.grantType = "refresh_token"
        self.refreshToken = refreshToken
    }

    enum CodingKeys: String, CodingKey {
        case grantType = "grant_type"
        case refreshToken = "refresh_token"
    }
}

/// `POST /api/v1/auth/logout` — lets the server revoke the refresh token.
public struct LogoutRequest: Encodable, Sendable, Equatable {
    public var refreshToken: String?

    public init(refreshToken: String?) {
        self.refreshToken = refreshToken
    }

    enum CodingKeys: String, CodingKey {
        case refreshToken = "refresh_token"
    }
}

// MARK: - RFC 8628 device authorization

/// `POST /api/v1/auth/device/code`.
public struct DeviceCodeRequest: Encodable, Sendable, Equatable {
    public var clientID: String

    public init(clientID: String) {
        self.clientID = clientID
    }

    enum CodingKeys: String, CodingKey {
        case clientID = "client_id"
    }
}

/// Device code grant handed to the user (`user_code` + verification page).
public struct DeviceCodeResponse: Decodable, Sendable, Equatable {
    public var deviceCode: String
    public var userCode: String
    public var verificationURI: String
    public var verificationURIComplete: String?
    public var expiresIn: Int
    public var interval: Int

    /// RFC 8628 §3.5: never poll faster than 5 seconds.
    public static let minimumInterval = 5

    public init(
        deviceCode: String,
        userCode: String,
        verificationURI: String,
        verificationURIComplete: String? = nil,
        expiresIn: Int = 600,
        interval: Int = 5
    ) {
        self.deviceCode = deviceCode
        self.userCode = userCode
        self.verificationURI = verificationURI
        self.verificationURIComplete = verificationURIComplete
        self.expiresIn = expiresIn
        self.interval = max(DeviceCodeResponse.minimumInterval, interval)
    }

    enum CodingKeys: String, CodingKey {
        case deviceCode = "device_code"
        case userCode = "user_code"
        case verificationURI = "verification_uri"
        case verificationURL = "verification_url"
        case verificationURIComplete = "verification_uri_complete"
        case expiresIn = "expires_in"
        case interval
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard let deviceCode = container.lossyNonEmptyString(.deviceCode),
              let userCode = container.lossyNonEmptyString(.userCode),
              let verification = container.lossyNonEmptyString(.verificationURI)
                ?? container.lossyNonEmptyString(.verificationURL)
        else {
            throw DecodingError.dataCorrupted(
                DecodingError.Context(codingPath: decoder.codingPath, debugDescription: "Incomplete device code response")
            )
        }
        self.init(
            deviceCode: deviceCode,
            userCode: userCode,
            verificationURI: verification,
            verificationURIComplete: container.lossyNonEmptyString(.verificationURIComplete),
            expiresIn: max(1, container.lossyInt(.expiresIn) ?? 600),
            interval: container.lossyInt(.interval) ?? DeviceCodeResponse.minimumInterval
        )
    }

    /// The page to open: the complete URI (code pre-filled) when provided.
    public var verificationURL: URL? {
        if let complete = verificationURIComplete, let url = URL(string: complete) {
            return url
        }
        return URL(string: verificationURI)
    }
}

/// `POST /api/v1/auth/device/token`.
public struct DeviceTokenRequest: Encodable, Sendable, Equatable {
    public static let grantType = "urn:ietf:params:oauth:grant-type:device_code"

    public var grantType: String
    public var deviceCode: String
    public var clientID: String

    public init(deviceCode: String, clientID: String) {
        self.grantType = DeviceTokenRequest.grantType
        self.deviceCode = deviceCode
        self.clientID = clientID
    }

    enum CodingKeys: String, CodingKey {
        case grantType = "grant_type"
        case deviceCode = "device_code"
        case clientID = "client_id"
    }
}

/// One poll of the device token endpoint.
public enum DeviceTokenPollResult: Sendable, Equatable {
    case authorized(AuthTokenResponse)
    case pending
    case slowDown
    case expired
    case denied
}

/// RFC 6749/8628 error body. Tolerates FastAPI's `{"detail": …}` wrapper.
public struct OAuthErrorBody: Decodable, Sendable, Equatable {
    public var error: String
    public var errorDescription: String?

    public init(error: String, errorDescription: String? = nil) {
        self.error = error
        self.errorDescription = errorDescription
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        if let code = container.lossyNonEmptyString(AnyCodingKey("error")) {
            error = code
            errorDescription = container.lossyNonEmptyString(AnyCodingKey("error_description"))
            return
        }
        if let nested = try? container.decodeIfPresent(OAuthErrorBody.self, forKey: AnyCodingKey("detail")) {
            self = nested
            return
        }
        if let detail = container.lossyNonEmptyString(AnyCodingKey("detail")) {
            error = detail
            errorDescription = nil
            return
        }
        throw DecodingError.dataCorrupted(
            DecodingError.Context(codingPath: decoder.codingPath, debugDescription: "No OAuth error code")
        )
    }

    public var pollResult: DeviceTokenPollResult? {
        switch error {
        case "authorization_pending": return .pending
        case "slow_down": return .slowDown
        case "expired_token": return .expired
        case "access_denied": return .denied
        default: return nil
        }
    }
}
