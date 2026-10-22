import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Needed when run as a bare executable (swift run) rather than an .app
        // bundle, so the window comes to the front with a menu bar.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@main
struct SpeakerBalanceApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var controller = VolumeController()

    var body: some Scene {
        WindowGroup("Speaker Balance") {
            ContentView()
                .environmentObject(controller)
        }
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}
