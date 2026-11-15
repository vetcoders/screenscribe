@preconcurrency import AVFoundation
import CoreMedia
import CoreVideo
import Foundation

/// Generates tiny real MP4/MOV fixtures on disk for validator and upload tests.
enum SyntheticVideoGenerator {
    static func writeMovie(
        to destination: URL,
        durationSeconds: Double = 1.5,
        includeVideo: Bool = true,
        audioTrackCount: Int = 1,
        fileType: AVFileType = .mp4
    ) async throws {
        try? FileManager.default.removeItem(at: destination)
        let writer = try AVAssetWriter(outputURL: destination, fileType: fileType)

        var videoInput: AVAssetWriterInput?
        var pixelAdaptor: AVAssetWriterInputPixelBufferAdaptor?
        if includeVideo {
            let settings: [String: Any] = [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: 160,
                AVVideoHeightKey: 120,
            ]
            let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
            input.expectsMediaDataInRealTime = false
            let adaptor = AVAssetWriterInputPixelBufferAdaptor(
                assetWriterInput: input,
                sourcePixelBufferAttributes: [
                    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                    kCVPixelBufferWidthKey as String: 160,
                    kCVPixelBufferHeightKey as String: 120,
                ]
            )
            writer.add(input)
            videoInput = input
            pixelAdaptor = adaptor
        }

        var audioInputs: [AVAssetWriterInput] = []
        for _ in 0..<max(0, audioTrackCount) {
            let audioSettings: [String: Any] = [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 44_100,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: 64_000,
            ]
            let input = AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)
            input.expectsMediaDataInRealTime = false
            writer.add(input)
            audioInputs.append(input)
        }

        guard writer.startWriting() else {
            throw writer.error ?? URLError(.cannotCreateFile)
        }
        writer.startSession(atSourceTime: .zero)

        if let videoInput, let pixelAdaptor {
            let fps = 10
            let totalFrames = max(1, Int((durationSeconds * Double(fps)).rounded()))
            for frameIndex in 0..<totalFrames {
                while !videoInput.isReadyForMoreMediaData {
                    try await Task.sleep(for: .milliseconds(5))
                }
                var pixelBuffer: CVPixelBuffer?
                CVPixelBufferCreate(
                    kCFAllocatorDefault,
                    160,
                    120,
                    kCVPixelFormatType_32BGRA,
                    nil,
                    &pixelBuffer
                )
                if let pixelBuffer {
                    let pts = CMTime(value: CMTimeValue(frameIndex), timescale: CMTimeScale(fps))
                    _ = pixelAdaptor.append(pixelBuffer, withPresentationTime: pts)
                }
            }
            videoInput.markAsFinished()
        }

        for audioInput in audioInputs {
            let sampleRate = 44_100
            let totalSamples = max(1024, Int((durationSeconds * Double(sampleRate)).rounded()))
            if let sampleBuffer = try makeSilentPCMBuffer(sampleCount: totalSamples, sampleRate: sampleRate) {
                while !audioInput.isReadyForMoreMediaData {
                    try await Task.sleep(for: .milliseconds(5))
                }
                _ = audioInput.append(sampleBuffer)
            }
            audioInput.markAsFinished()
        }

        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            writer.finishWriting {
                continuation.resume()
            }
        }
        if writer.status != .completed {
            throw writer.error ?? URLError(.cannotWriteToFile)
        }
    }

    private static func makeSilentPCMBuffer(sampleCount: Int, sampleRate: Int) throws -> CMSampleBuffer? {
        var asbd = AudioStreamBasicDescription(
            mSampleRate: Float64(sampleRate),
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked,
            mBytesPerPacket: 2,
            mFramesPerPacket: 1,
            mBytesPerFrame: 2,
            mChannelsPerFrame: 1,
            mBitsPerChannel: 16,
            mReserved: 0
        )
        var formatDescription: CMAudioFormatDescription?
        let status = CMAudioFormatDescriptionCreate(
            allocator: kCFAllocatorDefault,
            asbd: &asbd,
            layoutSize: 0,
            layout: nil,
            magicCookieSize: 0,
            magicCookie: nil,
            extensions: nil,
            formatDescriptionOut: &formatDescription
        )
        guard status == noErr, let formatDescription else { return nil }

        let byteLength = sampleCount * 2
        var blockBuffer: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault,
            memoryBlock: nil,
            blockLength: byteLength,
            blockAllocator: kCFAllocatorDefault,
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: byteLength,
            flags: kCMBlockBufferAssureMemoryNowFlag,
            blockBufferOut: &blockBuffer
        ) == kCMBlockBufferNoErr, let blockBuffer else {
            return nil
        }
        CMBlockBufferFillDataBytes(with: 0, blockBuffer: blockBuffer, offsetIntoDestination: 0, dataLength: byteLength)

        var sampleBuffer: CMSampleBuffer?
        let createStatus = CMAudioSampleBufferCreateReadyWithPacketDescriptions(
            allocator: kCFAllocatorDefault,
            dataBuffer: blockBuffer,
            formatDescription: formatDescription,
            sampleCount: sampleCount,
            presentationTimeStamp: .zero,
            packetDescriptions: nil,
            sampleBufferOut: &sampleBuffer
        )
        guard createStatus == noErr else { return nil }
        return sampleBuffer
    }
}
