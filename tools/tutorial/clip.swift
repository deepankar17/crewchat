// Turns recorded frames into an MP4 clip (H.264, 30 frames a second), holding each frame for
// its recorded duration. Used by tools/demo_recordings.py.
//
//   swiftc -O clip.swift -o clip && ./clip frames.json
//
// frames.json: {"output": path, "frames": [{"image": png path, "duration": seconds}]}
import AVFoundation
import CoreGraphics
import Foundation
import ImageIO

struct Frame: Decodable { let image: String; let duration: Double }
struct Job: Decodable { let output: String; let frames: [Frame] }

func fail(_ message: String) -> Never { FileHandle.standardError.write((message + "\n").data(using: .utf8)!); exit(1) }
func load(_ path: String) -> CGImage {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { fail("cannot read \(path)") }
    return img
}

guard CommandLine.arguments.count == 2, let data = FileManager.default.contents(atPath: CommandLine.arguments[1]),
      let job = try? JSONDecoder().decode(Job.self, from: data), !job.frames.isEmpty else { fail("usage: clip frames.json") }
let first = load(job.frames[0].image)
let width = first.width / 2 * 2, height = first.height / 2 * 2, fps = 30
let url = URL(fileURLWithPath: job.output)
try? FileManager.default.removeItem(at: url)
let writer = try! AVAssetWriter(outputURL: url, fileType: .mp4)
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height,
    AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 8_000_000]])
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
    kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height])
writer.add(input)
writer.startWriting()
writer.startSession(atSourceTime: .zero)

var t = 0.0, frame = 0
for f in job.frames {
    let image = load(f.image)
    t += f.duration
    var buffer: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &buffer)
    guard let pixels = buffer else { fail("no pixel buffer") }
    CVPixelBufferLockBaseAddress(pixels, [])
    let ctx = CGContext(data: CVPixelBufferGetBaseAddress(pixels), width: width, height: height, bitsPerComponent: 8,
                        bytesPerRow: CVPixelBufferGetBytesPerRow(pixels), space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)!
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    CVPixelBufferUnlockBaseAddress(pixels, [])
    // Hold this picture until the next frame is due.
    while Double(frame) / Double(fps) < t {
        while !input.isReadyForMoreMediaData { Thread.sleep(forTimeInterval: 0.002) }
        adaptor.append(pixels, withPresentationTime: CMTime(value: CMTimeValue(frame), timescale: CMTimeScale(fps)))
        frame += 1
    }
}
input.markAsFinished()
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
done.wait()
if writer.status != .completed { fail("clip: \(String(describing: writer.error))") }
print("  media/clips/\(url.lastPathComponent)  \(String(format: "%.1f", t))s  \(width)x\(height)")
