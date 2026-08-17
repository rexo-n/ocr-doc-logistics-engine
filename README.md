# OCR Document Logistics Engine (CROM)

A high-throughput, multi-threaded document recognition pipeline. This engine monitors a directory for incoming logistics PDFs (like invoices and delivery challans), uses computer vision to extract and identify the documents, and automatically routes and renames them based on strict regex heuristics.

### ⚙️ Core Architecture
* **Live Directory Watcher:** Real-time polling with queue management to handle bulk document dumps.
* **Multi-Threaded Workers:** Concurrent processing pool to handle heavy OCR rendering without UI blocking.
* **Hybrid Vision System:** 
  * Fast-path barcode/QR decoding (`pyzbar`) with JWT payload parsing.
  * Fallback OCR slow-path (`Tesseract`) with image preprocessing (sharpening, contrast, noise reduction via `Pillow`).
  * `PyMuPDF` (fitz) for PDF layout analysis and text layer extraction.
* **Rich TUI (Terminal UI):** Live telemetry dashboard built with `Rich`, tracking worker states, queue depth, win-rates, and recent history.

### The "Brain" (Heuristics Engine)
The engine doesn't just blindly read text; it scores it. It uses weighted regex patterns with negative lookbehinds to prevent false positives (e.g., rejecting 6-digit postal codes that look like tracking numbers).

### Usage
*This repository contains the core engine logic. Proprietary vendor schemas and client routing rules have been sanitized for public portfolio display.*
