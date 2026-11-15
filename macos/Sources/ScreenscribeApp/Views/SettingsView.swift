import AppKit
import ScreenscribeCore
import SwiftUI

extension AppModel {
    /// Binding to one recording/review default. Built from key paths on
    /// `editableConfiguration`, so every write is clamped and persisted by
    /// `updateConfiguration` and never touches the Cloud URL.
    @MainActor
    func configurationBinding<Value>(_ keyPath: WritableKeyPath<AppConfiguration, Value>) -> Binding<Value> {
        let root = Bindable(self).editableConfiguration
        return root[dynamicMember: keyPath]
    }
}

/// Settings window (⌘,).
///
/// Screenscribe Cloud does every model call on the server, so there are no
/// provider API key fields here — only the Cloud URL, the account, and
/// recording defaults.
struct SettingsView: View {
    private enum Tab: Hashable {
        case general
        case recording
        case account
        case cloud
    }

    @State private var selection: Tab = .general

    var body: some View {
        TabView(selection: $selection) {
            GeneralSettingsPane()
                .tabItem { Label(L10n.string("settings.tab.general"), systemImage: "gearshape") }
                .tag(Tab.general)
            RecordingSettingsPane()
                .tabItem { Label(L10n.string("settings.tab.recording"), systemImage: "record.circle") }
                .tag(Tab.recording)
            AccountSettingsPane()
                .tabItem { Label(L10n.string("settings.tab.account"), systemImage: "person.crop.circle") }
                .tag(Tab.account)
            CloudSettingsPane()
                .tabItem { Label(L10n.string("settings.tab.cloud"), systemImage: "cloud") }
                .tag(Tab.cloud)
        }
        .frame(width: 560)
        .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - General

struct GeneralSettingsPane: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Form {
            Section {
                Picker(L10n.string("options.preset"), selection: model.configurationBinding(\.defaultPreset)) {
                    ForEach(ReviewPreset.allCases) { preset in
                        Text(preset.localizedTitle).tag(preset)
                    }
                }
                Text(model.configuration.defaultPreset.localizedDetail)
                    .font(.callout)
                    .foregroundStyle(.secondary)
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
                .lineLimit(2...6)
            } header: {
                Text(L10n.string("settings.section.review"))
            }

            Section {
                Toggle(L10n.string("options.autoupload"), isOn: model.configurationBinding(\.autoUploadRecordings))
                Text(L10n.string("settings.autoupload.detail"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } header: {
                Text(L10n.string("settings.section.upload"))
            }

            Section {
                Text(L10n.string("settings.privacy.note"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    Link(L10n.string("legal.privacy"), destination: AppConfiguration.privacyPolicyURL)
                    Spacer()
                    Button(L10n.string("settings.reset")) {
                        model.resetConfiguration()
                    }
                }
            } header: {
                Text(L10n.string("settings.section.privacy"))
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - Recording

struct RecordingSettingsPane: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let configuration = model.configuration
        Form {
            Section {
                Picker(L10n.string("settings.codec"), selection: model.configurationBinding(\.videoCodec)) {
                    ForEach(VideoCodecChoice.allCases) { codec in
                        Text(codec.localizedTitle).tag(codec)
                    }
                }
                Picker(L10n.string("settings.container"), selection: model.configurationBinding(\.container)) {
                    ForEach(RecordingContainer.allCases) { container in
                        Text(container.localizedTitle).tag(container)
                    }
                }
                Picker(L10n.string("settings.fps"), selection: model.configurationBinding(\.framesPerSecond)) {
                    ForEach(AppConfiguration.allowedFrameRates, id: \.self) { fps in
                        Text(L10n.format("settings.fps.value", fps)).tag(fps)
                    }
                }
                Picker(L10n.string("settings.countdown"), selection: model.configurationBinding(\.countdownSeconds)) {
                    ForEach(AppConfiguration.allowedCountdowns, id: \.self) { seconds in
                        Text(seconds == 0 ? L10n.string("settings.countdown.off") : L10n.format("settings.countdown.value", seconds))
                            .tag(seconds)
                    }
                }
                Toggle(L10n.string("settings.cursor"), isOn: model.configurationBinding(\.showsCursor))
            } header: {
                Text(L10n.string("settings.section.video"))
            } footer: {
                Text(L10n.string("settings.codec.detail"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section {
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
            } header: {
                Text(L10n.string("settings.section.audio"))
            }

            Section {
                PermissionRow(
                    title: L10n.string("permission.screen"),
                    state: model.recorder.permissions.screenRecording,
                    settingsURL: PermissionPreflight.screenRecordingSettingsURL
                ) {
                    model.recorder.requestScreenRecordingAccess()
                }
                PermissionRow(
                    title: L10n.string("permission.microphone"),
                    state: model.recorder.permissions.microphone,
                    settingsURL: PermissionPreflight.microphoneSettingsURL
                ) {
                    Task { await model.recorder.requestMicrophoneAccess() }
                }
            } header: {
                Text(L10n.string("settings.section.permissions"))
            }
        }
        .formStyle(.grouped)
        .onAppear {
            model.recorder.refreshPermissions()
        }
    }
}

struct PermissionRow: View {
    let title: String
    let state: PermissionState
    let settingsURL: URL
    let request: () -> Void

    var body: some View {
        HStack {
            Text(title)
            Spacer()
            StatusBadge(text: L10n.string(stateKey), tint: state.isGranted ? ScreenscribeTheme.success : ScreenscribeTheme.warning)
            if !state.isGranted {
                if state != .restricted {
                    Button(L10n.string("permission.request"), action: request)
                }
                Button(L10n.string("permission.open")) {
                    PermissionPreflight.openSettings(settingsURL)
                }
            }
        }
    }

    private var stateKey: String {
        switch state {
        case .granted: return "permission.state.granted"
        case .denied: return "permission.state.denied"
        case .notDetermined: return "permission.state.undetermined"
        case .restricted: return "permission.state.restricted"
        }
    }
}

// MARK: - Account

struct AccountSettingsPane: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL
    @Environment(\.openWindow) private var openWindow
    @State private var confirmSignOut = false

    var body: some View {
        Form {
            if model.isSignedIn {
                let subscription = model.subscription
                Section {
                    LabeledContent(L10n.string("settings.account.email")) {
                        Text(verbatim: model.profile?.email ?? "—")
                            .textSelection(.enabled)
                    }
                    LabeledContent(L10n.string("settings.account.plan")) {
                        StatusBadge(
                            text: subscription.active ? (subscription.plan ?? L10n.string("account.plan.pro")) : L10n.string("account.plan.none"),
                            tint: subscription.active ? ScreenscribeTheme.success : ScreenscribeTheme.textSecondary
                        )
                    }
                    if let source = subscription.source {
                        LabeledContent(L10n.string("settings.account.source"), value: source.localizedTitle)
                    }
                    if let periodEnd = subscription.periodEnd {
                        LabeledContent(L10n.string("settings.account.renews"), value: DisplayFormat.dateTime(periodEnd))
                    }
                    if subscription.minutesLimit > 0 {
                        LabeledContent(L10n.string("settings.account.usage")) {
                            Text(L10n.format(
                                "account.minutes",
                                DisplayFormat.minutes(subscription.minutesUsed),
                                DisplayFormat.minutes(subscription.minutesLimit)
                            ))
                            .monospacedDigit()
                        }
                    }
                } header: {
                    Text(L10n.string("settings.section.account"))
                }

                Section {
                    HStack {
                        Button(L10n.string(subscription.active ? "menu.subscription" : "account.subscribe")) {
                            openWindow(id: ScreenscribeMacApp.workspaceWindowID)
                            model.presentPaywall(reason: nil)
                        }
                        if subscription.source == .appStore {
                            Link(L10n.string("paywall.manage"), destination: AppConfiguration.manageSubscriptionsURL)
                        }
                        Spacer()
                        Button(L10n.string("settings.account.refresh")) {
                            Task { await model.refreshProfile() }
                        }
                    }
                } header: {
                    Text(L10n.string("settings.section.subscription"))
                }

                Section {
                    Text(L10n.string("settings.account.manage.detail"))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack {
                        Button(L10n.string("settings.account.manage")) {
                            openURL(model.accountManagementURL)
                        }
                        Button(L10n.string("settings.account.delete"), role: .destructive) {
                            openURL(model.accountManagementURL)
                        }
                        Spacer()
                        Button(L10n.string("menu.signout")) {
                            confirmSignOut = true
                        }
                    }
                } header: {
                    Text(L10n.string("settings.section.data"))
                }
            } else {
                Section {
                    Text(L10n.string("settings.account.signedout"))
                        .foregroundStyle(.secondary)
                    Button(L10n.string("menu.signin")) {
                        openWindow(id: ScreenscribeMacApp.workspaceWindowID)
                        model.presentSignIn()
                    }
                } header: {
                    Text(L10n.string("settings.section.account"))
                }
            }
        }
        .formStyle(.grouped)
        .confirmationDialog(L10n.string("settings.account.signout.confirm"), isPresented: $confirmSignOut) {
            Button(L10n.string("menu.signout"), role: .destructive) {
                Task { await model.signOut() }
            }
            Button(L10n.string("common.cancel"), role: .cancel) {}
        }
    }
}

// MARK: - Cloud

/// Screenscribe Cloud origin. HTTPS everywhere; plain HTTP only on
/// loopback (`127.0.0.1`, `localhost`) for local Band 2 development.
struct CloudSettingsPane: View {
    @Environment(AppModel.self) private var model
    @State private var draft = ""
    @State private var errorMessage: String?
    @State private var statusMessage: String?
    @State private var isApplying = false
    @State private var confirmSwitch = false

    var body: some View {
        Form {
            Section {
                TextField(
                    L10n.string("settings.cloud.url"),
                    text: $draft,
                    prompt: Text(verbatim: AppConfiguration.defaultCloudBaseURL)
                )
                .autocorrectionDisabled()
                .onSubmit { requestApply() }

                LabeledContent(L10n.string("settings.cloud.current")) {
                    HStack(spacing: 6) {
                        Text(verbatim: model.environment.displayString)
                            .font(ScreenscribeTheme.monoCaption)
                            .textSelection(.enabled)
                        if model.environment.isLoopback {
                            StatusBadge(text: L10n.string("settings.cloud.local"), tint: ScreenscribeTheme.warning)
                        }
                    }
                }

                if let errorMessage {
                    Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(ScreenscribeTheme.failure)
                        .font(.callout)
                }
                if let statusMessage {
                    Label(statusMessage, systemImage: "checkmark.circle")
                        .foregroundStyle(ScreenscribeTheme.success)
                        .font(.callout)
                }

                HStack {
                    Button(L10n.string("settings.cloud.default")) {
                        draft = AppConfiguration.defaultCloudBaseURL
                        requestApply()
                    }
                    .disabled(isApplying || model.environment == CloudEnvironment.production)
                    Spacer()
                    if isApplying {
                        ProgressView().controlSize(.small)
                    }
                    Button(L10n.string("settings.cloud.apply")) { requestApply() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(isApplying || !hasChanges)
                }
            } header: {
                Text(L10n.string("settings.section.cloud"))
            } footer: {
                Text(L10n.string("settings.cloud.hint"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .formStyle(.grouped)
        .onAppear {
            draft = model.environment.displayString
        }
        .onChange(of: draft) {
            errorMessage = nil
            statusMessage = nil
        }
        .confirmationDialog(L10n.string("settings.cloud.confirm"), isPresented: $confirmSwitch) {
            Button(L10n.string("settings.cloud.confirm.action"), role: .destructive) {
                apply()
            }
            Button(L10n.string("common.cancel"), role: .cancel) {}
        } message: {
            Text(L10n.string("settings.cloud.confirm.detail"))
        }
    }

    private var hasChanges: Bool {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        return !trimmed.isEmpty && trimmed != model.environment.displayString
    }

    /// Tokens are bound to one origin: switching while signed in signs out,
    /// so ask first.
    private func requestApply() {
        errorMessage = nil
        do {
            let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
            let candidate = try CloudEnvironment(string: trimmed.isEmpty ? AppConfiguration.defaultCloudBaseURL : trimmed)
            guard candidate != model.environment else {
                draft = candidate.displayString
                return
            }
            if model.isSignedIn {
                confirmSwitch = true
            } else {
                apply()
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func apply() {
        isApplying = true
        let value = draft
        Task {
            defer { isApplying = false }
            do {
                let applied = try await model.applyCloudURL(value)
                draft = applied.displayString
                statusMessage = L10n.format("settings.cloud.applied", applied.host)
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
