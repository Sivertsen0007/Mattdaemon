// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "MLSuiteTerminal",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "MLSuiteTerminal",
            path: "Sources/MLSuiteTerminal"
        )
    ]
)
