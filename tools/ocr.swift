import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else { exit(0) }
guard let img = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let tiff = img.tiffRepresentation,
      let bmp = NSBitmapImageRep(data: tiff),
      let cg = bmp.cgImage else { exit(0) }
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = true
try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
let lines = (req.results ?? []).compactMap { ($0 as? VNRecognizedTextObservation)?.topCandidates(1).first?.string }
print(lines.joined(separator: "\n"))
