import AppKit
import ScreenscribeCore
import SwiftUI
import WebKit

/// The hosted review (`/r/{share_id}`) rendered in a `WKWebView`, with
/// Copy Share Link and Open in Browser.
///
/// Security: the Bearer is attached by `ScreenscribeCloudClient.shareRequest`
/// only for the configured origin; main-frame navigation to any host other
/// than that origin or the share link's own origin opens in the default
/// browser instead of inside the app.
struct ReviewWebView: View {
    let url: URL
    let title: String

    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL
    @State private var request: URLRequest?
    @State private var isLoading = false
    @State private var loadError: String?
    @State private var reloadToken = 0
    @State private var copied = false

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                if let request {
                    ReviewWebContainer(
                        request: request,
                        environment: model.environment,
                        reloadToken: reloadToken,
                        isLoading: $isLoading,
                        loadError: $loadError
                    )
                }
                if let loadError {
                    ContentUnavailableView {
                        Label(L10n.string("review.error.title"), systemImage: "wifi.exclamationmark")
                    } description: {
                        Text(loadError)
                    } actions: {
                        Button(L10n.string("review.reload")) {
                            self.loadError = nil
                            reloadToken += 1
                        }
                        .screenscribePrimaryButton()
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .screenscribeCanvas()
                } else if isLoading || request == nil {
                    ProgressView()
                        .controlSize(.large)
                }
            }
        }
        .navigationTitle(title)
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Button {
                    Pasteboard.copy(url)
                    copied = true
                } label: {
                    Label(
                        L10n.string(copied ? "review.copied" : "review.copy"),
                        systemImage: copied ? "checkmark" : "link"
                    )
                }
                .help(L10n.string("review.copy.help"))

                Button {
                    openURL(url)
                } label: {
                    Label(L10n.string("review.open"), systemImage: "safari")
                }
                .help(L10n.string("review.open.help"))

                Button {
                    loadError = nil
                    reloadToken += 1
                } label: {
                    Label(L10n.string("review.reload"), systemImage: "arrow.clockwise")
                }
            }
        }
        .task(id: url) {
            copied = false
            loadError = nil
            request = await model.client.shareRequest(for: url)
        }
        .task(id: copied) {
            guard copied else { return }
            try? await Task.sleep(for: .seconds(2))
            copied = false
        }
    }
}

/// `WKWebView` bridge. A non-persistent data store keeps no cookies or
/// cache after the view goes away (nothing survives sign-out).
struct ReviewWebContainer: NSViewRepresentable {
    let request: URLRequest
    let environment: CloudEnvironment
    let reloadToken: Int
    @Binding var isLoading: Bool
    @Binding var loadError: String?

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.underPageBackgroundColor = NSColor(red: 0x0A / 255, green: 0x0B / 255, blue: 0x0D / 255, alpha: 1)
        context.coordinator.load(request, in: webView, token: reloadToken)
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        context.coordinator.parent = self
        if context.coordinator.loadedURL != request.url || context.coordinator.loadedToken != reloadToken {
            context.coordinator.load(request, in: webView, token: reloadToken)
        }
    }

    static func dismantleNSView(_ webView: WKWebView, coordinator: Coordinator) {
        webView.stopLoading()
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var parent: ReviewWebContainer
        var loadedURL: URL?
        var loadedToken = 0

        init(parent: ReviewWebContainer) {
            self.parent = parent
        }

        /// Called from `makeNSView` / `updateNSView`: it must not write SwiftUI
        /// state (the owning view clears `loadError` before it changes inputs).
        func load(_ request: URLRequest, in webView: WKWebView, token: Int) {
            loadedURL = request.url
            loadedToken = token
            webView.load(request)
        }

        /// Pages stay in the app on the Band 2 origin and on the origin of
        /// the share link itself (the server may host `share_url` on its own
        /// host). The Bearer still only goes to the Band 2 origin.
        func staysInApp(_ url: URL) -> Bool {
            if parent.environment.isSameOrigin(url) {
                return true
            }
            guard let loadedURL else { return false }
            return Self.origin(of: url) != nil && Self.origin(of: url) == Self.origin(of: loadedURL)
        }

        static func origin(of url: URL) -> String? {
            guard let scheme = url.scheme?.lowercased(), scheme == "https" || scheme == "http",
                  let host = url.host(percentEncoded: false)?.lowercased(), !host.isEmpty
            else {
                return nil
            }
            let port = url.port ?? (scheme == "https" ? 443 : 80)
            return "\(scheme)://\(host):\(port)"
        }

        // MARK: Navigation policy

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction) async -> WKNavigationActionPolicy {
            guard let url = navigationAction.request.url else { return .cancel }
            let scheme = url.scheme?.lowercased() ?? ""
            if ["about", "blob", "data"].contains(scheme) {
                return .allow
            }
            // Sub-frames and pages on the review's own origin stay inside the review.
            if navigationAction.targetFrame?.isMainFrame == false || staysInApp(url) {
                return .allow
            }
            // Everything else (docs links, mailto:, third-party hosts) goes to the browser.
            if navigationAction.navigationType == .linkActivated || scheme == "mailto" || scheme == "http" || scheme == "https" {
                NSWorkspace.shared.open(url)
            }
            return .cancel
        }

        /// `target="_blank"` / `window.open`: open externally, never a popup.
        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let url = navigationAction.request.url {
                if staysInApp(url) {
                    webView.load(URLRequest(url: url))
                } else {
                    NSWorkspace.shared.open(url)
                }
            }
            return nil
        }

        // MARK: Loading state

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            parent.isLoading = true
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            parent.isLoading = false
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: any Error) {
            finish(with: error)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: any Error) {
            finish(with: error)
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationResponse: WKNavigationResponse
        ) async -> WKNavigationResponsePolicy {
            if navigationResponse.isForMainFrame, let http = navigationResponse.response as? HTTPURLResponse {
                switch http.statusCode {
                case 401, 403:
                    parent.loadError = L10n.string("review.error.private")
                    parent.isLoading = false
                    return .cancel
                case 404:
                    parent.loadError = L10n.string("review.error.missing")
                    parent.isLoading = false
                    return .cancel
                default:
                    break
                }
            }
            return .allow
        }

        private func finish(with error: any Error) {
            parent.isLoading = false
            let nsError = error as NSError
            // Cancelled loads (our own policy decisions, reloads) are not errors.
            if nsError.domain == NSURLErrorDomain, nsError.code == NSURLErrorCancelled {
                return
            }
            if nsError.domain == "WebKitErrorDomain", nsError.code == 102 {
                // Frame load interrupted by a policy change (external link).
                return
            }
            parent.loadError = error.localizedDescription
        }
    }
}
