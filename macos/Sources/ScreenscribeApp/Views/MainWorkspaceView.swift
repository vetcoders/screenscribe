import AppKit
import ScreenscribeCore
import SwiftUI
import UniformTypeIdentifiers

/// Main window: sidebar (Record · Uploads · Recent Reviews) + detail.
struct MainWorkspaceView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var app = model
        @Bindable var workspace = model.workspace

        NavigationSplitView {
            WorkspaceSidebar()
                .navigationSplitViewColumnWidth(min: 240, ideal: 280, max: 380)
        } detail: {
            WorkspaceDetail(selection: workspace.selection)
        }
        .toolbar {
            WorkspaceToolbar(model: model)
        }
        .sheet(item: $app.presentedSheet, onDismiss: { model.auth.cancelDeviceFlow() }) { sheet in
            switch sheet {
            case .signIn:
                SignInSheet()
                    .environment(model)
            case .paywall:
                PaywallSheet()
                    .environment(model)
            }
        }
        .fileImporter(
            isPresented: $workspace.isImporterPresented,
            allowedContentTypes: VideoFileValidator.supportedContentTypes,
            allowsMultipleSelection: true
        ) { result in
            switch result {
            case .success(let urls):
                Task { await model.workspace.importFiles(urls) }
            case .failure:
                break
            }
        }
        .dropDestination(for: URL.self) { urls, _ in
            let movies = urls.filter { VideoFileValidator.isSupported($0) }
            guard !movies.isEmpty else { return false }
            Task { await model.workspace.importFiles(movies) }
            return true
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            WorkspaceBanner()
        }
    }
}

// MARK: - Sidebar

struct WorkspaceSidebar: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var workspace = model.workspace

        List(selection: $workspace.selection) {
            NavigationLink(value: WorkspaceSelection.record) {
                Label(L10n.string("sidebar.record"), systemImage: "record.circle")
            }

            Section(L10n.string("sidebar.uploads")) {
                if workspace.openItems.isEmpty {
                    Text(L10n.string("sidebar.uploads.empty"))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(workspace.openItems) { item in
                        NavigationLink(value: WorkspaceSelection.upload(item.id)) {
                            UploadRow(item: item)
                        }
                        .contextMenu {
                            UploadActions(item: item)
                        }
                    }
                }
            }

            Section(L10n.string("sidebar.reviews")) {
                if workspace.recentReviews.isEmpty {
                    Text(L10n.string(model.isSignedIn ? "sidebar.reviews.empty" : "sidebar.reviews.signedout"))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(workspace.recentReviews) { review in
                        NavigationLink(value: WorkspaceSelection.review(review.shareID)) {
                            ReviewRow(review: review)
                        }
                        .contextMenu {
                            if let url = workspace.reviewURL(for: review) {
                                ShareLinkActions(url: url)
                            }
                        }
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            AccountFooter()
        }
    }
}

struct UploadRow: View {
    let item: UploadQueueItem

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: item.state.symbolName)
                .foregroundStyle(item.state.tint)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.displayName)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(subtitle)
                    .font(ScreenscribeTheme.monoCaption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 2)
    }

    private var subtitle: String {
        switch item.state {
        case .uploading:
            return L10n.format("queue.row.uploading", DisplayFormat.percent(item.uploadFraction * 100))
        case .processing:
            return "\(item.stage.localizedTitle) · \(DisplayFormat.percent(item.progressPct))"
        default:
            return item.state.localizedTitle
        }
    }
}

struct ReviewRow: View {
    let review: ReviewHistoryItem

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(review.displayTitle)
                .lineLimit(1)
                .truncationMode(.middle)
            if let createdAt = review.createdAt {
                Text(DisplayFormat.dateTime(createdAt))
                    .font(ScreenscribeTheme.monoCaption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }
}

/// Upload / Cancel / Remove — shared by the sidebar context menu and the detail pane.
struct UploadActions: View {
    let item: UploadQueueItem
    @Environment(AppModel.self) private var model

