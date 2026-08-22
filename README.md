# OCR Document Logistics Engine

> **I got tired of watching documents get processed manually, so I built a system to do it for me.**

A multi-threaded document recognition and routing engine built around a real logistics workflow.

The engine watches for incoming PDFs, identifies what they contain using QR/barcode detection, PDF text extraction, and OCR, then determines the most likely document type and routes the file accordingly.

This started as a practical problem, not a portfolio project. The code grew as the problems did.

---

## What It Does

At its core, the system takes this:

```text
Messy pile of incoming PDFs
```

and tries to turn it into this:

```text
            Incoming PDFs
                  |
                  v
          Directory Watcher
                  |
                  v
            Task Queue
                  |
          +-------+-------+
          |               |
          v               v
     QR / Barcode       OCR Pipeline
      Fast Path          Slow Path
          |               |
          +-------+-------+
                  |
                  v
        Document Classification
                  |
                  v
         Heuristic Scoring
                  |
                  v
      Validate / Reject / Route
                  |
                  v
          Rename + Audit Log
```

The goal isn't just to **read text**.

The goal is to make a decision about **what the document actually is**, while avoiding the hundreds of ways a messy logistics document can produce the wrong answer.

---

## Why I Built It

I had practical exposure to logistics workflows where documents were constantly being scanned, moved around, identified, and processed.

A lot of that work was repetitive.

So naturally, my first thought was basically:

> *"Why are we doing this manually?"*

That became the reason for this project.

What started as document recognition gradually turned into a larger system involving concurrency, OCR, barcode processing, filesystem safety, heuristics, queue management, and runtime monitoring.

It is still very much a project built by someone who likes finding problems and then going way too far trying to solve them.

---

## Architecture

### 1. Ingestion

A live directory watcher monitors incoming files and places new work into a thread-safe queue.

This allows the system to handle continuous document drops instead of relying on a single batch-processing run.

### 2. Parallel Processing

Worker threads consume queued documents concurrently.

OCR is expensive, so keeping processing separate from the interface allows the terminal UI to remain responsive while documents are being processed.

### 3. Dual-Path Recognition

The engine uses two primary recognition paths.

**Fast path**

* QR / barcode detection using `pyzbar`
* Payload extraction
* Structured identifier detection

**Fallback path**

* PDF rendering with `PyMuPDF`
* Image preprocessing with `Pillow`
* Tesseract OCR
* Text extraction and heuristic analysis

The idea is simple:

> **Use the cheap, reliable signal first. Use OCR when you actually need it.**

---

## The Classification Engine

This is where the project gets a little more interesting.

The engine doesn't blindly search for a number and assume it must be a document ID.

Instead, it evaluates multiple signals and assigns weighted scores to possible matches.

For example:

```text
Potential identifier
        |
        +--> Regex match
        |
        +--> Keyword context
        |
        +--> Document type
        |
        +--> Numeric validity
        |
        +--> Negative patterns
        |
        +--> Known noise
        |
        v
     Score
        |
        v
Accept / Reject / Re-evaluate
```

This matters because logistics documents are full of numbers that **look** useful but aren't.

A six-digit number might be:

* a document ID
* a postal code
* a phone number
* a GST-related value
* a random reference number

The engine therefore uses weighted patterns, negative lookbehinds, keyword context, and numeric filtering to reduce false positives.

---

## False-Positive Filtering

One of the recurring problems during development was:

> **OCR successfully reading the wrong thing.**

Perfect OCR isn't enough.

A recognizer can correctly read text and still make the wrong classification.

So the engine deliberately rejects known noise patterns and uses surrounding context to determine whether a candidate identifier is actually plausible.

This is one of the main reasons the project evolved beyond a simple:

```python
text = ocr(pdf)
regex.search(text)
```

script.

---

## Runtime Interface

The engine includes a live `Rich` terminal interface for monitoring processing in real time.

It tracks things such as:

* active workers
* queue depth
* processing state
* recent results
* processing history
* success / rejection information
* runtime events

The interface exists for a practical reason:

**when something goes wrong, I want to know what the system is doing without opening another terminal and guessing.**

---

