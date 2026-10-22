import CoreAudio
import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var controller: VolumeController
    @StateObject private var tone = TestTonePlayer()

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            deviceSection
            Divider()
            masterSection
            Divider()
            balanceSection
            Divider()
            testSection
            Divider()
            optionsSection
        }
        .padding(20)
        .frame(width: 520)
        .onDisappear { tone.stop() }
    }

    // MARK: - Sections

    private var deviceSection: some View {
        HStack {
            Text("Output device")
                .font(.headline)
            Picker("Output device", selection: deviceBinding) {
                ForEach(controller.devices) { device in
                    Text(device.name).tag(device.id)
                }
            }
            .labelsHidden()
            Button(action: { controller.refreshDevices() }) {
                Image(systemName: "arrow.clockwise")
            }
            .help("Rescan output devices")
        }
    }

    private var masterSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Overall volume")
                .font(.headline)
            HStack {
                Image(systemName: "speaker.fill")
                    .foregroundColor(.secondary)
                Slider(value: masterBinding, in: 0...1)
                Image(systemName: "speaker.wave.3.fill")
                    .foregroundColor(.secondary)
                Text(percent(controller.master))
                    .font(.body.monospacedDigit())
                    .frame(width: 46, alignment: .trailing)
            }
            Text("Raises or lowers both speakers together while keeping the balance below.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .disabled(controller.capability == .unsupported)
    }

    private var balanceSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Speaker balance")
                    .font(.headline)
                Spacer()
                Button("Reset") { controller.resetTrims() }
                    .disabled(controller.capability != .stereo)
            }
            channelRow(title: "Left",
                       value: controller.leftTrim,
                       hardware: controller.hardwareLeft,
                       decibels: controller.leftDecibels,
                       binding: leftBinding)
            channelRow(title: "Right",
                       value: controller.rightTrim,
                       hardware: controller.hardwareRight,
                       decibels: controller.rightDecibels,
                       binding: rightBinding)
            Text(balanceHint)
                .font(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func channelRow(title: String,
                            value: Double,
                            hardware: Double,
                            decibels: Float32?,
                            binding: Binding<Double>) -> some View {
        HStack {
            Text(title)
                .frame(width: 44, alignment: .leading)
            Slider(value: binding, in: 0...1)
            Text(percent(value))
                .font(.body.monospacedDigit())
                .frame(width: 46, alignment: .trailing)
            Text(hardwareText(hardware: hardware, decibels: decibels))
                .font(.caption.monospacedDigit())
                .foregroundColor(.secondary)
                .frame(width: 96, alignment: .trailing)
                .help("What the hardware currently reports for this speaker")
        }
        .disabled(controller.capability != .stereo)
    }

    private var testSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Test sound")
                .font(.headline)
            HStack {
                toneButton("Left", side: .left)
                toneButton("Right", side: .right)
                toneButton("Both", side: .both)
                if tone.playing != nil {
                    Button("Stop") { tone.stop() }
                        .keyboardShortcut(.escape, modifiers: [])
                }
            }
            if let error = tone.errorText {
                Text(error)
                    .font(.caption)
                    .foregroundColor(.red)
            }
            Text("Plays pink noise through one speaker at a time so you can match them by ear.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }

    private func toneButton(_ title: String, side: TestTonePlayer.Side) -> some View {
        Button(action: { tone.toggle(side, deviceID: controller.selectedDevice?.id) }) {
            Label(title, systemImage: tone.playing == side ? "stop.fill" : "play.fill")
        }
    }

    private var optionsSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle("Keep balance when the volume is changed elsewhere", isOn: keepBalanceBinding)
                .disabled(controller.capability != .stereo)
            Text("Re-applies your balance after the volume keys, the menu bar slider or a sleep/wake cycle move the speakers. Turn this off if you'd rather use the Balance slider in System Preferences.")
                .font(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - Helpers

    private var balanceHint: String {
        switch controller.capability {
        case .stereo:
            return "Turn the louder speaker down until both sound the same, then use Overall volume. The ratio you set here is kept as the volume goes up and down."
        case .masterOnly:
            return "This device only has a single volume control, so the speakers can't be balanced separately. Try another output device."
        case .unsupported:
            return "This device has no adjustable volume (common for HDMI and optical outputs). Use the volume knob on the sound system itself, or pick another output."
        }
    }

    private func percent(_ value: Double) -> String {
        String(format: "%.0f%%", value * 100)
    }

    private func hardwareText(hardware: Double, decibels: Float32?) -> String {
        if let db = decibels {
            return String(format: "now %.1f dB", db)
        }
        return "now " + percent(hardware)
    }

    private var deviceBinding: Binding<AudioDeviceID> {
        Binding(
            get: { controller.selectedDevice?.id ?? 0 },
            set: { controller.userSelected(deviceID: $0) }
        )
    }

    private var masterBinding: Binding<Double> {
        Binding(get: { controller.master }, set: { controller.setMaster($0) })
    }

    private var leftBinding: Binding<Double> {
        Binding(get: { controller.leftTrim }, set: { controller.setLeftTrim($0) })
    }

    private var rightBinding: Binding<Double> {
        Binding(get: { controller.rightTrim }, set: { controller.setRightTrim($0) })
    }

    private var keepBalanceBinding: Binding<Bool> {
        Binding(get: { controller.keepBalance }, set: { controller.setKeepBalance($0) })
    }
}