    var body: some View {
        if item.state == .billingBlocked {
            Button(L10n.string("queue.action.subscribe")) {
                model.presentPaywall(reason: nil)
            }
        }
        if item.canStartUploadNow {
            Button(L10n.string(item.state == .draft ? "queue.action.upload" : "queue.action.retry")) {
                Task { await model.workspace.upload(item.id) }
            }
        }
        if item.state == .uploading || item.state == .pending {
            Button(L10n.string("queue.action.cancel")) {
                Task { await model.workspace.cancelUpload(item.id) }
            }
        }
        Button(L10n.string("queue.action.remove"), role: .destructive) {
            Task { await model.workspace.remove(item.id) }
        }
    }
}

/// Copy Share Link + Open in Browser.
struct ShareLinkActions: View {
    let url: URL
    @Environment(\.openURL) private var openURL

    var body: some View {
        Button(L10n.string("review.copy")) {
            Pasteboard.copy(url)
        }
        Button(L10n.string("review.open")) {
            openURL(url)
        }
    }
}

enum Pasteboard {
    @MainActor
    static func copy(_ url: URL) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(url.absoluteString, forType: .string)
    }
}

struct AccountFooter: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Divider()
            if model.isSignedIn {
                let subscription = model.subscription
                HStack {
                    Text(model.profile?.email ?? L10n.string("account.signedin"))
                        .font(.callout)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    StatusBadge(
                        text: subscription.active ? (subscription.plan ?? L10n.string("account.plan.pro")) : L10n.string("account.plan.none"),
                        tint: subscription.active ? ScreenscribeTheme.success : ScreenscribeTheme.textSecondary
                    )
                }
                if subscription.active, subscription.minutesLimit > 0 {
                    ProgressView(value: subscription.usageFraction)
                        .tint(subscription.usageFraction > 0.9 ? ScreenscribeTheme.warning : .accentColor)
                    Text(L10n.format(
                        "account.minutes",
                        DisplayFormat.minutes(subscription.minutesUsed),
                        DisplayFormat.minutes(subscription.minutesLimit)
                    ))
                    .font(ScreenscribeTheme.monoCaption)
                    .foregroundStyle(.secondary)
                } else if !subscription.active {
                    Button(L10n.string("account.subscribe")) {
                        model.presentPaywall(reason: nil)
                    }
                    .controlSize(.small)
                    .screenscribePrimaryButton()
                }
            } else {
                Button(L10n.string("menu.signin")) {
                    model.presentSignIn()
                }
                .controlSize(.small)
                .screenscribePrimaryButton()
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 10)
    }
}

// MARK: - Toolbar

struct WorkspaceToolbar: ToolbarContent {
    let model: AppModel

    var body: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            Button {
                model.workspace.selection = .record
                if model.recorder.state == .idle {
                    model.recorder.startRecording()
                } else {
                    Task { await model.recorder.stopRecording() }
                }
            } label: {
                if model.recorder.state.isBusy {
                    Label(L10n.string("toolbar.stop"), systemImage: "stop.circle.fill")
                } else {
                    Label(L10n.string("toolbar.record"), systemImage: "record.circle")
                }
            }
            .help(L10n.string("toolbar.record.help"))

            Button {
                model.workspace.isImporterPresented = true
            } label: {
                Label(L10n.string("toolbar.import"), systemImage: "square.and.arrow.down")
            }
            .help(L10n.string("toolbar.import.help"))
        }

        ToolbarItem(placement: .automatic) {
            if model.isSignedIn {
                Menu {
                    if let email = model.profile?.email {
                        Text(email)
                    }
                    Button(L10n.string("menu.subscription")) {
                        model.presentPaywall(reason: nil)
                    }
                    Button(L10n.string("toolbar.reviews.refresh")) {
                        Task { await model.workspace.refreshReviews() }
                    }
                    Divider()
                    Button(L10n.string("menu.signout")) {
                        Task { await model.signOut() }
                    }
                } label: {
                    Label(L10n.string("menu.account"), systemImage: "person.crop.circle")
                }
            } else {
                Button {
                    model.presentSignIn()
                } label: {
                    Label(L10n.string("menu.signin"), systemImage: "person.crop.circle.badge.plus")
                }
            }
        }
    }
}

