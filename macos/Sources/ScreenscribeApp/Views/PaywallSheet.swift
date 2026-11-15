import ScreenscribeCore
import SwiftUI

/// Screenscribe Pro paywall (StoreKit 2).
///
/// App Review: purchases happen only through StoreKit (3.1.1 — the server
/// `upgrade_url` is never opened), and the sheet carries the auto-renewal
/// disclosure plus Terms of Use and Privacy Policy links (3.1.2).
struct PaywallSheet: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let paywall = model.paywall
        VStack(alignment: .leading, spacing: 18) {
            header(paywall)

            if model.subscription.active {
                activePlanCard
            }

            features

            plans(paywall)

            if let status = paywall.statusMessage {
                Label(status, systemImage: "clock")
                    .font(.callout)
                    .foregroundStyle(ScreenscribeTheme.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = paywall.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(ScreenscribeTheme.failure)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }

            actions(paywall)

            legal
        }
        .padding(28)
        .frame(width: 480)
        .task {
            if paywall.products.isEmpty {
                await paywall.loadProducts()
            }
        }
    }

    // MARK: Sections

    private func header(_ paywall: PaywallViewModel) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(StoreProductID.subscriptionGroupName)
                .font(ScreenscribeTheme.monoTitle)
            Text(paywall.headline)
                .font(.callout)
                .foregroundStyle(paywall.reason == nil ? Color.secondary : ScreenscribeTheme.warning)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var activePlanCard: some View {
        let subscription = model.subscription
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(L10n.string("paywall.active"), systemImage: "checkmark.seal.fill")
                    .foregroundStyle(ScreenscribeTheme.success)
                Spacer()
                if let source = subscription.source {
                    StatusBadge(text: source.localizedTitle, tint: ScreenscribeTheme.textSecondary)
                }
            }
            if let periodEnd = subscription.periodEnd {
                Text(L10n.format("paywall.active.renews", DisplayFormat.dateTime(periodEnd)))
                    .font(ScreenscribeTheme.monoCaption)
                    .foregroundStyle(.secondary)
            }
            if subscription.minutesLimit > 0 {
                ProgressView(value: subscription.usageFraction)
                    .tint(subscription.usageFraction > 0.9 ? ScreenscribeTheme.warning : .accentColor)
                Text(L10n.format(
                    "account.minutes",
                    DisplayFormat.minutes(subscription.minutesUsed),
                    DisplayFormat.minutes(subscription.minutesLimit)
                ))
                .font(ScreenscribeTheme.monoCaption)
                .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .screenscribeCard()
    }

    private var features: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(["paywall.feature.pipeline", "paywall.feature.reports", "paywall.feature.share"], id: \.self) { key in
                Label(L10n.string(key), systemImage: "checkmark")
                    .font(.callout)
            }
        }
    }

    @ViewBuilder
    private func plans(_ paywall: PaywallViewModel) -> some View {
        if paywall.isLoadingProducts, paywall.products.isEmpty {
            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text(L10n.string("paywall.loading"))
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 90)
        } else if paywall.products.isEmpty {
            Button(L10n.string("paywall.retry")) {
                Task { await paywall.loadProducts() }
            }
            .screenscribeSecondaryButton()
            .frame(maxWidth: .infinity, minHeight: 60)
        } else {
            VStack(spacing: 10) {
                ForEach(paywall.products) { product in
                    PlanOptionRow(
                        product: product,
                        isSelected: paywall.selectedProduct?.id == product.id,
                        savingsPercent: product.isAnnual ? paywall.annualSavingsPercent : nil
                    ) {
                        paywall.selectedProductID = product.id
                    }
                    .disabled(paywall.isBusy)
                }
            }
            .screenscribeGlassGroup(spacing: 10)
        }
    }

    private func actions(_ paywall: PaywallViewModel) -> some View {
        HStack(spacing: 10) {
            Button(L10n.string("paywall.restore")) {
                Task { await paywall.restore() }
            }
            .screenscribeSecondaryButton()
            .disabled(paywall.isBusy)

            if model.subscription.source == .appStore {
                Link(L10n.string("paywall.manage"), destination: AppConfiguration.manageSubscriptionsURL)
                    .font(.callout)
            }

            Spacer()

            Button(L10n.string("common.close")) {
                model.dismissSheet()
            }
            .keyboardShortcut(.cancelAction)

            Button {
                Task { await paywall.purchaseSelected() }
            } label: {
                HStack(spacing: 6) {
                    if paywall.purchasingProductID != nil || paywall.isRestoring {
                        ProgressView().controlSize(.small)
                    }
                    Text(L10n.string(model.isSignedIn ? "paywall.subscribe" : "paywall.signin.subscribe"))
                }
                .frame(minWidth: 140)
            }
            .keyboardShortcut(.defaultAction)
            .screenscribePrimaryButton()
            .disabled(paywall.isBusy || paywall.selectedProduct == nil)
        }
    }

    private var legal: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(L10n.string("paywall.legal.renewal"))
                .font(ScreenscribeTheme.monoCaption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 14) {
                Link(L10n.string("legal.terms"), destination: AppConfiguration.termsOfUseURL)
                Link(L10n.string("legal.privacy"), destination: AppConfiguration.privacyPolicyURL)
            }
            .font(ScreenscribeTheme.monoCaption)
        }
    }
}

/// One selectable plan: name, price per period, optional intro offer and
/// annual savings.
struct PlanOptionRow: View {
    let product: SubscriptionProduct
    let isSelected: Bool
    let savingsPercent: Int?
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(alignment: .center, spacing: 12) {
                Image(systemName: isSelected ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(isSelected ? Color.accentColor : ScreenscribeTheme.textSecondary)
                    .font(.title3)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(product.displayName)
                            .font(.headline)
                        if let savingsPercent {
                            StatusBadge(
                                text: L10n.format("paywall.savings", savingsPercent),
                                tint: ScreenscribeTheme.success
                            )
                        }
                    }
                    if !product.productDescription.isEmpty {
                        Text(product.productDescription)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                    if let offer = product.introductoryOffer {
                        Text(offer)
                            .font(ScreenscribeTheme.monoCaption)
                            .foregroundStyle(ScreenscribeTheme.success)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text(product.displayPrice)
                        .font(ScreenscribeTheme.mono(15, weight: .semibold))
                    if let period = product.period {
                        Text(period.localizedDescription)
                            .font(ScreenscribeTheme.monoCaption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .contentShape(Rectangle())
            .frame(maxWidth: .infinity, alignment: .leading)
            .screenscribeCard(cornerRadius: ScreenscribeTheme.compactCornerRadius)
            .overlay(
                RoundedRectangle(cornerRadius: ScreenscribeTheme.compactCornerRadius, style: .continuous)
                    .strokeBorder(isSelected ? Color.accentColor : .clear, lineWidth: 1.5)
            )
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
        .accessibilityLabel(Text(verbatim: "\(product.displayName), \(product.displayPrice)"))
    }
}
