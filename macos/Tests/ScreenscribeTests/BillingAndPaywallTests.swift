import Foundation
import ScreenscribeCore
import Testing

final class FakeStoreKitClient: StoreKitClientProtocol, @unchecked Sendable {
    private let lock = NSLock()
    var stubbedProducts: [SubscriptionProduct] = []
    var purchaseResult: StorePurchaseResult = .userCancelled
    var unfinished: [VerifiedTransaction] = []
    var entitlements: [VerifiedTransaction] = []
    private(set) var finishedIDs: [UInt64] = []
    private(set) var appStoreSyncCalls = 0
    private var continuation: AsyncStream<VerifiedTransaction>.Continuation?

    func products(for identifiers: [String]) async throws -> [SubscriptionProduct] {
        lock.withLock { stubbedProducts.filter { identifiers.contains($0.id) } }
    }

    func purchase(productID: String) async throws -> StorePurchaseResult {
        lock.withLock { purchaseResult }
    }

    func finish(transactionID: UInt64) async {
        lock.withLock {
            finishedIDs.append(transactionID)
            unfinished.removeAll { $0.transactionID == transactionID }
        }
    }

    func unfinishedTransactions() async -> [VerifiedTransaction] {
        lock.withLock { unfinished }
    }

    func currentEntitlements() async -> [VerifiedTransaction] {
        lock.withLock { entitlements }
    }

    func transactionUpdates() -> AsyncStream<VerifiedTransaction> {
        AsyncStream { continuation in
            self.lock.withLock {
                self.continuation = continuation
            }
        }
    }

    func emitUpdate(_ tx: VerifiedTransaction) {
        let cont = lock.withLock { continuation }
        cont?.yield(tx)
    }

    func syncWithAppStore() async throws {
        lock.withLock { appStoreSyncCalls += 1 }
    }
}

@Suite("StoreKit 2 Billing and 402 Paywall Invariants")
struct BillingAndPaywallTests {
    @Test("StoreKitManager NEVER calls finish() when Band 2 JWS sync fails, and finishes after retryPendingSync succeeds")
    func finishGatedOnBackendSync() async throws {
        let fake = FakeStoreKitClient()
        let tx = VerifiedTransaction(
            transactionID: 7001,
            originalTransactionID: 7001,
            productID: StoreProductID.monthly,
            signedTransactionInfo: "header.payload.sig",
            environment: .sandbox,
            purchaseDate: Date(timeIntervalSince1970: 1_750_000_000),
            expirationDate: Date(timeIntervalSince1970: 1_850_000_000)
        )
        fake.purchaseResult = .success(tx)
        fake.unfinished = [tx]

        actor SyncGate {
            var shouldFail = true
            func attempt(_ incoming: VerifiedTransaction) throws -> UserProfile {
                if shouldFail {
                    throw CloudError.http(status: 503, message: "Backend unavailable")
                }
                return UserProfile(
                    userID: "usr_1",
                    email: "pro@example.test",
                    subscription: SubscriptionSnapshot(
                        active: true,
                        plan: "pro_monthly",
                        source: .appStore,
                        minutesLimit: 300,
                        minutesUsed: 0
                    )
                )
            }
            func allowSuccess() { shouldFail = false }
        }

        let gate = SyncGate()
        let manager = StoreKitManager(client: fake) { incoming in
            try await gate.attempt(incoming)
        }

        await #expect(throws: CloudError.self) {
            _ = try await manager.purchase(productID: StoreProductID.monthly)
        }
        #expect(fake.finishedIDs.isEmpty)
        #expect(await manager.pendingSyncCount == 1)

        await gate.allowSuccess()
        let recovered = await manager.retryPendingSync()
        #expect(recovered?.subscription.active == true)
        #expect(fake.finishedIDs == [7001])
        #expect(await manager.pendingSyncCount == 0)
    }

    @Test("StoreKitManager sorts monthly before annual and restores entitlements")
    func loadProductsOrderingAndRestore() async throws {
        let fake = FakeStoreKitClient()
        fake.stubbedProducts = [
            SubscriptionProduct(
                id: StoreProductID.annual,
                displayName: "Pro Annual",
                productDescription: "Yearly plan",
                displayPrice: "$199.99",
                price: Decimal(string: "199.99")!,
                period: SubscriptionPeriod(value: 1, unit: .year)
            ),
            SubscriptionProduct(
                id: StoreProductID.monthly,
                displayName: "Pro Monthly",
                productDescription: "Monthly plan",
                displayPrice: "$19.99",
                price: Decimal(string: "19.99")!,
                period: SubscriptionPeriod(value: 1, unit: .month)
            ),
        ]
        let tx = VerifiedTransaction(
            transactionID: 8801,
            originalTransactionID: 8800,
            productID: StoreProductID.annual,
            signedTransactionInfo: "jws.tx.8801",
            environment: .production,
            purchaseDate: Date()
        )
        fake.entitlements = [tx]

        let manager = StoreKitManager(client: fake) { synced in
            #expect(synced.productID == StoreProductID.annual)
            return UserProfile(
                userID: "usr_restore",
                subscription: SubscriptionSnapshot(active: true, plan: "pro_annual", source: .appStore, minutesLimit: 3600)
            )
        }

        let products = try await manager.loadProducts()
        #expect(products.map(\.id) == [StoreProductID.monthly, StoreProductID.annual])

        let restored = try await manager.restorePurchases()
        #expect(restored?.subscription.plan == "pro_annual")
        #expect(fake.appStoreSyncCalls == 1)
        #expect(fake.finishedIDs == [8801])
    }

    @Test("402 PaymentRequiredPayload decodes both no_active_subscription and quota_exceeded")
    func paymentRequiredDecoding() {
        let inactiveJSON = Data("""
        {"error":"subscription_required","code":"no_active_subscription","message":"Subscribe","upgrade_url":"https://cloud.example.test/billing"}
        """.utf8)
        let inactive = PaymentRequiredPayload.decode(from: inactiveJSON)
        #expect(!inactive.isQuotaExceeded)
        #expect(inactive.code == PaymentRequiredPayload.noActiveSubscription)

        let quotaJSON = Data("""
        {"detail":{"error":"subscription_required","code":"quota_exceeded","message":"Out of minutes"}}
        """.utf8)
        let quota = PaymentRequiredPayload.decode(from: quotaJSON)
        #expect(quota.isQuotaExceeded)
        #expect(!quota.localizedMessage.isEmpty)
    }
}