// MARK: - Banner

struct WorkspaceBanner: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        if let message = model.workspace.errorMessage ?? model.bannerMessage {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(ScreenscribeTheme.warning)
                Text(message)
                    .font(.callout)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                Button {
                    model.workspace.clearError()
                    model.bannerMessage = nil
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.borderless)
                .accessibilityLabel(L10n.string("common.dismiss"))
            }
            .padding(12)
            .background(.bar)
        }
    }
}

// MARK: - Detail

struct WorkspaceDetail: View {
    let selection: WorkspaceSelection?
    @Environment(AppModel.self) private var model

    var body: some View {
        switch selection {
        case .record?, nil:
            RecordPane()
        case .upload(let id)?:
            if let item = model.workspace.item(id: id) {
                if item.state == .completed, let url = model.workspace.reviewURL(for: item) {
                    ReviewWebView(url: url, title: item.displayName)
                } else {
                    PipelineProgressView(item: item)
                }
            } else {
                ContentUnavailableView(L10n.string("detail.missing"), systemImage: "questionmark.folder")
            }
        case .review(let shareID)?:
            let review = model.workspace.recentReviews.first { $0.shareID == shareID }
            ReviewWebView(
                url: model.reviewURL(shareID: shareID, shareURL: review?.shareURL),
                title: review?.displayTitle ?? shareID
            )
        }
    }
}

// MARK: - Record pane

struct RecordPane: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(L10n.string("record.title"))
                        .font(ScreenscribeTheme.monoTitle)
                    Text(L10n.string("record.subtitle"))
                        .foregroundStyle(ScreenscribeTheme.textSecondary)
                }
                PermissionBanner()
                RecorderControlCard()
                SourcePickerCard()
                ReviewOptionsCard()
            }
            .padding(28)
            .frame(maxWidth: 780, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .screenscribeCanvas()
        .navigationTitle(L10n.string("app.name"))
        .task {
            await model.recorder.refreshSources()
        }
    }
}

struct PermissionBanner: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let recorder = model.recorder
        let permissions = recorder.permissions
        if !permissions.canRecordScreen {
            VStack(alignment: .leading, spacing: 10) {
                Label(L10n.string("recorder.permission.screen.title"), systemImage: "rectangle.dashed.badge.record")
                    .font(.headline)
                Text(L10n.string("recorder.permission.screen.detail"))
                    .foregroundStyle(ScreenscribeTheme.textSecondary)
                HStack {
                    Button(L10n.string("recorder.permission.grant")) {
                        recorder.requestScreenRecordingAccess()
                    }
                    .screenscribePrimaryButton()
                    Button(L10n.string("recorder.permission.settings")) {
                        PermissionPreflight.openSettings(PermissionPreflight.screenRecordingSettingsURL)
                    }
                    .screenscribeSecondaryButton()
                    Button(L10n.string("recorder.permission.recheck")) {
                        Task { await recorder.refreshSources() }
                    }
                    .buttonStyle(.link)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .screenscribeCard()
        }
        if model.configuration.capturesMicrophone, permissions.microphone != .granted {
            HStack(spacing: 12) {
                Image(systemName: "mic.slash")
                    .foregroundStyle(ScreenscribeTheme.warning)
                Text(L10n.string(permissions.microphone == .notDetermined ? "recorder.permission.mic.ask" : "recorder.permission.mic.denied"))
                    .frame(maxWidth: .infinity, alignment: .leading)
                if permissions.microphone == .notDetermined {
                    Button(L10n.string("recorder.permission.mic.allow")) {
                        Task { await recorder.requestMicrophoneAccess() }
                    }
                    .screenscribeSecondaryButton()
                } else {
                    Button(L10n.string("recorder.permission.settings")) {
                        PermissionPreflight.openSettings(PermissionPreflight.microphoneSettingsURL)
                    }
                    .screenscribeSecondaryButton()
                }
            }
            .screenscribeCard()
        }
    }
}

