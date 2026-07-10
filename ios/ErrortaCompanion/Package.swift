// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ErrortaCompanion",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "ErrortaCompanionCore", targets: ["ErrortaCompanionCore"]),
        .executable(name: "ErrortaCompanionApp", targets: ["ErrortaCompanionApp"]),
    ],
    targets: [
        .target(name: "ErrortaCompanionCore"),
        .executableTarget(
            name: "ErrortaCompanionApp",
            dependencies: ["ErrortaCompanionCore"]
        ),
        .testTarget(
            name: "ErrortaCompanionCoreTests",
            dependencies: ["ErrortaCompanionCore"]
        ),
    ]
)
