import ScreenscribeCore
import SwiftUI

/// Screenscribe for macOS: a native recorder + uploader for the hosted
/// review pipeline. Everything heavy (audio extraction, transcription,
/// analysis, report rendering) runs in Screenscribe Cloud; the app only
/// records, uploads and shows the hosted review.
@main
struct ScreenscribeMacApp: App {
    static let workspaceWindowID = "workspace"

    @State private var model = AppModel()

    var body: some Scene {
        Window(L10n.string("app.name"), id: Self.workspaceWindowID) {
            MainWorkspaceView()
                .environment(model)
                .task { await model.start() }
        }
        .defaultSize(width: 1120, height: 740)
        .windowToolbarStyle(.unified)
        .commands {
            ScreenscribeCommands(model: model)
        }

        MenuBarExtra {
            MenuBarHUDView()
                .environment(model)
                .task { await model.start() }
        } label: {
            MenuBarStatusLabel(recorder: model.recorder)
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView()
                .environment(model)
        }
    }
}

/// Menu bar icon; shows the running timecode while recording.
struct MenuBarStatusLabel: View {
    let recorder: RecorderViewModel

    var body: some View {
        switch recorder.state {
        case .recording:
            Label(recorder.elapsedText, systemImage: "record.circle.fill")
                .labelStyle(.titleAndIcon)
                .monospacedDigit()
        case .paused:
            Label(recorder.elapsedText, systemImage: "pause.circle.fill")
                .labelStyle(.titleAndIcon)
                .monospacedDigit()
        case .countingDown(let remaining):
            Label(String(remaining), systemImage: "timer")
                .labelStyle(.titleAndIcon)
        default:
            Label(L10n.string("app.name"), systemImage: "record.circle")
                .labelStyle(.iconOnly)
        }
    }
}

struct ScreenscribeCommands: Commands {
    let model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button(L10n.string("menu.record")) {
                openWindow(id: ScreenscribeMacApp.workspaceWindowID)
                model.workspace.selection = .record
                model.recorder.startRecording()
            }
            .keyboardShortcut("r", modifiers: [.command, .shift])
            .disabled(model.recorder.state != .idle)

            Button(L10n.string("menu.stop")) {
                Task { await model.recorder.stopRecording() }
            }
            .keyboardShortcut(".", modifiers: [.command])
            .disabled(!model.recorder.state.isBusy)

            Divider()

            Button(L10n.string("menu.import")) {
                openWindow(id: ScreenscribeMacApp.workspaceWindowID)
                model.workspace.isImporterPresented = true
            }
            .keyboardShortcut("o", modifiers: [.command])
        }

        CommandMenu(Text(L10n.string("menu.account"))) {
            if model.isSignedIn {
                Button(L10n.string("menu.subscription")) {
                    openWindow(id: ScreenscribeMacApp.workspaceWindowID)
                    model.presentPaywall(reason: nil)
                }
                Button(L10n.string("menu.signout")) {
                    Task { await model.signOut() }
                }
            } else {
                Button(L10n.string("menu.signin")) {
                    openWindow(id: ScreenscribeMacApp.workspaceWindowID)
                    model.presentSignIn()
                }
            }
        }
    }
}
