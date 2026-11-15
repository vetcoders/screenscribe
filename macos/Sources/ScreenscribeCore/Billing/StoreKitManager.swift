import Foundation
import StoreKit

/// The StoreKit 2 surface the app depends on. `StoreKit2Client` is the live
/// implementation; the integrator's tests inject a deterministic fake.
public protocol StoreKitClientProtocol: Sendable {
    func products(for identifiers: [String]) async throws -> [SubscriptionProduct]
    func purchase(productID: String) async throws -> StorePurchaseResult
    /// Finishes a transaction. Only called after Band 2 accepted its JWS.
    func finish(transactionID: UInt64) async
    func unfinishedTransactions() async -> [VerifiedTransaction]
    func currentEntitlements() async -> [VerifiedTransaction]
    /// `Transaction.updates`: renewals, Ask to Buy approvals, refunds,
    /// purchases made on another Mac.
    func transactionUpdates() -> AsyncStream<VerifiedTransaction>
    /// `AppStore.sync()` (Restore Purchases).
    func syncWithAppStore() async throws
}

public enum StoreKitClientError: Error, Equatable, Sendable, LocalizedError {
    case productNotFound(String)
    case failedVerification(String)
    case purchasesNotAllowed

    public var errorDescription: String? {
        switch self {
        case .productNotFound: return L10n.string("paywall.error.product")
        case .failedVerification: return L10n.string("paywall.error.verification")
        case .purchasesNotAllowed: return L10n.string("paywall.error.notallowed")
        }
    }
}

/// Keeps StoreKit objects so `finish(transactionID:)` can finish the exact
/// `Transaction` that was verified.
actor StoreKitObjectCache {
    private var products: [String: Product] = [:]
    private var transactions: [UInt64: Transaction] = [:]

    func store(_ products: [Product]) {
        for product in products {
            self.products[product.id] = product
        }
    }

    func product(id: String) -> Product? {
        products[id]
    }

    func store(_ transaction: Transaction) {
        transactions[transaction.id] = transaction
    }

    func takeTransaction(id: UInt64) -> Transaction? {
        transactions.removeValue(forKey: id)
    }
}

/// Live StoreKit 2 client.
public final class StoreKit2Client: StoreKitClientProtocol {
    private let cache = StoreKitObjectCache()

    public init() {}

    public func products(for identifiers: [String]) async throws -> [SubscriptionProduct] {
        let products = try await Product.products(for: identifiers)
        await cache.store(products)
        var result: [SubscriptionProduct] = []
        for product in products {
            result.append(await Self.makeProduct(product))
        }
        return result
    }

    public func purchase(productID: String) async throws -> StorePurchaseResult {
        guard AppStore.canMakePayments else {
            throw StoreKitClientError.purchasesNotAllowed
        }
        let product: Product
        if let cached = await cache.product(id: productID) {
            product = cached
        } else if let fetched = try await Product.products(for: [productID]).first {
            await cache.store([fetched])
            product = fetched
        } else {
            throw StoreKitClientError.productNotFound(productID)
        }

        let result = try await product.purchase()
        switch result {
        case .success(let verification):
            return try await .success(verified(verification))
        case .pending:
            return .pending
        case .userCancelled:
            return .userCancelled
        default:
            return .pending
        }
    }

    public func finish(transactionID: UInt64) async {
        if let transaction = await cache.takeTransaction(id: transactionID) {
            await transaction.finish()
            return
        }
        for await result in Transaction.unfinished {
            if case .verified(let transaction) = result, transaction.id == transactionID {
                await transaction.finish()
                return
            }
        }
    }

    public func unfinishedTransactions() async -> [VerifiedTransaction] {
        var result: [VerifiedTransaction] = []
        for await verification in Transaction.unfinished {
            if let transaction = try? await verified(verification) {
                result.append(transaction)
            }
        }
        return result
    }

    public func currentEntitlements() async -> [VerifiedTransaction] {
        var result: [VerifiedTransaction] = []
        for await verification in Transaction.currentEntitlements {
            if let transaction = try? await verified(verification),
               StoreProductID.all.contains(transaction.productID) {
                result.append(transaction)
            }
        }
        return result
    }

