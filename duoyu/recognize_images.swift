import Foundation
import Vision

var output: [String] = []
for path in CommandLine.arguments.dropFirst() {
    do {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.recognitionLanguages = ["zh-Hans", "en-US"]
        try VNImageRequestHandler(data: data).perform([request])
        let text = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
        output.append(text)
    } catch {
        fputs("OCR failed for \(path): \(error)\n", stderr)
        exit(1)
    }
}
let encoded = try JSONSerialization.data(withJSONObject: output)
FileHandle.standardOutput.write(encoded)
