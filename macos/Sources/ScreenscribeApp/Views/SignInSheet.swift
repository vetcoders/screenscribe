import AppKit
import AuthenticationServices
import ScreenscribeCore
import SwiftUI

/// Sign in to Screenscribe Cloud: Sign in with Apple, email + password,
/// or the browser device flow (RFC 8628) for accounts created on the web.
struct SignInSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL
    @Environment(\.colorScheme) private var colorScheme
    @FocusState private var focusedField: Field?
    @State private var codeCopied = false

    private enum Field: Hashable {
        case email
        case credential
    }

    var body: some View {
        @Bindable var auth = model.auth

        VStack(alignment: .leading, spacing: 18) {
            header

            if let notice = auth.notice {
                Label(notice, systemImage: "info.circle")
                    .font(.callout)
                    .foregroundStyle(ScreenscribeTheme.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let code = auth.deviceCode {
                deviceFlowPanel(code)
            } else {
                appleButton
                separator
                emailForm(auth: $auth)
                deviceFlowEntry
            }

            if let error = auth.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(ScreenscribeTheme.failure)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }

            footer
        }
        .padding(28)
        .frame(width: 440)
        .onAppear {
            focusedField = .email
        }
        .onChange(of: auth.deviceCode) { _, newCode in
            // RFC 8628 §3.3: send the user to the verification page right away.
            if let newCode, let url = verificationURL(for: newCode) {
                openURL(url)
            }
        }
        .task(id: codeCopied) {
            guard codeCopied else { return }
            try? await Task.sleep(for: .seconds(2))
            codeCopied = false
        }
    }

    // MARK: Sections

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(L10n.string("signin.title"))
                .font(ScreenscribeTheme.monoTitle)
            Text(L10n.string("signin.subtitle"))
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var appleButton: some View {
        SignInWithAppleButton(model.auth.mode == .register ? .signUp : .signIn) { request in
            request.requestedScopes = AuthViewModel.appleScopes
        } onCompletion: { result in
            model.auth.handleAppleAuthorization(result)
        }
        .signInWithAppleButtonStyle(colorScheme == .dark ? .white : .black)
        .frame(height: 36)
        .disabled(model.auth.isBusy)
        .accessibilityLabel(Text(L10n.string("signin.apple")))
    }

    private var separator: some View {
        HStack(spacing: 10) {
            Rectangle().fill(.separator).frame(height: 1)
            Text(L10n.string("signin.or"))
                .font(ScreenscribeTheme.monoCaption)
                .foregroundStyle(.secondary)
            Rectangle().fill(.separator).frame(height: 1)
        }
    }

    private func emailForm(auth: Bindable<AuthViewModel>) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker(L10n.string("signin.mode"), selection: auth.mode) {
                ForEach(AuthViewModel.Mode.allCases) { mode in
                    Text(mode.localizedTitle).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            TextField(L10n.string("signin.email"), text: auth.email, prompt: Text(L10n.string("signin.email.prompt")))
                .textContentType(.username)
                .focused($focusedField, equals: .email)
                .onSubmit { focusedField = .credential }

            SecureField(L10n.string("signin.credential"), text: auth.credential)
                .textContentType(model.auth.mode == .register ? .newPassword : .password)
                .focused($focusedField, equals: .credential)
                .onSubmit { model.auth.submitEmail() }

            if model.auth.mode == .register {
                Text(L10n.format("signin.credential.hint", AuthViewModel.minimumCredentialLength))
                    .font(ScreenscribeTheme.monoCaption)
                    .foregroundStyle(.secondary)
            }

            HStack {
                if model.auth.isBusy {
                    ProgressView().controlSize(.small)
                }
                Spacer()
                Button {
                    model.auth.submitEmail()
                } label: {
                    Text(L10n.string(model.auth.mode == .register ? "signin.submit.register" : "signin.submit.signin"))
                        .frame(minWidth: 120)
                }
                .keyboardShortcut(.defaultAction)
                .screenscribePrimaryButton()
                .disabled(!model.auth.canSubmitEmail)
            }
        }
        .textFieldStyle(.roundedBorder)
    }

    private var deviceFlowEntry: some View {
        VStack(alignment: .leading, spacing: 6) {
            Divider()
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(L10n.string("signin.device.title"))
                        .font(.callout.weight(.medium))
                    Text(L10n.string("signin.device.detail"))
                        .font(ScreenscribeTheme.monoCaption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Button(L10n.string("signin.device.start")) {
                    model.auth.startDeviceFlow()
                }
                .screenscribeSecondaryButton()
                .disabled(model.auth.isBusy)
            }
        }
    }

    private func deviceFlowPanel(_ code: DeviceCodeResponse) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(L10n.string("signin.device.instructions"))
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 12) {
                Text(code.userCode)
                    .font(ScreenscribeTheme.mono(30, weight: .semibold))
                    .textSelection(.enabled)
                    .accessibilityLabel(Text(L10n.format("signin.device.code.a11y", code.userCode)))
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code.userCode, forType: .string)
                    codeCopied = true
                } label: {
                    Label(
                        L10n.string(codeCopied ? "signin.device.copied" : "signin.device.copy"),
                        systemImage: codeCopied ? "checkmark" : "doc.on.doc"
                    )
                }
                .screenscribeSecondaryButton()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .screenscribeCard()

            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                if let expiresAt = model.auth.deviceExpiresAt {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Text(L10n.format(
                            "signin.device.waiting",
                            DisplayFormat.timecode(max(0, expiresAt.timeIntervalSince(context.date)))
                        ))
                        .font(ScreenscribeTheme.monoCaption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                    }
                }
                Spacer()
                if let url = verificationURL(for: code) {
                    Button(L10n.string("signin.device.open")) {
                        openURL(url)
                    }
                    .screenscribePrimaryButton()
                }
                Button(L10n.string("common.cancel")) {
                    model.auth.cancelDeviceFlow()
                }
                .screenscribeSecondaryButton()
            }
        }
    }

    private var footer: some View {
        HStack(alignment: .firstTextBaseline) {
            Link(L10n.string("legal.privacy"), destination: AppConfiguration.privacyPolicyURL)
                .font(ScreenscribeTheme.monoCaption)
            Text(model.environment.host)
                .font(ScreenscribeTheme.monoCaption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .help(L10n.string("signin.server.help"))
            Spacer()
            Button(L10n.string("common.close")) {
                model.dismissSheet()
            }
            .keyboardShortcut(.cancelAction)
        }
    }

    // MARK: Helpers

    /// Verification page from the device code response. Only `https` (or
    /// plain `http` on loopback for local development) is ever opened.
    private func verificationURL(for code: DeviceCodeResponse) -> URL? {
        let reference = code.verificationURIComplete ?? code.verificationURI
        guard let url = model.environment.resolve(reference),
              let scheme = url.scheme?.lowercased(),
              let host = url.host()?.lowercased()
        else {
            return nil
        }
        if scheme == "https" { return url }
        if scheme == "http", CloudEnvironment.loopbackHosts.contains(host) { return url }
        return nil
    }
}
