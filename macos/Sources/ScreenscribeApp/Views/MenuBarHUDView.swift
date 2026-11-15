import AppKit
import ScreenscribeCore
import SwiftUI

/// Menu bar HUD (`MenuBarExtra` `.window` style): start/pause/stop a
/// recording, see the upload queue and jump to recent reviews.
///
/// The capture engine excludes every window of this process, so the HUD
/// never shows up in a recording.
struct MenuBarHUDView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            HUDRecorderSection(openWorkspace: { openWorkspace() })
            if !model.workspace.openItems.isEmpty {
                HUDQueueSection(openWorkspace: { openWorkspace() })
            }
            if !model.workspace.recentReviews.isEmpty {
                HUDRecentReviewsSection(openWorkspace: { openWorkspace() })
            }
            if let banner = model.bannerMessage {
                Label(banner, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(ScreenscribeTheme.warning)
                    .lineLimit(3)
            }
            Divider()
            footer
        }
        .padding(14)
        .frame(width: 340)
        .screenscribeGlassGroup(spacing: 10)
        .onAppear {
            model.recorder.refreshPermissions()
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "record.circle")
                .foregroundStyle(model.recorder.isRecordingOrPaused ? ScreenscribeTheme.recording : ScreenscribeTheme.textSecondary)
            Text(L10n.string("app.name"))
                .font(ScreenscribeTheme.mono(14, weight: .semibold))
            Spacer()
            if model.isSignedIn {
                let subscription = model.subscription
                StatusBadge(
                    text: subscription.active ? (subscription.plan ?? L10n.string("account.plan.pro")) : L10n.string("account.plan.none"),
                    tint: subscription.active ? ScreenscribeTheme.success : ScreenscribeTheme.textSecondary
                )
            } else {
                Button(L10n.string("menu.signin")) {
                    openWorkspace()
                    model.presentSignIn()
                }
                .controlSize(.small)
                .screenscribeSecondaryButton()
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Button {
                openWorkspace()
            } label: {
                Label(L10n.string("hud.open"), systemImage: "macwindow")
            }
            .buttonStyle(.borderless)
            Spacer()
            SettingsLink {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.borderless)
            .help(L10n.string("hud.settings"))
            Button {
                NSApplication.shared.terminate(nil)
            } label: {
                Image(systemName: "power")
            }
            .buttonStyle(.borderless)
            .help(L10n.string("hud.quit"))
            .disabled(model.recorder.state.isBusy)
        }
        .font(.callout)
    }

    private func openWorkspace() {
        openWindow(id: ScreenscribeMacApp.workspaceWindowID)
        NSApplication.shared.activate()
    }
}

// MARK: - Recorder

struct HUDRecorderSection: View {
    @Environment(AppModel.self) private var model
    let openWorkspace: () -> Void

