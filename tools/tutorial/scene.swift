// Renders one tutorial scene as an MP4 of an exact length: a slide, with (optionally) a recorded
// clip playing in a frame on it, timed to the narration. A silent stereo track is included,
// because video editors such as Google Vids refuse video with no sound track.
//
//   swiftc -O scene.swift -o scene && ./scene job.json
//
// job.json: {"background": png, "duration": seconds, "output": mp4,
//            "clip": mp4 or null, "rect": [x, y, w, h] (top-left origin, 1920x1080),
//            "lead": seconds before the clip starts, "tail": seconds the last frame holds,
//            "silence": AAC (.m4a) silence at least as long as the scene}
import AVFoundation
import CoreGraphics
import CoreImage
import Foundation
import ImageIO

struct Job: Decodable {
    let background: String, duration: Double, output: String
    let clip: String?, rect: [Double]?, lead: Double?, tail: Double?, silence: String
}

func fail(_ m: String) -> Never { FileHandle.standardError.write((m + "\n").data(using: .utf8)!); exit(1) }
guard CommandLine.arguments.count == 2, let data = FileManager.default.contents(atPath: CommandLine.arguments[1]),
      let job = try? JSONDecoder().decode(Job.self, from: data) else { fail("usage: scene job.json") }

let W = 1920, H = 1080, fps = 30
guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: job.background) as CFURL, nil),
      let background = CGImageSourceCreateImageAtIndex(src, 0, nil) else { fail("cannot read background") }
let frames = Int((job.duration * Double(fps)).rounded(.up))

// The clip, read frame by frame in order.
var reader: AVAssetReader?
var output: AVAssetReaderTrackOutput?
var clipLength = 0.0
if let path = job.clip {
    let asset = AVURLAsset(url: URL(fileURLWithPath: path))
    clipLength = asset.duration.seconds
    reader = try! AVAssetReader(asset: asset)
    output = AVAssetReaderTrackOutput(track: asset.tracks(withMediaType: .video)[0],
                                      outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
    reader!.add(output!)
    reader!.startReading()
}
let lead = job.lead ?? 0.4, tail = job.tail ?? 0.8
let playFor = max(1.0, job.duration - lead - tail)
let speed = clipLength > 0 ? min(2.6, max(0.5, clipLength / playFor)) : 1
let ci = CIContext()
var current: CGImage?
var nextSample: CMSampleBuffer?
func advance(to t: Double) {
    // Move forward until the newest frame at or before clip time t is current.
    while true {
        if nextSample == nil { nextSample = output?.copyNextSampleBuffer() }
        guard let s = nextSample else { return }
        if CMSampleBufferGetPresentationTimeStamp(s).seconds > t && current != nil { return }
        if let px = CMSampleBufferGetImageBuffer(s) {
            current = ci.createCGImage(CIImage(cvPixelBuffer: px), from: CGRect(x: 0, y: 0, width: CVPixelBufferGetWidth(px), height: CVPixelBufferGetHeight(px)))
        }
        nextSample = nil
    }
}

let url = URL(fileURLWithPath: job.output)
let silent = url.deletingPathExtension().appendingPathExtension("tmp.mov")
try? FileManager.default.removeItem(at: silent)
let writer = try! AVAssetWriter(outputURL: silent, fileType: .mov)
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: W, AVVideoHeightKey: H,
    // A still slide needs far fewer bits than a recording; this keeps every scene under 10 MB.
    AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: job.clip == nil ? 3_000_000 : 6_000_000]])
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA, kCVPixelBufferWidthKey as String: W, kCVPixelBufferHeightKey as String: H])
writer.add(input)
writer.startWriting()
writer.startSession(atSourceTime: .zero)

for f in 0..<frames {
    let t = Double(f) / Double(fps)
    var buffer: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &buffer)
    let px = buffer!
    CVPixelBufferLockBaseAddress(px, [])
    let ctx = CGContext(data: CVPixelBufferGetBaseAddress(px), width: W, height: H, bitsPerComponent: 8,
                        bytesPerRow: CVPixelBufferGetBytesPerRow(px), space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)!
    ctx.interpolationQuality = .high
    if job.clip == nil {
        // A still slide drifts slowly closer.
        let zoom = 1.0 + 0.03 * t / job.duration
        let w = Double(W) * zoom, h = Double(H) * zoom
        ctx.draw(background, in: CGRect(x: (Double(W) - w) / 2, y: (Double(H) - h) / 2, width: w, height: h))
    } else {
        ctx.draw(background, in: CGRect(x: 0, y: 0, width: W, height: H))
        advance(to: max(0, (t - lead) * speed))
        if let frame = current, let r = job.rect {
            let rect = CGRect(x: r[0], y: Double(H) - r[1] - r[3], width: r[2], height: r[3])  // to bottom-left origin
            ctx.saveGState()
            ctx.addPath(CGPath(roundedRect: rect, cornerWidth: 18, cornerHeight: 18, transform: nil))
            ctx.clip()
            ctx.draw(frame, in: rect)
            ctx.restoreGState()
        }
    }
    if t < 0.3 {  // a soft start, so cuts between scenes are not abrupt
        ctx.setFillColor(CGColor(red: 0.97, green: 0.96, blue: 0.94, alpha: CGFloat(1 - t / 0.3)))
        ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
    }
    CVPixelBufferUnlockBaseAddress(px, [])
    while !input.isReadyForMoreMediaData { Thread.sleep(forTimeInterval: 0.002) }
    adaptor.append(px, withPresentationTime: CMTime(value: CMTimeValue(f), timescale: CMTimeScale(fps)))
}
input.markAsFinished()
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
done.wait()

// Add the silent sound track and write the MP4.
let comp = AVMutableComposition()
let video = AVURLAsset(url: silent)
let length = CMTime(value: CMTimeValue(frames), timescale: CMTimeScale(fps))
try! comp.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)!
    .insertTimeRange(CMTimeRange(start: .zero, duration: length), of: video.tracks(withMediaType: .video)[0], at: .zero)
let quiet = AVURLAsset(url: URL(fileURLWithPath: job.silence))
try! comp.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)!
    .insertTimeRange(CMTimeRange(start: .zero, duration: length), of: quiet.tracks(withMediaType: .audio)[0], at: .zero)
try? FileManager.default.removeItem(at: url)
let ex = AVAssetExportSession(asset: comp, presetName: AVAssetExportPresetPassthrough)!
ex.outputURL = url
ex.outputFileType = .mp4
ex.exportAsynchronously { done.signal() }
done.wait()
try? FileManager.default.removeItem(at: silent)
if ex.status != .completed { fail("export failed") }
print(String(format: "  %@  %.1fs%@", url.lastPathComponent, job.duration,
             job.clip == nil ? "" : String(format: "  clip %.1fs at %.2fx", clipLength, speed)))
