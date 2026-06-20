"""Apple Vision Framework OCR via Swift subprocess.
No extra Python packages needed — uses macOS built-in Vision.framework.
"""
from __future__ import annotations

import subprocess
import tempfile
import os
import re
from pathlib import Path

# Swift script that calls VNRecognizeTextRequest
_SWIFT_OCR = r"""
import Vision
import Foundation
import CoreGraphics
import ImageIO
import CoreImage

let path = CommandLine.arguments[1]
let fileURL = URL(fileURLWithPath: path)

// Try CGImageSource with URL (handles HEIC natively on macOS 10.13+)
func loadCGImage(from url: URL) -> CGImage? {
    if let src = CGImageSourceCreateWithURL(url as CFURL, nil),
       let img = CGImageSourceCreateImageAtIndex(src, 0, nil) {
        return img
    }
    // Fallback: CIImage -> CGImage
    let ctx = CIContext()
    if let ci = CIImage(contentsOf: url),
       let cg = ctx.createCGImage(ci, from: ci.extent) {
        return cg
    }
    return nil
}

guard let img = loadCGImage(from: fileURL) else {
    fputs("ERROR: cannot load \(path)\n", stderr)
    exit(1)
}

var lines: [String] = []
let sema = DispatchSemaphore(value: 0)

let req = VNRecognizeTextRequest { r, _ in
    let obs = (r.results as? [VNRecognizedTextObservation] ?? [])
        .sorted { $0.boundingBox.minY > $1.boundingBox.minY }
    lines = obs.compactMap { $0.topCandidates(1).first?.string }
    sema.signal()
}
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
req.recognitionLanguages = ["en-US", "ko-KR"]

try? VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
sema.wait()
print(lines.joined(separator: "\n"))
"""

# Cache the temp script path so we don't recreate it every call
_SCRIPT_PATH: str | None = None

def _ensure_script() -> str:
    global _SCRIPT_PATH
    if _SCRIPT_PATH and Path(_SCRIPT_PATH).exists():
        return _SCRIPT_PATH
    tmp = tempfile.NamedTemporaryFile(suffix=".swift", delete=False, mode="w")
    tmp.write(_SWIFT_OCR)
    tmp.close()
    _SCRIPT_PATH = tmp.name
    return _SCRIPT_PATH


def ocr_image(image_path: str) -> list[str]:
    """Run Apple Vision OCR on an image file.

    Returns a list of text lines sorted top-to-bottom.
    Supports: JPEG, PNG, HEIC, TIFF, BMP, GIF.
    """
    path = Path(image_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {path}")

    # Vision.framework handles HEIC natively — no conversion needed.
    # sips fallback only if Vision fails (e.g. corrupted file).
    work_path = str(path)
    _tmp_jpeg: str | None = None

    script = _ensure_script()
    result = subprocess.run(
        ["swift", script, work_path],
        capture_output=True, text=True, timeout=30,
    )

    # If Vision failed on HEIC, retry after sips conversion
    if result.returncode != 0 and path.suffix.lower() in (".heic", ".heif"):
        try:
            import tempfile as _tf
            tmp = _tf.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.close()
            _tmp_jpeg = tmp.name
            conv = subprocess.run(
                ["sips", "-s", "format", "jpeg", str(path), "--out", _tmp_jpeg],
                capture_output=True,
            )
            if conv.returncode == 0:
                result = subprocess.run(
                    ["swift", script, _tmp_jpeg],
                    capture_output=True, text=True, timeout=30,
                )
        except Exception:
            pass

    if _tmp_jpeg:
        try: os.unlink(_tmp_jpeg)
        except Exception: pass

    if result.returncode != 0:
        raise RuntimeError(f"OCR failed: {result.stderr.strip()}")

    lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    return lines


def ocr_image_text(image_path: str) -> str:
    """Convenience wrapper — returns full text as single string."""
    return "\n".join(ocr_image(image_path))