## Operational Modes

The engine includes several modes designed around real document-handling problems.

|      Key      | Mode           | Purpose                                              |
| :-----------: | -------------- | ---------------------------------------------------- |
| `P` / `Space` | Pause / Resume | Pause directory polling and task assignment          |
|      `S`      | Stitch         | Merge sequential multi-page scan drops               |
|      `F`      | Failsafe       | Append uncertain pages to the last verified document |
|      `I`      | Panic Stop     | Abort active processing and pause the queue          |
|      `Q`      | Quit           | Shut down workers cleanly                            |

These aren't just UI features. They exist because real input isn't always neat.

---

## Filesystem Safety

Document processing can become messy when files are being moved while another process is still touching them.

The engine therefore uses atomic filesystem operations where appropriate to reduce the chance of partially processed or corrupted output.

Operations are also recorded in an audit log.

Example:

```text
[2026-08-18 00:15:30] ID:INV-849201 TYPE:INVOICE SIZE:420.5KB METHOD:AUTO
[2026-08-18 00:16:02] ID:LR-102938  TYPE:LR      SIZE:1.1MB   METHOD:AUTO
```

---

## A Note About the Configuration

This isn't a universal OCR package.

The classification logic is intentionally designed around **known document schemas, identifier formats, keywords, and noise patterns**.

To adapt it to another workflow, the rules and configuration need to be calibrated for that environment.

For example:

```python
CONFIG: AppConfig = {
    "WORKER_THREADS": 2,
    "PREFERRED_PREFIX": "img_",
    "WATCH_EXTENSIONS": (".pdf",),
    "INVOICE_PREFIX": "INV-",
    "MIN_OCR_CONFIDENCE": 25.0,
}
```

Pattern rules can then be defined for specific document types:

```python
PatternRule(
    name="INVOICE",
    doc_type="INVOICE",
    priority=80,
    patterns=(
        r"(?i)\bINVOICE\s*(?:NO\.?)?\s*[:\-]?\s*(INV-\d{5,6})\b",
        r"(?<!\d)(INV-\d{5,6})(?!\d)",
    ),
    keywords=(
        "TAX INVOICE",
        "BILL TO",
        "ORIGINAL FOR RECIPIENT",
        "GSTIN",
    ),
)
```

This makes the engine a **framework for a workflow**, rather than a one-size-fits-all OCR script.

---

## Prerequisites

### Tesseract

Tesseract OCR must be installed separately.

**Debian / Ubuntu:**

```bash
sudo apt update
sudo apt install -y tesseract-ocr libzbar0
```

**Windows:**

Install Tesseract OCR and configure the executable path if required.

### Python

Python 3.10+ is recommended.

Install dependencies:

```bash
pip install pymupdf pillow pytesseract pyzbar rich
```

---

## Running It

Launch the engine:

```bash
python crom.py
```

Start watch mode, select the working directory, and begin feeding documents into the monitored folder.

The system handles the rest:

```text
Detect
  ↓
Queue
  ↓
Process
  ↓
Classify
  ↓
Validate
  ↓
Route
  ↓
Log
```

---

## Project Structure

The public repository contains the core processing engine and supporting logic.

Proprietary vendor schemas, client-specific routing rules, and sensitive document information have been removed or generalized for public release.

The goal is to demonstrate the engineering behind the system without exposing private operational data.

---

## What I Learned Building This

This project ended up teaching me much more than OCR.

I got to work through problems involving:

* concurrency
* queues and worker coordination
* OCR reliability
* document classification
* regex design
* false-positive handling
* filesystem race conditions
* process monitoring
* terminal UI design
* error handling
* designing around imperfect real-world input

Most importantly, it taught me that **getting the computer to read something is only half the problem.**

The harder part is getting it to make the *right decision*.

---

## Status

This is an evolving project.

The architecture and core engine are usable, but document-specific rules and heuristics are inherently tied to the workflow they were designed for.

That is part of the project.

It was never meant to be perfect.

It was meant to solve a real problem, survive messy input, and keep getting better.

---

## License

Distributed under the MIT License.

---

### `REXON // build it, break it, understand it.`
