import AudioToolbox
import AVFoundation
import Combine
import CoreAudio
import Foundation

/// Plays pink noise through the left, right or both speakers so the two can be
/// compared by ear while adjusting the balance.
final class TestTonePlayer: ObservableObject {
    enum Side: Equatable {
        case left, right, both
    }

    @Published private(set) var playing: Side?
    @Published private(set) var errorText: String?

    private var engine: AVAudioEngine?
    private var sourceNode: AVAudioSourceNode?

    func toggle(_ side: Side, deviceID: AudioDeviceID?) {
        if playing == side {
            stop()
        } else {
            start(side, deviceID: deviceID)
        }
    }

    func stop() {
        engine?.stop()
        engine = nil
        sourceNode = nil
        playing = nil
    }

    private func start(_ side: Side, deviceID: AudioDeviceID?) {
        stop()

        let engine = AVAudioEngine()

        // Route the engine to the device the user is adjusting (defaults to the
        // system output if this fails).
        if let id = deviceID, let unit = engine.outputNode.audioUnit {
            var device = id
            AudioUnitSetProperty(unit,
                                 kAudioOutputUnitProperty_CurrentDevice,
                                 kAudioUnitScope_Global,
                                 0,
                                 &device,
                                 UInt32(MemoryLayout<AudioDeviceID>.size))
        }

        var sampleRate = engine.outputNode.outputFormat(forBus: 0).sampleRate
        if sampleRate <= 0 { sampleRate = 44100 }
        guard let format = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 2) else {
            errorText = "Couldn't create a stereo audio format."
            return
        }

        let noise = PinkNoise()
        let playLeft = side != .right
        let playRight = side != .left
        let gain: Float = 0.3

        let node = AVAudioSourceNode(format: format) { _, _, frameCount, bufferList -> OSStatus in
            let buffers = UnsafeMutableAudioBufferListPointer(bufferList)
            let pointers: [UnsafeMutablePointer<Float>?] = buffers.map { buffer in
                buffer.mData?.assumingMemoryBound(to: Float.self)
            }
            let frames = Int(frameCount)
            for frame in 0..<frames {
                let sample = noise.next() * gain
                for (index, pointer) in pointers.enumerated() {
                    guard let pointer = pointer else { continue }
                    let enabled = (index == 0 && playLeft) || (index == 1 && playRight)
                    pointer[frame] = enabled ? sample : 0
                }
            }
            return noErr
        }

        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: format)
        engine.mainMixerNode.outputVolume = 1

        do {
            try engine.start()
            self.engine = engine
            self.sourceNode = node
            playing = side
            errorText = nil
        } catch {
            errorText = "Couldn't start the test sound: \(error.localizedDescription)"
            playing = nil
        }
    }
}

/// Paul Kellet's pink-noise approximation (economy version).
final class PinkNoise {
    private var b0: Float = 0, b1: Float = 0, b2: Float = 0
    private var b3: Float = 0, b4: Float = 0, b5: Float = 0, b6: Float = 0

    func next() -> Float {
        let white = Float.random(in: -1...1)
        b0 = 0.99886 * b0 + white * 0.0555179
        b1 = 0.99332 * b1 + white * 0.0750759
        b2 = 0.96900 * b2 + white * 0.1538520
        b3 = 0.86650 * b3 + white * 0.3104856
        b4 = 0.55000 * b4 + white * 0.5329522
        b5 = -0.7616 * b5 - white * 0.0168980
        let pink = b0 + b1 + b2 + b3 + b4 + b5 + b6 + white * 0.5362
        b6 = white * 0.115926
        return pink * 0.11
    }
}
