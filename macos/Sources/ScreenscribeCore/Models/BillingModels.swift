import Foundation

// MARK: - Products

/// Auto-renewable subscription products in the "Screenscribe Pro" group.
/// Must match `Config/Screenscribe.storekit` and App Store Connect exactly.
public enum StoreProductID {
    public static let monthly = "io.vetcoders.screenscribe.pro.monthly"
    public static let annual = "io.vetcoders.screenscribe.pro.annual"
    public static let all: [String] = [monthly, annual]
    public static let subscriptionGroupName = "Screenscribe Pro"
}

/// `environment` field of `POST /api/v1/billing/appstore/sync`.
public enum StoreEnvironment: String, Codable, Sendable, Equatable {
    case production = "Production"
    case sandbox = "Sandbox"
    case xcode = "Xcode"

    /// Maps StoreKit's `AppStore.Environment.rawValue` (`"Production"`,
    /// `"Sandbox"`, `"Xcode"`) — case-insensitively — to the wire value.
    public init(storeKitRawValue: String) {
        switch storeKitRawValue.trimmingCharacters(in: .whitespaces).lowercased() {
        case "sandbox": self = .sandbox
        case "xcode", "localtesting", "local": self = .xcode
        default: self = .production
        }
    }
}

public enum SubscriptionPeriodUnit: String, Sendable, Equatable {
    case day
    case week
    case month
    case year
}

public struct SubscriptionPeriod: Sendable, Equatable {
    public var value: Int
    public var unit: SubscriptionPeriodUnit

    public init(value: Int, unit: SubscriptionPeriodUnit) {
        self.value = value
        self.unit = unit
    }

    /// "per month" / "every 3 months" style label.
    public var localizedDescription: String {
        let count = max(1, value)
        switch unit {
        case .day:
            return count == 1 ? L10n.string("billing.period.day") : L10n.format("billing.period.days", count)
        case .week:
            return count == 1 ? L10n.string("billing.period.week") : L10n.format("billing.period.weeks", count)
        case .month:
            return count == 1 ? L10n.string("billing.period.month") : L10n.format("billing.period.months", count)
        case .year:
            return count == 1 ? L10n.string("billing.period.year") : L10n.format("billing.period.years", count)
        }
    }

    /// "1 week" / "3 days" style duration (intro offers), localized by Foundation.
    public var localizedDuration: String {
        let count = max(1, value)
        var components = DateComponents()
        switch unit {
        case .day: components.day = count
        case .week: components.weekOfMonth = count
        case .month: components.month = count
        case .year: components.year = count
        }
        let formatter = DateComponentsFormatter()
        formatter.unitsStyle = .full
        formatter.allowedUnits = [.day, .weekOfMonth, .month, .year]
        formatter.maximumUnitCount = 1
        return formatter.string(from: components) ?? localizedDescription
    }
}

/// A StoreKit product reduced to the values the paywall renders.
public struct SubscriptionProduct: Sendable, Equatable, Identifiable {
    public var id: String
    public var displayName: String
    public var productDescription: String
    public var displayPrice: String
    public var price: Decimal
    public var period: SubscriptionPeriod?
    /// Localized introductory offer label (e.g. a free trial) when eligible.
    public var introductoryOffer: String?
    public var isFamilyShareable: Bool

    public init(
        id: String,
        displayName: String,
        productDescription: String,
        displayPrice: String,
        price: Decimal,
        period: SubscriptionPeriod?,
        introductoryOffer: String? = nil,
        isFamilyShareable: Bool = false
    ) {
        self.id = id
        self.displayName = displayName
        self.productDescription = productDescription
        self.displayPrice = displayPrice
        self.price = price
        self.period = period
        self.introductoryOffer = introductoryOffer
        self.isFamilyShareable = isFamilyShareable
    }

    public var isAnnual: Bool {
        id == StoreProductID.annual || period?.unit == .year
    }
}

// MARK: - Transactions

/// A StoreKit 2 transaction whose JWS passed on-device verification.
/// Band 2 re-verifies `signedTransactionInfo` server-side before granting.
public struct VerifiedTransaction: Sendable, Equatable, Identifiable {
    public var transactionID: UInt64
    public var originalTransactionID: UInt64
    public var productID: String
    /// `VerificationResult<Transaction>.jwsRepresentation`.
    public var signedTransactionInfo: String
    /// `VerificationResult<RenewalInfo>.jwsRepresentation`, when available.
    public var signedRenewalInfo: String?
    public var environment: StoreEnvironment
    public var purchaseDate: Date
    public var expirationDate: Date?
    public var revocationDate: Date?
    public var isUpgraded: Bool

