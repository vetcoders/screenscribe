import ScreenscribeCore
import SwiftUI

/// Progress of one upload through the hosted pipeline:
/// upload → audio → transcribe → detect → screenshots → analyze → report.
struct PipelineProgressView: View {
    let item: UploadQueueItem
    @Environment(AppModel.self) private var model

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header
                uploadCard
                stagesCard
                if let message = item.lastError {
                    messageCard(message)
                }
                actions
            }
            .padding(28)
            .frame(maxWidth: 780, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .screenscribeCanvas()
        .navigationTitle(item.displayName)
    }

    // MARK: Sections

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Text(item.displayName)
                    .font(ScreenscribeTheme.monoTitle)
                    .lineLimit(1)
                    .truncationMode(.middle)
                StatusBadge(text: item.state.localizedTitle, tint: item.state.tint)
            }
            HStack(spacing: 14) {
                Label(item.preset.localizedTitle, systemImage: "slider.horizontal.3")
                Label(item.language.localizedTitle, systemImage: "globe")
                Label(item.visibility.localizedTitle, systemImage: "eye")
                if item.totalBytes > 0 {
                    Label(DisplayFormat.bytes(item.totalBytes), systemImage: "film")
                }
            }
            .font(ScreenscribeTheme.monoCaption)
            .foregroundStyle(ScreenscribeTheme.textSecondary)
        }
    }

    private var uploadCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label(L10n.string("pipeline.upload"), systemImage: uploadSymbol)
                    .foregroundStyle(uploadTint)
                Spacer()
                Text(uploadDetail)
                    .font(ScreenscribeTheme.monoCaption)
                    .foregroundStyle(ScreenscribeTheme.textSecondary)
            }
            if item.state == .uploading {
                ProgressView(value: item.uploadFraction)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .screenscribeCard()
    }

    private var stagesCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(L10n.string("pipeline.title"))
                    .font(.headline)
                Spacer()
                if item.state == .processing || item.state == .completed {
                    Text(DisplayFormat.percent(item.progressPct))
                        .font(ScreenscribeTheme.monoBody)
                        .monospacedDigit()
                }
            }
            if item.state == .processing {
                ProgressView(value: min(1, max(0, item.progressPct / 100)))
            }
            ForEach(Array(JobStage.pipeline.enumerated()), id: \.offset) { index, stage in
                PipelineStageRow(title: stage.localizedTitle, status: status(forStageAt: index))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .screenscribeCard()
    }

    private func messageCard(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: item.state == .failed ? "exclamationmark.triangle.fill" : "info.circle")
                .foregroundStyle(item.state == .failed ? ScreenscribeTheme.failure : ScreenscribeTheme.warning)
            Text(message)
                .font(.callout)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let retryAt = item.nextAttemptAt, item.state == .pending {
                Text(L10n.format("pipeline.retry.at", retryAt.formatted(date: .omitted, time: .standard)))
                    .font(ScreenscribeTheme.monoCaption)
                    .foregroundStyle(ScreenscribeTheme.textSecondary)
            }
        }
        .screenscribeCard()
    }

    private var actions: some View {
        HStack(spacing: 10) {
            if item.state == .billingBlocked {
                Button(L10n.string("queue.action.subscribe")) {
                    model.presentPaywall(reason: nil)
                }
                .screenscribePrimaryButton()
            }
            if item.canStartUploadNow {
                Button(L10n.string(item.state == .draft ? "queue.action.upload" : "queue.action.retry")) {
                    Task { await model.workspace.upload(item.id) }
                }
                .screenscribePrimaryButton()
            }
            if item.state == .uploading || item.state == .pending {
                Button(L10n.string("queue.action.cancel")) {
                    Task { await model.workspace.cancelUpload(item.id) }
                }
                .screenscribeSecondaryButton()
            }
            Spacer()
            Button(L10n.string("queue.action.remove"), role: .destructive) {
                Task { await model.workspace.remove(item.id) }
            }
            .screenscribeSecondaryButton()
        }
    }

    // MARK: Derivations

    private var uploadSymbol: String {
        switch item.state {
        case .uploading: return "arrow.up.circle"
        case .processing, .completed: return "checkmark.circle.fill"
        case .failed where item.jobID == nil: return "exclamationmark.triangle.fill"
        default: return "circle.dashed"
        }
    }

    private var uploadTint: Color {
        switch item.state {
        case .processing, .completed: return ScreenscribeTheme.success
        case .uploading: return .accentColor
        case .failed where item.jobID == nil: return ScreenscribeTheme.failure
        default: return ScreenscribeTheme.textSecondary
        }
    }

    private var uploadDetail: String {
        switch item.state {
        case .uploading:
            return L10n.format(
                "pipeline.upload.bytes",
                DisplayFormat.bytes(item.uploadedBytes),
                DisplayFormat.bytes(item.totalBytes)
            )
        case .processing, .completed:
            return L10n.string("pipeline.upload.done")
        case .draft:
            return L10n.string("pipeline.upload.draft")
        case .pending:
            return isSignedIn ? L10n.string("pipeline.upload.waiting") : L10n.string("pipeline.upload.signin")
        case .billingBlocked:
            return L10n.string("queue.state.billing")
        case .failed:
            return item.jobID == nil ? L10n.string("pipeline.upload.failed") : L10n.string("pipeline.upload.done")
        }
    }

    private var isSignedIn: Bool {
        model.isSignedIn
    }

    private func status(forStageAt index: Int) -> PipelineStageRow.Status {
        switch item.state {
        case .completed:
            return .done
        case .processing:
            let current = item.stage.pipelineIndex
            if index < current { return .done }
            if index == current { return .running }
            return .waiting
        case .failed where item.jobID != nil:
            let current = max(0, item.stage.pipelineIndex)
            if index < current { return .done }
            if index == current { return .failed }
            return .waiting
        default:
            return .waiting
        }
    }
}

struct PipelineStageRow: View {
    enum Status {
        case waiting
        case running
        case done
        case failed
    }

    let title: String
    let status: Status

    var body: some View {
        HStack(spacing: 12) {
            indicator
                .frame(width: 18, height: 18)
            Text(title)
                .font(ScreenscribeTheme.monoBody)
                .foregroundStyle(status == .waiting ? ScreenscribeTheme.textSecondary : ScreenscribeTheme.textPrimary)
            Spacer()
        }
        .accessibilityElement(children: .combine)
        .accessibilityValue(Text(accessibilityStatus))
    }

    @ViewBuilder
    private var indicator: some View {
        switch status {
        case .waiting:
            Image(systemName: "circle")
                .foregroundStyle(ScreenscribeTheme.textSecondary)
        case .running:
            ProgressView()
                .controlSize(.small)
        case .done:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(ScreenscribeTheme.success)
        case .failed:
            Image(systemName: "xmark.octagon.fill")
                .foregroundStyle(ScreenscribeTheme.failure)
        }
    }

    private var accessibilityStatus: String {
        switch status {
        case .waiting: return L10n.string("pipeline.status.waiting")
        case .running: return L10n.string("pipeline.status.running")
        case .done: return L10n.string("pipeline.status.done")
        case .failed: return L10n.string("pipeline.status.failed")
        }
    }
}
