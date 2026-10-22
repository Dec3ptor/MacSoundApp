import Combine
import CoreAudio
import Foundation

/// Owns the "balance + linked overall volume" model and keeps it in sync with
/// the hardware.
///
/// Each speaker gets a *trim* (0…1). The hardware level written to a channel is
/// `master × trim`, so moving the master slider raises or lowers both speakers
/// by the same proportion while the ratio between them stays fixed. Trims are
/// remembered per output device.
final class VolumeController: ObservableObject {
    enum Capability {
        /// Left and right channels have independent, settable hardware volume.
        case stereo
        /// Only a single master control is available (no balance possible).
        case masterOnly
        /// No settable volume at all (typical for HDMI / optical outputs).
        case unsupported
    }

    @Published private(set) var devices: [AudioDevice] = []
    @Published private(set) var selectedDevice: AudioDevice?
    @Published private(set) var capability: Capability = .unsupported

    /// Overall level, 0…1.
    @Published private(set) var master: Double = 1
    /// Per-speaker trims, 0…1. 1 = "as loud as the master allows".
    @Published private(set) var leftTrim: Double = 1
    @Published private(set) var rightTrim: Double = 1

    /// What the hardware currently reports (for the read-outs in the UI).
    @Published private(set) var hardwareLeft: Double = 0
    @Published private(set) var hardwareRight: Double = 0
    @Published private(set) var leftDecibels: Float32?
    @Published private(set) var rightDecibels: Float32?

    /// Re-apply the balance whenever something else (volume keys, menu bar
    /// slider, sleep/wake) changes the hardware levels.
    @Published private(set) var keepBalance: Bool

    private let defaults = UserDefaults.standard
    private var deviceListeners: [PropertyListener] = []
    private var systemListeners: [PropertyListener] = []
    private var pendingSync: DispatchWorkItem?

    /// Last values we wrote, used to recognise our own writes echoing back.
    private var lastWritten: (left: Double, right: Double)?
    /// Last values we read, used to ignore no-op notifications.
    private var lastObserved: (left: Double, right: Double)?
    /// Hardware quantises the scalar, so compare with some slack.
    private let tolerance = 0.03
    /// Whether the selection should follow the system's default output device.
    private var followsSystemDefault = true

    private enum Keys {
        static let keepBalance = "keepBalance"
        static func trims(for device: AudioDevice) -> String { "trims." + device.uid }
    }

    init() {
        keepBalance = (defaults.object(forKey: Keys.keepBalance) as? Bool) ?? true
        refreshDevices()

        let systemObject = AudioObjectID(kAudioObjectSystemObject)
        systemListeners = [
            PropertyListener(objectID: systemObject,
                             selector: kAudioHardwarePropertyDefaultOutputDevice,
                             scope: kAudioObjectPropertyScopeGlobal,
                             element: AudioDevice.masterChannel) { [weak self] in
                self?.systemDefaultChanged()
            },
            PropertyListener(objectID: systemObject,
                             selector: kAudioHardwarePropertyDevices,
                             scope: kAudioObjectPropertyScopeGlobal,
                             element: AudioDevice.masterChannel) { [weak self] in
                self?.refreshDevices()
            },
        ].compactMap { $0 }
    }

    // MARK: - Devices

    func refreshDevices() {
        devices = AudioDevice.allOutputDevices()
        let systemDefault = AudioDevice.defaultOutputDeviceID()

        if let current = selectedDevice,
           !followsSystemDefault,
           devices.contains(where: { $0.id == current.id }) {
            return
        }
        if let id = systemDefault, let device = devices.first(where: { $0.id == id }) {
            if selectedDevice?.id != device.id { select(device) }
        } else if let first = devices.first {
            if selectedDevice?.id != first.id { select(first) }
        } else {
            select(nil)
        }
    }

    /// Called when the user picks a device in the popup.
    func userSelected(deviceID: AudioDeviceID) {
        guard let device = devices.first(where: { $0.id == deviceID }) else { return }
        followsSystemDefault = (deviceID == AudioDevice.defaultOutputDeviceID())
        if selectedDevice?.id != device.id { select(device) }
    }

    private func systemDefaultChanged() {
        guard followsSystemDefault else { return }
        refreshDevices()
    }

    private func select(_ device: AudioDevice?) {
        pendingSync?.cancel()
        pendingSync = nil
        deviceListeners = []
        lastWritten = nil
        lastObserved = nil
        selectedDevice = device

        guard let device = device else {
            capability = .unsupported
            return
        }

        if device.canSetVolume(channel: AudioDevice.leftChannel) && device.canSetVolume(channel: AudioDevice.rightChannel) {
            capability = .stereo
        } else if device.canSetVolume(channel: AudioDevice.masterChannel) {
            capability = .masterOnly
        } else {
            capability = .unsupported
            leftDecibels = nil
            rightDecibels = nil
            return
        }

        let channels: [AudioObjectPropertyElement] = capability == .stereo
            ? [AudioDevice.leftChannel, AudioDevice.rightChannel]
            : [AudioDevice.masterChannel]
        deviceListeners = channels.compactMap { channel in
            PropertyListener(objectID: device.id,
                             selector: kAudioDevicePropertyVolumeScalar,
                             scope: kAudioObjectPropertyScopeOutput,
                             element: channel) { [weak self] in
                self?.scheduleSync()
            }
        }

        if capability == .stereo, keepBalance, let saved = savedTrims(for: device) {
            // Restore the remembered balance for this device and re-apply it
            // at whatever overall level the hardware is currently at.
            leftTrim = saved.left
            rightTrim = saved.right
            let levels = readLevels()
            updateMeters(levels)
            lastObserved = levels
            master = inferMaster(from: levels)
            apply()
        } else {
            adoptHardware()
        }
    }

