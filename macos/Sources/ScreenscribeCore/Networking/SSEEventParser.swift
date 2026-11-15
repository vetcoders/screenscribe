import Foundation

/// One dispatched server-sent event.
public struct SSEEvent: Sendable, Equatable {
    /// `event:` field; `nil` means the default `message` type.
    public var event: String?
    public var data: String
    public var id: String?
    public var retry: Int?

    public init(event: String? = nil, data: String, id: String? = nil, retry: Int? = nil) {
        self.event = event
        self.data = data
        self.id = id
        self.retry = retry
    }
}

/// Incremental `text/event-stream` parser (WHATWG HTML §9.2).
///
/// Bytes are buffered per line and decoded only once a line terminator
/// arrives, so a multi-byte UTF-8 character split across two network chunks
/// is reassembled correctly. Handles CR, LF and CRLF terminators, a leading
/// BOM, `:` comment lines (keepalives), multi-line `data:` and `retry:`.
public struct SSEEventParser: Sendable {
    private var lineBuffer: [UInt8] = []
    private var dataBuffer = ""
    private var hasData = false
    private var eventType: String?
    private var lastEventID: String?
    private var retry: Int?
    private var previousWasCR = false
    private var atStreamStart = true

    /// Comment lines seen so far (`: keepalive`).
    public private(set) var keepaliveCount = 0

    public init() {}

    /// Feeds one byte; returns an event when a blank line dispatches one.
    public mutating func consume(byte: UInt8) -> SSEEvent? {
        if previousWasCR {
            previousWasCR = false
            if byte == 0x0A {
                return nil
            }
        }
        switch byte {
        case 0x0D:
            previousWasCR = true
            return processLine()
        case 0x0A:
            return processLine()
        default:
            lineBuffer.append(byte)
            return nil
        }
    }

    /// Feeds a chunk; returns every event it completes, in order.
    public mutating func consume(_ data: Data) -> [SSEEvent] {
        var events: [SSEEvent] = []
        for byte in data {
            if let event = consume(byte: byte) {
                events.append(event)
            }
        }
        return events
    }

    /// End of stream. The spec discards an unterminated event; pass
    /// `dispatchIncomplete: true` to salvage it instead.
    public mutating func finish(dispatchIncomplete: Bool = false) -> SSEEvent? {
        if !lineBuffer.isEmpty {
            _ = processLine()
        }
        let event = dispatchIncomplete ? dispatch() : nil
        resetEvent()
        previousWasCR = false
        return event
    }

    private mutating func processLine() -> SSEEvent? {
        var bytes = lineBuffer
        lineBuffer.removeAll(keepingCapacity: true)
        if atStreamStart {
            atStreamStart = false
            if bytes.starts(with: [0xEF, 0xBB, 0xBF]) {
                bytes.removeFirst(3)
            }
        }
        let line = String(decoding: bytes, as: UTF8.self)
        if line.isEmpty {
            return dispatch()
        }
        if line.hasPrefix(":") {
            keepaliveCount += 1
            return nil
        }

        let field: Substring
        var value: Substring
        if let colon = line.firstIndex(of: ":") {
            field = line[line.startIndex..<colon]
            value = line[line.index(after: colon)...]
            if value.hasPrefix(" ") {
                value = value.dropFirst()
            }
        } else {
            field = line[line.startIndex...]
            value = ""
        }

        switch field {
        case "event":
            eventType = String(value)
        case "data":
            dataBuffer.append(contentsOf: value)
            dataBuffer.append("\n")
            hasData = true
        case "id":
            if !value.contains("\u{0}") {
                lastEventID = String(value)
            }
        case "retry":
            if !value.isEmpty, value.allSatisfy({ $0.isASCII && $0.isNumber }) {
                retry = Int(value)
            }
        default:
            break
        }
        return nil
    }

    private mutating func dispatch() -> SSEEvent? {
        defer { resetEvent() }
        guard hasData else { return nil }
        var data = dataBuffer
        if data.hasSuffix("\n") {
            data.removeLast()
        }
        let type = (eventType?.isEmpty ?? true) ? nil : eventType
        return SSEEvent(event: type, data: data, id: lastEventID, retry: retry)
    }

    private mutating func resetEvent() {
        dataBuffer = ""
        hasData = false
        eventType = nil
    }
}
