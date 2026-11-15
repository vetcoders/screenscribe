// swift-tools-version: 6.0
//
// Screenscribe for macOS — native Mac App Store client.
// ScreenscribeCore: networking, auth, billing, capture, queue and observable state.
// ScreenscribeApp: the SwiftUI app (MenuBarExtra HUD, workspace, settings).
//
// The test target (ScreenscribeTests) is owned by the integrator and is wired in
// once macos/Tests/ScreenscribeTests exists.

import PackageDescription

let package = Package(
    name: "Screenscribe",
    defaultLocalization: "en",
    platforms: [
        .macOS(.v15),
    ],
    products: [
        .library(name: "ScreenscribeCore", targets: ["ScreenscribeCore"]),
        .executable(name: "ScreenscribeApp", targets: ["ScreenscribeApp"]),
    ],
    targets: [
        .target(
            name: "ScreenscribeCore",
            path: "Sources/ScreenscribeCore",
            resources: [
                .process("Resources"),
            ]
        ),
        .executableTarget(
            name: "ScreenscribeApp",
            dependencies: ["ScreenscribeCore"],
            path: "Sources/ScreenscribeApp"
        ),
    ]
)