struct RecorderControlCard: View {
    @Environment(AppModel.self) private var model
    @State private var confirmDiscard = false

    var body: some View {
        let recorder = model.recorder
        VStack(alignment: .leading, spacing: 14) {
            switch recorder.state {
            case .idle:
                HStack(spacing: 14) {
                    Button {
                        recorder.startRecording()
                    } label: {
                        Label(L10n.string("recorder.start"), systemImage: "record.circle.fill")
                            .frame(minWidth: 160)
                    }
                    .controlSize(.large)
                    .screenscribePrimaryButton()
                    .disabled(!recorder.canStart)
                    Text(L10n.string("recorder.start.hint"))
                        .font(ScreenscribeTheme.monoCaption)
                        .foregroundStyle(ScreenscribeTheme.textSecondary)
                }
            case .countingDown(let remaining):
                HStack(spacing: 16) {
                    Text(String(remaining))
                        .font(ScreenscribeTheme.mono(48, weight: .bold))
                        .contentTransition(.numericText(countsDown: true))
                        .monospacedDigit()
                    Text(L10n.string("recorder.countdown"))
                        .foregroundStyle(ScreenscribeTheme.textSecondary)
                    Spacer()
                    Button(L10n.string("common.cancel")) {
                        recorder.cancelCountdown()
                    }
                    .screenscribeSecondaryButton()
                }
            case .preparing:
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text(L10n.string("recorder.preparing"))
                }
            case .recording, .paused:
                HStack(spacing: 16) {
                    Circle()
                        .fill(recorder.state == .recording ? ScreenscribeTheme.recording : ScreenscribeTheme.textSecondary)
                        .frame(width: 12, height: 12)
                    Text(recorder.elapsedText)
                        .font(ScreenscribeTheme.timer)
                        .monospacedDigit()
                    if recorder.state == .paused {
                        StatusBadge(text: L10n.string("recorder.paused"), tint: ScreenscribeTheme.warning)
                    }
                    Spacer()
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
                    }
                    .screenscribePrimaryButton()
                    Button(role: .destructive) {
                        confirmDiscard = true
                    } label: {
                        Label(L10n.string("recorder.discard"), systemImage: "trash")
                    }
                    .screenscribeSecondaryButton()
                }
            case .finalizing:
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text(L10n.string("recorder.finalizing"))
                }
            }

            if let error = recorder.errorMessage {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(ScreenscribeTheme.failure)
                    .textSelection(.enabled)
            }
            if recorder.state == .idle, let last = recorder.lastRecording {
                Text(L10n.format(
                    "recorder.saved",
                    DisplayFormat.timecode(last.duration),
                    DisplayFormat.bytes(last.fileSize)
                ))
                .font(ScreenscribeTheme.monoCaption)
                .foregroundStyle(ScreenscribeTheme.textSecondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .screenscribeCard()
        .confirmationDialog(L10n.string("recorder.discard.confirm"), isPresented: $confirmDiscard) {
            Button(L10n.string("recorder.discard"), role: .destructive) {
                Task { await recorder.discardRecording() }
            }
            Button(L10n.string("common.cancel"), role: .cancel) {}
        }
    }
}