    public init(
        transactionID: UInt64,
        originalTransactionID: UInt64,
        productID: String,
        signedTransactionInfo: String,
        signedRenewalInfo: String? = nil,
        environment: StoreEnvironment,
        purchaseDate: Date,
        expirationDate: Date? = nil,
        revocationDate: Date? = nil,
        isUpgraded: Bool = false
    ) {
        self.transactionID = transactionID
        self.originalTransactionID = originalTransactionID
        self.productID = productID
        self.signedTransactionInfo = signedTransactionInfo
        self.signedRenewalInfo = signedRenewalInfo
        self.environment = environment
        self.purchaseDate = purchaseDate
        self.expirationDate = expirationDate
        self.revocationDate = revocationDate
        self.isUpgraded = isUpgraded
    }

    public var id: UInt64 { transactionID }

    /// Revoked or expired transactions are still synced (so the ledger can
    /// downgrade) but never count as an active entitlement locally.
    public func isActiveEntitlement(at date: Date = Date()) -> Bool {
        guard revocationDate == nil, !isUpgraded else { return false }
        if let expirationDate {
            return expirationDate > date
        }
        return true
    }
}

/// Raw result of `Product.purchase()` after verification.
public enum StorePurchaseResult: Sendable, Equatable {
    case success(VerifiedTransaction)
    /// Ask to Buy / SCA: the transaction arrives later via `Transaction.updates`.
    case pending
    case userCancelled
}

/// What the paywall shows after a purchase attempt.
public enum PurchaseOutcome: Sendable, Equatable {
    /// Synced with Band 2 and finished; carries the refreshed profile.
    case purchased(UserProfile)
    case pending
    case cancelled
}

/// `POST /api/v1/billing/appstore/sync` body.
public struct AppStoreSyncRequest: Encodable, Sendable, Equatable {
    public var signedTransactionInfo: String
    public var signedRenewalInfo: String?
    public var environment: String

    public init(signedTransactionInfo: String, signedRenewalInfo: String?, environment: StoreEnvironment) {
        self.signedTransactionInfo = signedTransactionInfo
        self.signedRenewalInfo = signedRenewalInfo
        self.environment = environment.rawValue
    }

    public init(transaction: VerifiedTransaction) {
        self.init(
            signedTransactionInfo: transaction.signedTransactionInfo,
            signedRenewalInfo: transaction.signedRenewalInfo,
            environment: transaction.environment
        )
    }

    enum CodingKeys: String, CodingKey {
        case signedTransactionInfo = "signed_transaction_info"
        case signedRenewalInfo = "signed_renewal_info"
        case environment
    }
}

// MARK: - 402 Payment Required

/// Hard subscription gate body. The Mac App Store build never opens
/// `upgradeURL` (App Review 3.1.1) — it presents the StoreKit paywall instead.
public struct PaymentRequiredPayload: Decodable, Sendable, Equatable {
    public static let noActiveSubscription = "no_active_subscription"
    public static let quotaExceeded = "quota_exceeded"

    public var error: String
    public var code: String
    public var message: String?
    public var upgradeURL: String?

    public init(
        error: String = "subscription_required",
        code: String = PaymentRequiredPayload.noActiveSubscription,
        message: String? = nil,
        upgradeURL: String? = nil
    ) {
        self.error = error
        self.code = code
        self.message = message
        self.upgradeURL = upgradeURL
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: AnyCodingKey.self)
        if container.lossyNonEmptyString(AnyCodingKey("error")) == nil,
           container.lossyNonEmptyString(AnyCodingKey("code")) == nil,
           let nested = try? container.decodeIfPresent(PaymentRequiredPayload.self, forKey: AnyCodingKey("detail")) {
            self = nested
            return
        }
        error = container.lossyNonEmptyString(AnyCodingKey("error")) ?? "subscription_required"
        code = container.lossyNonEmptyString(AnyCodingKey("code")) ?? PaymentRequiredPayload.noActiveSubscription
        message = container.lossyNonEmptyString(AnyCodingKey("message"))
            ?? container.lossyNonEmptyString(AnyCodingKey("detail"))
        upgradeURL = container.lossyNonEmptyString(AnyCodingKey("upgrade_url"))
    }

    /// Never throws: an unparseable 402 still means "subscription required".
    public static func decode(from data: Data) -> PaymentRequiredPayload {
        (try? WireCoding.makeDecoder().decode(PaymentRequiredPayload.self, from: data)) ?? PaymentRequiredPayload()
    }

    public var isQuotaExceeded: Bool {
        code == PaymentRequiredPayload.quotaExceeded
    }

    public var localizedMessage: String {
        isQuotaExceeded ? L10n.string("paywall.reason.quota") : L10n.string("paywall.reason.inactive")
    }
}

public typealias QuotaErrorPayload = PaymentRequiredPayload
