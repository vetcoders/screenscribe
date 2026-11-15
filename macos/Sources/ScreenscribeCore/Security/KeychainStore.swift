import Foundation
import Security

/// Byte storage for credentials. Implementations must be thread-safe.
public protocol KeychainStoreProtocol: Sendable {
    func data(for account: String) throws -> Data?
    func set(_ data: Data, for account: String) throws
    func remove(_ account: String) throws
    func removeAll() throws
}

public enum KeychainError: Error, Equatable, Sendable, LocalizedError {
    case unhandled(OSStatus)
    case unexpectedItem

    public var errorDescription: String? {
        switch self {
        case .unhandled(let status):
            let message = SecCopyErrorMessageString(status, nil) as String? ?? "\(status)"
            return L10n.format("error.keychain", message)
        case .unexpectedItem:
            return L10n.format("error.keychain", "unexpected item")
        }
    }
}

/// Generic-password Keychain storage scoped to the app's sandbox container.
///
/// Items are `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`: available to
/// background uploads after login, never synced to iCloud or restored to
/// another Mac.
public struct SecurityKeychainStore: KeychainStoreProtocol {
    public static let defaultService = "io.vetcoders.screenscribe.auth"

    public let service: String
    /// Use the data-protection keychain. Requires a signed build with a
    /// keychain access group, so it stays off for `swift run` development.
    public let useDataProtectionKeychain: Bool

    public init(service: String = SecurityKeychainStore.defaultService, useDataProtectionKeychain: Bool = false) {
        self.service = service
        self.useDataProtectionKeychain = useDataProtectionKeychain
    }

    private func baseQuery(account: String?) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
        ]
        if let account {
            query[kSecAttrAccount as String] = account
        }
        if useDataProtectionKeychain {
            query[kSecUseDataProtectionKeychain as String] = true
        }
        return query
    }

    public func data(for account: String) throws -> Data? {
        var query = baseQuery(account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        switch status {
        case errSecSuccess:
            guard let data = result as? Data else { throw KeychainError.unexpectedItem }
            return data
        case errSecItemNotFound:
            return nil
        default:
            throw KeychainError.unhandled(status)
        }
    }

    public func set(_ data: Data, for account: String) throws {
        let query = baseQuery(account: account)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        switch updateStatus {
        case errSecSuccess:
            return
        case errSecItemNotFound:
            var insert = query
            insert.merge(attributes) { _, new in new }
            let addStatus = SecItemAdd(insert as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw KeychainError.unhandled(addStatus) }
        default:
            throw KeychainError.unhandled(updateStatus)
        }
    }

    public func remove(_ account: String) throws {
        let status = SecItemDelete(baseQuery(account: account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.unhandled(status)
        }
    }

    public func removeAll() throws {
        let status = SecItemDelete(baseQuery(account: nil) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.unhandled(status)
        }
    }
}

/// Process-local store for previews and the integrator's tests.
public final class InMemoryKeychainStore: KeychainStoreProtocol, @unchecked Sendable {
    private let lock = NSLock()
    private var items: [String: Data]

    public init(items: [String: Data] = [:]) {
        self.items = items
    }

    public func data(for account: String) throws -> Data? {
        lock.withLock { items[account] }
    }

    public func set(_ data: Data, for account: String) throws {
        lock.withLock { items[account] = data }
    }

    public func remove(_ account: String) throws {
        lock.withLock { _ = items.removeValue(forKey: account) }
    }

    public func removeAll() throws {
        lock.withLock { items.removeAll() }
    }

    public var accounts: [String] {
        lock.withLock { Array(items.keys).sorted() }
    }
}
