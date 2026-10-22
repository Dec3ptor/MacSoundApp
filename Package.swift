// swift-tools-version:5.5
// Speaker Balance – per-speaker trim + linked overall volume for macOS.
// Builds with the Xcode 13.2 toolchain (the last one that runs on Big Sur).
import PackageDescription

let package = Package(
    name: "SpeakerBalance",
    platforms: [
        .macOS(.v11)
    ],
    targets: [
        .executableTarget(
            name: "SpeakerBalance",
            path: "Sources/SpeakerBalance",
            linkerSettings: [
                .linkedFramework("CoreAudio"),
                .linkedFramework("AudioToolbox"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("AppKit"),
            ]
        )
    ]
)
