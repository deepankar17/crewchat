// Assembles the tutorial video from slides and narration, with AVFoundation (part of macOS).
//
//   swiftc -O compose.swift -o compose && ./compose timeline.json
//
// timeline.json: {width, height, fps, fade, output, scenes: [{image, duration}], audio: [{path, start}]}
// Each slide drifts slowly closer while it is on screen, and the next one fades in over `fade`
// seconds. The narration clips are laid on one audio track at their start times.
import AVFoundation
import CoreGraphics
import Foundation
import ImageIO

struct Scene: Decodable { let image: String; let duration: Double }
struct Clip: Decodable { let path: String; let start: Double }
struct Timeline: Decodable {
    let width: Int, height: Int, fps: Int
    let fade: Double
    let output: String
    let scenes: [Scene]
    let audio: [Clip]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func loadImage(_ path: String) -> CGImage {
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { fail("cannot read \(path)") }
    return image
}

let args = CommandLine.arguments
guard args.count == 2, let data = FileManager.default.contents(atPath: args[1]) else { fail("usage: compose timeline.json") }
let timeline: Timeline
do { timeline = try JSONDecoder().decode(Timeline.self, from: data) } catch { fail("bad timeline: \(error)") }

let width = timeline.width, height = timeline.height, fps = timeline.fps
let output = URL(fileURLWithPath: timeline.output)
let silent = output.deletingPathExtension().appendingPathExtension("video-only.mov")
try? FileManager.default.removeItem(at: silent)
try? FileManager.default.removeItem(at: output)

// 1. The pictures: one frame at a time, drawn with Core Graphics, encoded as H.264.
let writer = try! AVAssetWriter(outputURL: silent, fileType: .mov)
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height,
    AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 10_000_000, AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel],
])
input.expectsMediaDataInRealTime = false
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
    kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height,
])
writer.add(input)
writer.startWriting()
writer.startSession(atSourceTime: .zero)

let images = timeline.scenes.map { loadImage($0.image) }
var starts: [Double] = []
var total = 0.0
for scene in timeline.scenes { starts.append(total); total += scene.duration }
let frames = Int((total * Double(fps)).rounded(.up))

func draw(_ image: CGImage, in context: CGContext, progress: Double, alpha: CGFloat) {
    let zoom = 1.0 + 0.035 * progress  // a slow push in
    let w = Double(width) * zoom, h = Double(height) * zoom
    context.setAlpha(alpha)
    context.draw(image, in: CGRect(x: (Double(width) - w) / 2, y: (Double(height) - h) / 2, width: w, height: h))
}

for frame in 0..<frames {
    let t = Double(frame) / Double(fps)
    var index = starts.lastIndex(where: { $0 <= t }) ?? 0
    index = min(index, images.count - 1)
    let into = t - starts[index]
    while !input.isReadyForMoreMediaData { Thread.sleep(forTimeInterval: 0.005) }
    var buffer: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &buffer)
    guard let pixels = buffer else { fail("no pixel buffer") }
    CVPixelBufferLockBaseAddress(pixels, [])
    let context = CGContext(data: CVPixelBufferGetBaseAddress(pixels), width: width, height: height, bitsPerComponent: 8,
                            bytesPerRow: CVPixelBufferGetBytesPerRow(pixels), space: CGColorSpaceCreateDeviceRGB(),
                            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)!
    context.interpolationQuality = .high
    context.setFillColor(CGColor(red: 0.97, green: 0.96, blue: 0.94, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    if index > 0 && into < timeline.fade {
        let previous = timeline.scenes[index - 1].duration
        draw(images[index - 1], in: context, progress: min(1, (previous + into) / previous), alpha: 1)
        draw(images[index], in: context, progress: into / timeline.scenes[index].duration, alpha: CGFloat(into / timeline.fade))
    } else {
        draw(images[index], in: context, progress: into / timeline.scenes[index].duration, alpha: 1)
    }
    CVPixelBufferUnlockBaseAddress(pixels, [])
    adaptor.append(pixels, withPresentationTime: CMTime(value: CMTimeValue(frame), timescale: CMTimeScale(fps)))
}
input.markAsFinished()
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
done.wait()
if writer.status != .completed { fail("video: \(String(describing: writer.error))") }

// 2. Add the narration and write an MP4.
let composition = AVMutableComposition()
let video = AVURLAsset(url: silent)
let videoTrack = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)!
let sourceVideo = video.tracks(withMediaType: .video)[0]
try! videoTrack.insertTimeRange(CMTimeRange(start: .zero, duration: video.duration), of: sourceVideo, at: .zero)
let audioTrack = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)!
for clip in timeline.audio {
    let asset = AVURLAsset(url: URL(fileURLWithPath: clip.path))
    guard let track = asset.tracks(withMediaType: .audio).first else { fail("no audio in \(clip.path)") }
    try! audioTrack.insertTimeRange(CMTimeRange(start: .zero, duration: asset.duration), of: track,
                                    at: CMTime(seconds: clip.start, preferredTimescale: 600))
}
guard let export = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetHighestQuality) else { fail("no exporter") }
export.outputURL = output
export.outputFileType = .mp4
export.exportAsynchronously { done.signal() }
done.wait()
if export.status != .completed { fail("export: \(String(describing: export.error))") }
try? FileManager.default.removeItem(at: silent)
print("Wrote \(output.path) (\(String(format: "%.1f", total)) s, \(frames) frames)")
