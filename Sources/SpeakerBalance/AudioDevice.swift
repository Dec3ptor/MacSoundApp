import CoreAudio
import Foundation

/// A thin wrapper around a CoreAudio output device.
///
/// macOS exposes a hardware volume scalar (0…1) per output channel on most
/// built-in and USB devices. Element 1 is the left channel, element 2 the
/// right channel and element 0 the "master" control (when the device has one).
/// The Balance slider in System Preferences is implemented on top of exactly
/// these per-channel controls, which is what this app drives directly.
struct AudioDevice: Identifiable, Hashable {
    let id: AudioDeviceID
    let uid: String
    let name: String
    let outputChannelCount: Int

    static let masterChannel: AudioObjectPropertyElement = 0
    static let leftChannel: AudioObjectPropertyElement = 1
    static let rightChannel: AudioObjectPropertyElement = 2

    /// Returns nil for devices without any output channels (microphones etc.).
    init?(id: AudioDeviceID) {
        let channels = AudioDevice.outputChannelCount(of: id)
        guard channels > 0 else { return nil }
        self.id = id
        self.outputChannelCount = channels
        self.uid = AudioDevice.stringProperty(id, selector: kAudioDevicePropertyDeviceUID) ?? "device-\(id)"
        self.name = AudioDevice.stringProperty(id, selector: kAudioObjectPropertyName) ?? "Output device \(id)"
    }

    // MARK: - Discovery

    static func allOutputDevices() -> [AudioDevice] {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: masterChannel
        )
        let systemObject = AudioObjectID(kAudioObjectSystemObject)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(systemObject, &address, 0, nil, &size) == noErr, size > 0 else {
            return []
        }
        let count = Int(size) / MemoryLayout<AudioDeviceID>.size
        var ids = [AudioDeviceID](repeating: 0, count: count)
        guard AudioObjectGetPropertyData(systemObject, &address, 0, nil, &size, &ids) == noErr else {
            return []
        }
        return ids.compactMap { AudioDevice(id: $0) }
    }

    static func defaultOutputDeviceID() -> AudioDeviceID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: masterChannel
        )
        var deviceID: AudioDeviceID = 0
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceID)
        guard status == noErr, deviceID != 0 else { return nil }
        return deviceID
    }

    private static func outputChannelCount(of id: AudioDeviceID) -> Int {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioObjectPropertyScopeOutput,
            mElement: masterChannel
        )
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &address, 0, nil, &size) == noErr, size > 0 else { return 0 }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, raw) == noErr else { return 0 }
        let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
        return list.reduce(0) { $0 + Int($1.mNumberChannels) }
    }

    private static func stringProperty(_ id: AudioObjectID, selector: AudioObjectPropertySelector) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: masterChannel
        )
        guard AudioObjectHasProperty(id, &address) else { return nil }
        var value: Unmanaged<CFString>? = nil
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value)
        guard status == noErr, let string = value?.takeRetainedValue() else { return nil }
        return string as String
    }

    // MARK: - Volume

    private func volumeAddress(_ selector: AudioObjectPropertySelector, channel: AudioObjectPropertyElement) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeOutput, mElement: channel)
    }

    func hasVolume(channel: AudioObjectPropertyElement) -> Bool {
        var address = volumeAddress(kAudioDevicePropertyVolumeScalar, channel: channel)
        return AudioObjectHasProperty(id, &address)
    }

    func canSetVolume(channel: AudioObjectPropertyElement) -> Bool {
        var address = volumeAddress(kAudioDevicePropertyVolumeScalar, channel: channel)
        guard AudioObjectHasProperty(id, &address) else { return false }
        var settable: DarwinBoolean = false
        guard AudioObjectIsPropertySettable(id, &address, &settable) == noErr else { return false }
        return settable.boolValue
    }

    /// Hardware volume scalar, 0…1.
    func volume(channel: AudioObjectPropertyElement) -> Float32? {
        var address = volumeAddress(kAudioDevicePropertyVolumeScalar, channel: channel)
        var value: Float32 = 0
        var size = UInt32(MemoryLayout<Float32>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr else { return nil }
        return value
    }

    @discardableResult
    func setVolume(_ volume: Float32, channel: AudioObjectPropertyElement) -> Bool {
        var address = volumeAddress(kAudioDevicePropertyVolumeScalar, channel: channel)
        var value = min(1, max(0, volume))
        let size = UInt32(MemoryLayout<Float32>.size)
        return AudioObjectSetPropertyData(id, &address, 0, nil, size, &value) == noErr
    }

    /// The same control expressed in decibels, as reported by the driver.
    func decibels(channel: AudioObjectPropertyElement) -> Float32? {
        var address = volumeAddress(kAudioDevicePropertyVolumeDecibels, channel: channel)
        guard AudioObjectHasProperty(id, &address) else { return nil }
        var value: Float32 = 0
        var size = UInt32(MemoryLayout<Float32>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr else { return nil }
        return value
    }
}

/// Registers a CoreAudio property listener and removes it when deallocated.
final class PropertyListener {
    private let objectID: AudioObjectID
    private var address: AudioObjectPropertyAddress
    private let queue: DispatchQueue
    private let block: AudioObjectPropertyListenerBlock
    private var active = false

    init?(objectID: AudioObjectID,
          selector: AudioObjectPropertySelector,
          scope: AudioObjectPropertyScope,
          element: AudioObjectPropertyElement,
          queue: DispatchQueue = .main,
          handler: @escaping () -> Void) {
        self.objectID = objectID
        self.address = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: element)
        self.queue = queue
        self.block = { _, _ in handler() }
        let status = AudioObjectAddPropertyListenerBlock(objectID, &address, queue, block)
        guard status == noErr else { return nil }
        active = true
    }

    func remove() {
        guard active else { return }
        AudioObjectRemovePropertyListenerBlock(objectID, &address, queue, block)
        active = false
    }

    deinit {
        remove()
    }
}