    // MARK: - User actions

    func setMaster(_ value: Double) {
        master = clamp(value)
        apply()
    }

    func setLeftTrim(_ value: Double) {
        leftTrim = clamp(value)
        saveTrims()
        apply()
    }

    func setRightTrim(_ value: Double) {
        rightTrim = clamp(value)
        saveTrims()
        apply()
    }

    func resetTrims() {
        leftTrim = 1
        rightTrim = 1
        saveTrims()
        apply()
    }

    func setKeepBalance(_ enabled: Bool) {
        keepBalance = enabled
        defaults.set(enabled, forKey: Keys.keepBalance)
        if enabled, capability == .stereo {
            apply()
        }
    }

    // MARK: - Hardware I/O

    /// Writes `master × trim` to each channel.
    private func apply() {
        guard let device = selectedDevice else { return }
        switch capability {
        case .stereo:
            let left = clamp(master * leftTrim)
            let right = clamp(master * rightTrim)
            lastWritten = (left, right)
            device.setVolume(Float32(left), channel: AudioDevice.leftChannel)
            device.setVolume(Float32(right), channel: AudioDevice.rightChannel)
        case .masterOnly:
            lastWritten = (master, master)
            device.setVolume(Float32(master), channel: AudioDevice.masterChannel)
        case .unsupported:
            break
        }
        // Refresh the read-outs shortly after the driver has settled.
        scheduleSync()
    }

    private func readLevels() -> (left: Double, right: Double) {
        guard let device = selectedDevice else { return (0, 0) }
        switch capability {
        case .stereo:
            let left = Double(device.volume(channel: AudioDevice.leftChannel) ?? 0)
            let right = Double(device.volume(channel: AudioDevice.rightChannel) ?? 0)
            return (left, right)
        case .masterOnly:
            let level = Double(device.volume(channel: AudioDevice.masterChannel) ?? 0)
            return (level, level)
        case .unsupported:
            return (0, 0)
        }
    }

    private func updateMeters(_ levels: (left: Double, right: Double)) {
        hardwareLeft = levels.left
        hardwareRight = levels.right
        guard let device = selectedDevice else {
            leftDecibels = nil
            rightDecibels = nil
            return
        }
        switch capability {
        case .stereo:
            leftDecibels = device.decibels(channel: AudioDevice.leftChannel)
            rightDecibels = device.decibels(channel: AudioDevice.rightChannel)
        case .masterOnly:
            let level = device.decibels(channel: AudioDevice.masterChannel)
            leftDecibels = level
            rightDecibels = level
        case .unsupported:
            leftDecibels = nil
            rightDecibels = nil
        }
    }

    /// Take the model from whatever the hardware is doing right now.
    private func adoptHardware() {
        let levels = readLevels()
        updateMeters(levels)
        lastObserved = levels
        switch capability {
        case .stereo:
            let peak = max(levels.left, levels.right)
            master = peak
            if peak > 0.001 {
                leftTrim = clamp(levels.left / peak)
                rightTrim = clamp(levels.right / peak)
            }
        case .masterOnly:
            master = levels.left
            leftTrim = 1
            rightTrim = 1
        case .unsupported:
            break
        }
    }

    /// Given hardware levels, work out the master that best matches our trims.
    private func inferMaster(from levels: (left: Double, right: Double)) -> Double {
        let scale = max(leftTrim, rightTrim)
        guard scale > 0.001 else { return max(levels.left, levels.right) }
        return clamp(max(levels.left, levels.right) / scale)
    }

    /// Listener callbacks arrive once per channel, so coalesce them and look
    /// at the device a moment later when both channels have settled.
    private func scheduleSync() {
        pendingSync?.cancel()
        let item = DispatchWorkItem { [weak self] in self?.syncFromHardware() }
        pendingSync = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: item)
    }

    private func syncFromHardware() {
        guard selectedDevice != nil, capability != .unsupported else { return }
        let levels = readLevels()
        updateMeters(levels)

        // Nothing actually changed since we last looked → nothing to do.
        // (This also guarantees we can never fight the driver in a loop.)
        if let observed = lastObserved,
           abs(observed.left - levels.left) < 0.001,
           abs(observed.right - levels.right) < 0.001 {
            return
        }
        lastObserved = levels

        // Our own write coming back → nothing to do.
        if let written = lastWritten,
           abs(written.left - levels.left) <= tolerance,
           abs(written.right - levels.right) <= tolerance {
            return
        }

        // Something else changed the volume (keys, menu bar, another app…).
        guard capability == .stereo else {
            master = levels.left
            return
        }

        if keepBalance {
            master = inferMaster(from: levels)
            let expectedLeft = clamp(master * leftTrim)
            let expectedRight = clamp(master * rightTrim)
            if abs(expectedLeft - levels.left) > tolerance || abs(expectedRight - levels.right) > tolerance {
                apply()
            }
        } else {
            adoptHardware()
        }
    }

    // MARK: - Persistence

    private func savedTrims(for device: AudioDevice) -> (left: Double, right: Double)? {
        guard let values = defaults.array(forKey: Keys.trims(for: device)) as? [Double], values.count == 2 else {
            return nil
        }
        return (clamp(values[0]), clamp(values[1]))
    }

    private func saveTrims() {
        guard let device = selectedDevice else { return }
        defaults.set([leftTrim, rightTrim], forKey: Keys.trims(for: device))
    }

    private func clamp(_ value: Double) -> Double {
        min(1, max(0, value))
    }
}