struct SourcePickerCard: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var recorder = model.recorder
        let catalog = recorder.catalog

        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(L10n.string("recorder.source"))
                    .font(.headline)
                Spacer()
                if recorder.isLoadingSources {
                    ProgressView().controlSize(.small)
                }
                Button {
                    Task { await recorder.refreshSources() }
                } label: {
                    Label(L10n.string("recorder.source.refresh"), systemImage: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
            }

            Picker(L10n.string("recorder.source"), selection: $recorder.sourceKind) {
                ForEach(CaptureSourceKind.allCases) { kind in
                    Text(kind.localizedTitle).tag(kind)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            switch recorder.sourceKind {
            case .display:
                displayPicker(selection: $recorder.selectedDisplayID, catalog: catalog)
            case .window:
                Picker(L10n.string("recorder.source.window"), selection: $recorder.selectedWindowID) {
                    Text(L10n.string("recorder.window.choose")).tag(CGWindowID?.none)
                    ForEach(catalog.windows) { window in
                        Text(verbatim: window.title.isEmpty ? window.applicationName : "\(window.applicationName) — \(window.title)")
                            .tag(CGWindowID?.some(window.id))
                    }
                }
            case .application:
                Picker(L10n.string("recorder.source.application"), selection: $recorder.selectedApplicationID) {
                    Text(L10n.string("recorder.application.choose")).tag(pid_t?.none)
                    ForEach(catalog.applications) { application in
                        Text(verbatim: application.name).tag(pid_t?.some(application.id))
                    }
                }
                displayPicker(selection: $recorder.selectedDisplayID, catalog: catalog)
            }

            Text(L10n.string("recorder.source.privacy"))
                .font(ScreenscribeTheme.monoCaption)
                .foregroundStyle(ScreenscribeTheme.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .screenscribeCard()
    }

    private func displayPicker(selection: Binding<CGDirectDisplayID?>, catalog: CaptureSourceCatalog) -> some View {
        Picker(L10n.string("recorder.source.display"), selection: selection) {
            ForEach(catalog.displays) { display in
                Text(verbatim: "\(display.name) — \(display.width)×\(display.height)")
                    .tag(CGDirectDisplayID?.some(display.id))
            }
        }
    }
}

struct ReviewOptionsCard: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let configuration = model.configuration
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("options.title"))
                .font(.headline)

            Picker(L10n.string("options.preset"), selection: model.configurationBinding(\.defaultPreset)) {
                ForEach(ReviewPreset.allCases) { preset in
                    Text(preset.localizedTitle).tag(preset)
                }
            }
            Text(configuration.defaultPreset.localizedDetail)
                .font(.callout)
                .foregroundStyle(ScreenscribeTheme.textSecondary)

            Picker(L10n.string("options.language"), selection: model.configurationBinding(\.defaultLanguage)) {
                ForEach(RecordingLanguage.allCases) { language in
                    Text(language.localizedTitle).tag(language)
                }
            }
            Picker(L10n.string("options.visibility"), selection: model.configurationBinding(\.defaultVisibility)) {
                ForEach(ReviewVisibility.allCases) { visibility in
                    Text(visibility.localizedTitle).tag(visibility)
                }
            }
            TextField(
                L10n.string("options.prompt"),
                text: model.configurationBinding(\.additionalPrompt),
                prompt: Text(L10n.string("options.prompt.placeholder")),
                axis: .vertical
            )
            .lineLimit(2...5)

            Divider()

            Toggle(L10n.string("options.audio.system"), isOn: model.configurationBinding(\.capturesSystemAudio))
            Toggle(L10n.string("options.audio.microphone"), isOn: model.configurationBinding(\.capturesMicrophone))
            if configuration.capturesMicrophone {
                Picker(L10n.string("options.audio.device"), selection: model.configurationBinding(\.microphoneDeviceID)) {
                    Text(L10n.string("options.audio.device.default")).tag(String?.none)
                    ForEach(model.recorder.microphones) { microphone in
                        Text(verbatim: microphone.name).tag(String?.some(microphone.id))
                    }
                }
            }
            Toggle(L10n.string("options.autoupload"), isOn: model.configurationBinding(\.autoUploadRecordings))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .screenscribeCard()
    }
}
