import Foundation

/// Why a configured Cloud URL was rejected.
public enum CloudEnvironmentError: Error, Equatable, Sendable, LocalizedError {
    case malformedURL
    /// Plain `http` is only accepted for loopback development hosts.
    case insecureScheme
    case unsupportedScheme(String)
    case missingHost
    /// URLs carrying embedded userinfo before the host are refused outright.
    case embeddedCredentials

    public var errorDescription: String? {
        switch self {
        case .malformedURL: return L10n.string("settings.cloud.error.malformed")
        case .insecureScheme: return L10n.string("settings.cloud.error.insecure")
        case .unsupportedScheme: return L10n.string("settings.cloud.error.scheme")
        case .missingHost: return L10n.string("settings.cloud.error.host")
        case .embeddedCredentials: return L10n.string("settings.cloud.error.credentials")
        }
    }
}

/// A validated, normalized Screenscribe Cloud origin.
///
/// HTTPS is required for every remote host. Plain HTTP is accepted only for
/// loopback (`127.0.0.1`, `localhost`, `::1`) so a developer can point the app at
/// a local Band 2 server; App Transport Security allows exactly that through
/// `NSAllowsLocalNetworking`.
public struct CloudEnvironment: Sendable, Equatable, Hashable {
    public let baseURL: URL

    public static let loopbackHosts: Set<String> = ["127.0.0.1", "localhost", "::1"]

    public init(baseURL: URL) throws {
        self.baseURL = try CloudEnvironment.normalized(baseURL)
    }

    public init(string: String) throws {
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let url = URL(string: trimmed) else {
            throw CloudEnvironmentError.malformedURL
        }
        try self.init(baseURL: url)
    }

    /// The hosted production origin (`AppConfiguration.defaultCloudBaseURL`).
    public static let production: CloudEnvironment = {
        guard let environment = try? CloudEnvironment(string: AppConfiguration.defaultCloudBaseURL) else {
            preconditionFailure("AppConfiguration.defaultCloudBaseURL must be a valid HTTPS origin")
        }
        return environment
    }()

    /// Parses a user-entered Cloud URL, falling back to production when it is
    /// empty or invalid (settings validate separately and surface the error).
    public static func resolved(from string: String) -> CloudEnvironment {
        (try? CloudEnvironment(string: string)) ?? .production
    }

    public var host: String {
        CloudEnvironment.bareHost(of: baseURL) ?? ""
    }

    public var isLoopback: Bool {
        CloudEnvironment.loopbackHosts.contains(host)
    }

    public var displayString: String {
        baseURL.absoluteString
    }

    // MARK: - Validation

    public static func validate(_ url: URL) throws {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased()
        else {
            throw CloudEnvironmentError.malformedURL
        }
        guard let host = bareHost(of: url), !host.isEmpty else {
            throw CloudEnvironmentError.missingHost
        }
        if components.user != nil || components.password != nil {
            throw CloudEnvironmentError.embeddedCredentials
        }
        switch scheme {
        case "https":
            return
        case "http":
            guard loopbackHosts.contains(host) else {
                throw CloudEnvironmentError.insecureScheme
            }
        default:
            throw CloudEnvironmentError.unsupportedScheme(scheme)
        }
    }

    /// Validates, lowercases scheme + host, and strips query, fragment and
    /// trailing slashes so endpoint construction is deterministic.
    public static func normalized(_ url: URL) throws -> URL {
        try validate(url)
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw CloudEnvironmentError.malformedURL
        }
        components.scheme = components.scheme?.lowercased()
        components.percentEncodedHost = components.percentEncodedHost?.lowercased()
        components.query = nil
        components.fragment = nil
        var path = components.percentEncodedPath
        while path.hasSuffix("/") {
            path.removeLast()
        }
        components.percentEncodedPath = path
        guard let result = components.url else {
            throw CloudEnvironmentError.malformedURL
        }
        return result
    }

    // MARK: - Endpoint construction

    /// `baseURL` + `path` (e.g. `/api/v1/jobs`), preserving any base path prefix.
    public func endpoint(_ path: String, query: [URLQueryItem] = []) -> URL {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            return baseURL.appendingPathComponent(path)
        }
        let suffix = path.hasPrefix("/") ? path : "/" + path
        components.path = components.path + suffix
        components.queryItems = query.isEmpty ? nil : query
        return components.url ?? baseURL.appendingPathComponent(path)
    }

    /// Resolves a server-provided reference (`/api/v1/jobs/job_…` or an absolute
    /// URL) against the base origin. Returns `nil` when it cannot be parsed.
    public func resolve(_ reference: String) -> URL? {
        let trimmed = reference.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if trimmed.hasPrefix("/"), !trimmed.hasPrefix("//") {
            guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
                  let referenceComponents = URLComponents(string: trimmed)
            else {
                return nil
            }
            components.percentEncodedPath = referenceComponents.percentEncodedPath
            components.percentEncodedQuery = referenceComponents.percentEncodedQuery
            components.fragment = nil
            return components.url
        }
        return URL(string: trimmed, relativeTo: baseURL)?.absoluteURL
    }

    // MARK: - Same-origin policy

    /// Bearer tokens are only ever attached to requests for this exact origin
    /// (scheme + host + effective port).
    public func isSameOrigin(_ url: URL) -> Bool {
        guard let lhs = CloudEnvironment.origin(of: baseURL),
              let rhs = CloudEnvironment.origin(of: url)
        else {
            return false
        }
        return lhs == rhs
    }

    @discardableResult
    public func requireSameOrigin(_ reference: String) throws -> URL {
        guard let url = resolve(reference) else {
            throw CloudError.invalidURL(reference)
        }
        guard isSameOrigin(url) else {
            throw CloudError.crossOrigin(url.absoluteString)
        }
        return url
    }

    // MARK: - Helpers

    struct Origin: Equatable {
        let scheme: String
        let host: String
        let port: Int
    }

    static func origin(of url: URL) -> Origin? {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: true),
              let scheme = components.scheme?.lowercased(),
              let host = bareHost(of: url)
        else {
            return nil
        }
        let port: Int
        if let explicit = components.port {
            port = explicit
        } else {
            switch scheme {
            case "https": port = 443
            case "http": port = 80
            default: return nil
            }
        }
        return Origin(scheme: scheme, host: host, port: port)
    }

    /// Host without IPv6 brackets, lowercased.
    static func bareHost(of url: URL) -> String? {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: true),
              let raw = components.percentEncodedHost, !raw.isEmpty
        else {
            return nil
        }
        let lowered = raw.lowercased()
        if lowered.hasPrefix("["), lowered.hasSuffix("]") {
            return String(lowered.dropFirst().dropLast())
        }
        return lowered.removingPercentEncoding ?? lowered
    }
}