    public func transactionUpdates() -> AsyncStream<VerifiedTransaction> {
        AsyncStream { continuation in
            let task = Task {
                for await verification in Transaction.updates {
                    if let transaction = try? await self.verified(verification) {
                        continuation.yield(transaction)
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    public func syncWithAppStore() async throws {
        try await AppStore.sync()
    }

    // MARK: Mapping

    /// Rejects unverified JWS; keeps the `Transaction` for a later `finish()`.
    func verified(_ verification: VerificationResult<Transaction>) async throws -> VerifiedTransaction {
        switch verification {
        case .unverified(_, let error):
            throw StoreKitClientError.failedVerification(error.localizedDescription)
        case .verified(let transaction):
            await cache.store(transaction)
            var renewalJWS: String?
            if transaction.productType == .autoRenewable, let status = await transaction.subscriptionStatus {
                renewalJWS = status.renewalInfo.jwsRepresentation
            }
            return VerifiedTransaction(
                transactionID: transaction.id,
                originalTransactionID: transaction.originalID,
                productID: transaction.productID,
                signedTransactionInfo: verification.jwsRepresentation,
                signedRenewalInfo: renewalJWS,
                environment: StoreEnvironment(storeKitRawValue: transaction.environment.rawValue),
                purchaseDate: transaction.purchaseDate,
                expirationDate: transaction.expirationDate,
                revocationDate: transaction.revocationDate,
                isUpgraded: transaction.isUpgraded
            )
        }
    }

    static func makeProduct(_ product: Product) async -> SubscriptionProduct {
        var period: SubscriptionPeriod?
        var introductoryOffer: String?
        if let subscription = product.subscription {
            let subscriptionPeriod = mapPeriod(subscription.subscriptionPeriod)
            period = subscriptionPeriod
            if let offer = subscription.introductoryOffer, await subscription.isEligibleForIntroOffer {
                let offerPeriod = mapPeriod(offer.period).localizedDuration
                if offer.paymentMode == .freeTrial {
                    introductoryOffer = L10n.format("paywall.offer.trial", offerPeriod)
                } else {
                    introductoryOffer = L10n.format("paywall.offer.intro", offer.displayPrice, offerPeriod)
                }
            }
        }
        return SubscriptionProduct(
            id: product.id,
            displayName: product.displayName,
            productDescription: product.description,
            displayPrice: product.displayPrice,
            price: product.price,
            period: period,
            introductoryOffer: introductoryOffer,
            isFamilyShareable: product.isFamilyShareable
        )
    }

    static func mapPeriod(_ period: Product.SubscriptionPeriod) -> SubscriptionPeriod {
        let unit: SubscriptionPeriodUnit
        switch period.unit {
        case .day: unit = .day
        case .week: unit = .week
        case .month: unit = .month
        case .year: unit = .year
        default: unit = .month
        }
        return SubscriptionPeriod(value: period.value, unit: unit)
    }
}

/// Purchase, restore and renewal orchestration.
///
/// Invariant: a transaction is finished **only after** Band 2 accepted its
/// JWS (`POST /api/v1/billing/appstore/sync`). If the sync fails (offline,
/// signed out, 5xx) the transaction stays unfinished, StoreKit redelivers it,
/// and `retryPendingSync()` tries again — the customer is never charged
/// without the entitlement landing on their account.
public actor StoreKitManager {
    public typealias Syncer = @Sendable (VerifiedTransaction) async throws -> UserProfile

    private let client: any StoreKitClientProtocol
    private var syncer: Syncer?
    private var listener: Task<Void, Never>?
    private var pendingSync: [UInt64: VerifiedTransaction] = [:]

    public private(set) var products: [SubscriptionProduct] = []

    public init(client: any StoreKitClientProtocol = StoreKit2Client(), syncer: Syncer? = nil) {
        self.client = client
        self.syncer = syncer
    }

    /// Installs (or removes, on sign-out) the Band 2 sync call.
    public func updateSyncer(_ syncer: Syncer?) {
        self.syncer = syncer
    }

    public var pendingSyncCount: Int {
        pendingSync.count
    }

    /// Monthly first, then annual.
    @discardableResult
    public func loadProducts() async throws -> [SubscriptionProduct] {
        let loaded = try await client.products(for: StoreProductID.all)
        products = loaded.sorted { lhs, rhs in
            if lhs.isAnnual != rhs.isAnnual {
                return !lhs.isAnnual
            }
            return lhs.price < rhs.price
        }
        return products
    }

    /// Purchase → sync JWS to Band 2 → finish.
    public func purchase(productID: String) async throws -> PurchaseOutcome {
        switch try await client.purchase(productID: productID) {
        case .success(let transaction):
            return try await .purchased(syncAndFinish(transaction))
        case .pending:
            return .pending
        case .userCancelled:
            return .cancelled
        }
    }

    /// `AppStore.sync()`, then re-sync every current entitlement and any
    /// unfinished transaction. Returns the latest profile, if any synced.
    public func restorePurchases() async throws -> UserProfile? {
        try await client.syncWithAppStore()
        var latest: UserProfile?
        var lastError: (any Error)?
        var seen = Set<UInt64>()
        let candidates = await client.currentEntitlements() + client.unfinishedTransactions()
        for transaction in candidates where seen.insert(transaction.transactionID).inserted {
            do {
                latest = try await syncAndFinish(transaction)
            } catch {
                lastError = error
            }
        }
        if latest == nil, let lastError {
            throw lastError
        }
        return latest
    }

    /// Starts the `Transaction.updates` listener (call once at launch).
    public func startTransactionListener(onProfile: @escaping @Sendable (UserProfile) async -> Void) {
        guard listener == nil else { return }
        let updates = client.transactionUpdates()
        listener = Task { [weak self] in
            for await transaction in updates {
                guard let self else { return }
                if let profile = try? await self.syncAndFinish(transaction) {
                    await onProfile(profile)
                }
            }
        }
    }

    public func stopTransactionListener() {
        listener?.cancel()
        listener = nil
    }

    /// Retries transactions whose sync failed earlier plus anything StoreKit
    /// still reports as unfinished (e.g. bought while signed out).
    @discardableResult
    public func retryPendingSync() async -> UserProfile? {
        var candidates = Array(pendingSync.values)
        let known = Set(candidates.map(\.transactionID))
        for transaction in await client.unfinishedTransactions() where !known.contains(transaction.transactionID) {
            candidates.append(transaction)
        }
        var latest: UserProfile?
        for transaction in candidates {
            if let profile = try? await syncAndFinish(transaction) {
                latest = profile
            }
        }
        return latest
    }

    func syncAndFinish(_ transaction: VerifiedTransaction) async throws -> UserProfile {
        guard let syncer else {
            pendingSync[transaction.transactionID] = transaction
            throw CloudError.notAuthenticated
        }
        do {
            let profile = try await syncer(transaction)
            pendingSync[transaction.transactionID] = nil
            await client.finish(transactionID: transaction.transactionID)
            return profile
        } catch {
            pendingSync[transaction.transactionID] = transaction
            throw error
        }
    }
}