    var body: some View {
        let recorder = model.recorder
        VStack(alignment: .leading, spacing: 10) {
            if !recorder.permissions.canRecordScreen {
                Label(L10n.string("recorder.permission.screen.title"), systemImage: "rectangle.dashed.badge.record")
                    .font(.callout.weight(.medium))
                HStack {
                    Button(L10n.string("recorder.permission.grant")) {
                        recorder.requestScreenRecordingAccess()
                    }
                    .controlSize(.small)
                    .screenscribePrimaryButton()
                    Button(L10n.string("recorder.permission.settings")) {
                        PermissionPreflight.openSettings(PermissionPreflight.screenRecordingSettingsURL)
                    }
                    .controlSize(.small)
                    .screenscribeSecondaryButton()
                }
            } else {
                controls(recorder)
            }

            if let error = recorder.errorMessage {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(ScreenscribeTheme.failure)
                    .lineLimit(3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .screenscribeCard(cornerRadius: ScreenscribeTheme.compactCornerRadius)
    }

    @ViewBuilder
    private func controls(_ recorder: RecorderViewModel) -> some View {
        switch recorder.state {
        case .idle:
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: sourceSymbol(recorder))
                        .foregroundStyle(ScreenscribeTheme.textSecondary)
                    Text(recorder.selectedSource?.localizedTitle ?? L10n.string("hud.source.none"))
                        .font(ScreenscribeTheme.monoCaption)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    Button(L10n.string("hud.source.change")) {
                        model.workspace.selection = .record
                        openWorkspace()
                    }
                    .buttonStyle(.link)
                    .font(.caption)
                }
                Button {
                    recorder.startRecording()
                } label: {
                    Label(L10n.string("recorder.start"), systemImage: "record.circle.fill")
                        .frame(maxWidth: .infinity)
                }
                .controlSize(.large)
                .screenscribePrimaryButton()
                .disabled(!recorder.canStart)
            }
            .task {
                if recorder.catalog == .empty, !recorder.isLoadingSources {
                    await recorder.refreshSources()
                }
            }
        case .countingDown(let remaining):
            HStack(spacing: 12) {
                Text(String(remaining))
                    .font(ScreenscribeTheme.mono(30, weight: .bold))
                    .contentTransition(.numericText(countsDown: true))
                    .monospacedDigit()
                Text(L10n.string("recorder.countdown"))
                    .font(.callout)
                    .foregroundStyle(ScreenscribeTheme.textSecondary)
                Spacer()
                Button(L10n.string("common.cancel")) {
                    recorder.cancelCountdown()
                }
                .controlSize(.small)
                .screenscribeSecondaryButton()
            }
        case .preparing, .finalizing:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(L10n.string(recorder.state == .preparing ? "recorder.preparing" : "recorder.finalizing"))
                    .font(.callout)
            }
        case .recording, .paused:
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    Circle()
                        .fill(recorder.state == .recording ? ScreenscribeTheme.recording : ScreenscribeTheme.textSecondary)
                        .frame(width: 10, height: 10)
                    Text(recorder.elapsedText)
                        .font(ScreenscribeTheme.mono(24, weight: .medium))
                        .monospacedDigit()
                    if recorder.state == .paused {
                        StatusBadge(text: L10n.string("recorder.paused"), tint: ScreenscribeTheme.warning)
                    }
                    Spacer()
                }
                HStack(spacing: 8) {
                    Button {
                        Task { await recorder.togglePause() }
                    } label: {
                        Label(
                            L10n.string(recorder.state == .paused ? "recorder.resume" : "recorder.pause"),
                            systemImage: recorder.state == .paused ? "play.fill" : "pause.fill"
                        )
                    }
                    .screenscribeSecondaryButton()
                    Button {
                        Task { await recorder.stopRecording() }
                    } label: {
                        Label(L10n.string("recorder.stop"), systemImage: "stop.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .screenscribePrimaryButton()
                }
            }
        }
    }

    private func sourceSymbol(_ recorder: RecorderViewModel) -> String {
        switch recorder.sourceKind {
        case .display: return "display"
        case .window: return "macwindow"
        case .application: return "app.dashed"
        }
    }
}

// MARK: - Queue

struct HUDQueueSection: View {
    @Environment(AppModel.self) private var model
    let openWorkspace: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(L10n.string("hud.queue"))
                .font(ScreenscribeTheme.monoCaption)
                .foregroundStyle(ScreenscribeTheme.textSecondary)
            ForEach(model.workspace.openItems.prefix(3)) { item in
                Button {
                    model.workspace.selection = .upload(item.id)
                    openWorkspace()
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            Image(systemName: item.state.symbolName)
                                .foregroundStyle(item.state.tint)
                            Text(item.displayName)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer()
                            Text(progressText(item))
                                .font(ScreenscribeTheme.monoCaption)
                                .foregroundStyle(ScreenscribeTheme.textSecondary)
                                .monospacedDigit()
                        }
                        if item.state == .uploading {
                            ProgressView(value: item.uploadFraction)
                                .controlSize(.mini)
                        } else if item.state == .processing {
                            ProgressView(value: min(1, max(0, item.progressPct / 100)))
                                .controlSize(.mini)
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .font(.callout)
            }
            if model.workspace.openItems.count > 3 {
                Text(L10n.format("hud.queue.more", model.workspace.openItems.count - 3))
                    .font(.caption)
                    .foregroundStyle(ScreenscribeTheme.textSecondary)
            }
        }
    }

    private func progressText(_ item: UploadQueueItem) -> String {
        switch item.state {
        case .uploading:
            return DisplayFormat.percent(item.uploadFraction * 100)
        case .processing:
            return item.stage.localizedTitle
        default:
            return item.state.localizedTitle
        }
    }
}

// MARK: - Recent reviews

struct HUDRecentReviewsSection: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL
    let openWorkspace: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(L10n.string("hud.reviews"))
                .font(ScreenscribeTheme.monoCaption)
                .foregroundStyle(ScreenscribeTheme.textSecondary)
            ForEach(model.workspace.recentReviews.prefix(3)) { review in
                HStack(spacing: 8) {
                    Button {
                        model.workspace.selection = .review(review.shareID)
                        openWorkspace()
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "doc.text.magnifyingglass")
                                .foregroundStyle(ScreenscribeTheme.textSecondary)
                            Text(review.displayTitle)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer()
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if let url = model.workspace.reviewURL(for: review) {
                        Button {
                            Pasteboard.copy(url)
                        } label: {
                            Image(systemName: "link")
                        }
                        .buttonStyle(.borderless)
                        .help(L10n.string("review.copy.help"))
                        Button {
                            openURL(url)
                        } label: {
                            Image(systemName: "safari")
                        }
                        .buttonStyle(.borderless)
                        .help(L10n.string("review.open.help"))
                    }
                }
                .font(.callout)
            }
        }
    }
}
