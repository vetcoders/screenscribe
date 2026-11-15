import Foundation
import ScreenscribeCore

/// Per-session HTTP stubbing via `X-Mock-Session-ID` so parallel Swift Testing suites never collide.
final class MockURLProtocol: URLProtocol, @unchecked Sendable {
    struct CapturedRequest: Sendable {
        let method: String
        let url: URL
        let headers: [String: String]
        let body: Data

        func header(_ name: String) -> String? {
            let needle = name.lowercased()
            return headers.first { $0.key.lowercased() == needle }?.value
        }
    }

    struct StubResponse: Sendable {
        var statusCode: Int
        var headers: [String: String]
        var chunks: [Data]
        var error: (any Error)?

        init(
            statusCode: Int = 200,
            headers: [String: String] = ["Content-Type": "application/json"],
            data: Data = Data(),
            error: (any Error)? = nil
        ) {
            self.statusCode = statusCode
            self.headers = headers
            self.chunks = data.isEmpty ? [] : [data]
            self.error = error
        }

        init(
            statusCode: Int = 200,
            headers: [String: String] = ["Content-Type": "text/event-stream"],
            chunks: [Data]
        ) {
            self.statusCode = statusCode
            self.headers = headers
            self.chunks = chunks
            self.error = nil
        }

        static func json(_ object: Any, statusCode: Int = 200) -> StubResponse {
            let payload = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data()
            return StubResponse(statusCode: statusCode, headers: ["Content-Type": "application/json"], data: payload)
        }
    }

    typealias Handler = @Sendable (CapturedRequest) throws -> StubResponse

    private static let sessionHeader = "X-Mock-Session-ID"
    private static let registryLock = NSLock()
    nonisolated(unsafe) private static var handlers: [String: Handler] = [:]

    static func makeSession(handler: @escaping Handler) -> (URLSession, String) {
        let id = UUID().uuidString
        registryLock.withLock {
            handlers[id] = handler
        }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        config.httpShouldSetCookies = false
        config.httpCookieAcceptPolicy = .never
        config.httpAdditionalHeaders = [sessionHeader: id]
        return (URLSession(configuration: config), id)
    }

    static func unregister(_ id: String) {
        registryLock.withLock {
            _ = handlers.removeValue(forKey: id)
        }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let client else { return }
        let headers = request.allHTTPHeaderFields ?? [:]
        let sessionID = headers.first { $0.key.caseInsensitiveCompare(Self.sessionHeader) == .orderedSame }?.value
        let handler: Handler? = Self.registryLock.withLock {
            sessionID.flatMap { Self.handlers[$0] }
        }
        guard let handler, let url = request.url else {
            client.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }

        let body = request.httpBody ?? Self.readStream(request.httpBodyStream)
        let captured = CapturedRequest(
            method: request.httpMethod ?? "GET",
            url: url,
            headers: headers,
            body: body
        )

        do {
            let stub = try handler(captured)
            if let error = stub.error {
                client.urlProtocol(self, didFailWithError: error)
                return
            }
            guard let response = HTTPURLResponse(
                url: url,
                statusCode: stub.statusCode,
                httpVersion: "HTTP/1.1",
                headerFields: stub.headers
            ) else {
                client.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
                return
            }
            client.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            for chunk in stub.chunks where !chunk.isEmpty {
                client.urlProtocol(self, didLoad: chunk)
            }
            client.urlProtocolDidFinishLoading(self)
        } catch {
            client.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}

    private static func readStream(_ stream: InputStream?) -> Data {
        guard let stream else { return Data() }
        stream.open()
        defer { stream.close() }
        var output = Data()
        let bufferSize = 64 * 1024
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }
        while stream.hasBytesAvailable {
            let count = stream.read(buffer, maxLength: bufferSize)
            if count > 0 {
                output.append(buffer, count: count)
            } else {
                break
            }
        }
        return output
    }
}
