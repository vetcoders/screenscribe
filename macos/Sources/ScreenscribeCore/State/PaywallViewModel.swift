import Foundation
import Observation

/// Screenscribe Pro paywall: StoreKit 2 products, purchase, restore.
///
/// App Review 3.1.1: purchases happen only through StoreKit. A `402`
/// `upgrade_url` from Band 2 is never opened — only its message is shown.
@MainActor
@Observable
public final class PaywallViewModel {
    public private(set) var products: [SubscriptionProduct] = []
    public private(set) var isLoadingProducts = false
    public private(set) var purchasingProductID: String?
    public private(set) var isRestoring = false
    public private(set) var errorMessage: String?
    /// Non-error status (pending approval, nothing to restore).
    public private(set) var statusMessage: String?
    public var selectedProductID: String?
    /// Why the paywall opened (`402` payload), if a job triggered it.
    public var reason: PaymentRequiredPayload?
    /// Set when a signed-out user tapped Subscribe: re-open after sign-in.
    var resumeAfterSignIn = false

    @ObservationIgnored private weak var app: AppModel?

    public init() {}

    func attach(to app: AppModel) {
        self.app = app
    }

    public var isBusy: Bool {
        purchasingProductID != nil || isRestoring
    }

    public var selectedProduct: SubscriptionProduct? {
        products.first { $0.id == selectedProductID } ?? products.first
    }

    /// Headline under the title: the `402` reason or the generic pitch.
    public var headline: String {
        reason?.localizedMessage ?? L10n.string("paywall.subtitle")
    }

    /// Savings of the annual plan versus twelve monthly payments, if any.
    public var annualSavingsPercent: Int? {
        guard let monthly = products.first(where: { !$0.isAnnual && $0.period?.unit == .month && $0.period?.value == 1 }),
              let annual = products.first(where: { $0.isAnnual })
        else {
            return nil
        }
        let yearlyAtMonthly = NSDecimalNumber(decimal: monthly.price).doubleValue * 12
        let annualPrice = NSDecimalNumber(decimal: annual.price).doubleValue
        guard yearlyAtMonthly > 0, annualPrice < yearlyAtMonthly else { return nil }
        let percent = Int(((yearlyAtMonthly - annualPrice) / yearlyAtMonthly * 100).rounded(.down))
        return percent > 0 ? percent : nil
    }

    // MARK: Products

    public func loadProducts() async {
        guard let app, !isLoadingProducts else { return }
        isLoadingProducts = true
        defer { isLoadingProducts = false }
        do {
            let loaded = try await app.storeKit.loadProducts()
            products = loaded
            if selectedProductID == nil || !loaded.contains(where: { $0.id == selectedProductID }) {
                selectedProductID = loaded.first(where: { $0.isAnnual })?.id ?? loaded.first?.id
            }
            if loaded.isEmpty {
                errorMessage = L10n.string("paywall.error.unavailable")
            } else if errorMessage == L10n.string("paywall.error.unavailable") {
                errorMessage = nil
            }
        } catch {
            errorMessage = L10n.format("paywall.error.load", error.localizedDescription)
        }
    }

    // MARK: Purchase

    /// Buys the selected plan. The entitlement is attached to the signed-in
    /// Screenscribe account, so a signed-out user is asked to sign in first.
    public func purchaseSelected() async {
        guard let product = selectedProduct else {
            await loadProducts()
            return
        }
        await purchase(product)
    }

    public func purchase(_ product: SubscriptionProduct) async {
        guard let app, !isBusy else { return }
        guard app.isSignedIn else {
            resumeAfterSignIn = true
            app.presentSignIn()
            return
        }
        errorMessage = nil
        statusMessage = nil
        purchasingProductID = product.id
        defer { purchasingProductID = nil }
        do {
            switch try await app.storeKit.purchase(productID: product.id) {
            case .purchased(let profile):
                app.profileUpdated(profile)
                if !profile.subscription.canCreateJobs {
                    statusMessage = L10n.string("paywall.status.syncing")
                }
            case .pending:
                statusMessage = L10n.string("paywall.status.pending")
            case .cancelled:
                break
            }
        } catch {
            errorMessage = Self.message(for: error)
        }
    }

    /// `AppStore.sync()` + re-sync of current entitlements with Band 2.
    public func restore() async {
        guard let app, !isBusy else { return }
        guard app.isSignedIn else {
            resumeAfterSignIn = true
            app.presentSignIn()
            return
        }
        errorMessage = nil
        statusMessage = nil
        isRestoring = true
        defer { isRestoring = false }
        do {
            if let profile = try await app.storeKit.restorePurchases() {
                app.profileUpdated(profile)
                if !profile.subscription.canCreateJobs {
                    statusMessage = L10n.string("paywall.status.nothing")
                }
            } else {
                statusMessage = L10n.string("paywall.status.nothing")
            }
        } catch {
            errorMessage = Self.message(for: error)
        }
    }

    public func clearMessages() {
        errorMessage = nil
        statusMessage = nil
    }

    static func message(for error: any Error) -> String {
        if let storeError = error as? StoreKitClientError {
            return storeError.errorDescription ?? L10n.string("paywall.error.generic")
        }
        let cloudError = CloudError.from(error)
        switch cloudError {
        case .notAuthenticated, .unauthorized:
            return L10n.string("paywall.error.signin")
        case .cancelled:
            return L10n.string("error.cancelled")
        default:
            // The purchase stays unfinished and is synced again on next launch.
            return L10n.format("paywall.error.sync", cloudError.localizedMessage)
        }
    }
}
