#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CROM v10.3 — Automated Document Recognition & Renaming Pipeline
Real-time PDF watcher: QR / OCR extraction → document identification → auto-rename.
USES TESSERACT OCR PROGRAM IN WINDOWS
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Generator, List, Optional, Set, Sequence, Tuple, TypedDict

# =============================================================================
# Typed configuration
# =============================================================================

class AppConfig(TypedDict):
    APP_NAME:                str
    APP_VERSION:             str
    LOG_FILENAME:            str
    WORKER_THREADS:          int
    QUEUE_MAX_SIZE:          int
    FILE_LOCK_RETRIES:       int
    FILE_LOCK_DELAY:         float
    QUEUE_POLL_DELAY:        float
    OCR_ZOOM:                float
    RENDER_THRESHOLD:        int
    MIN_TEXT_FOR_FAST_ACCEPT: int
    PREFERRED_PREFIX:        str
    WATCH_EXTENSIONS:        Tuple[str, ...]
    TESSERACT_FAST:          str
    TESSERACT_ACCURATE:      str
    TESSERACT_CANDIDATES:    Tuple[str, ...]
    ENABLE_QR_PRIORITY:      bool
    ENABLE_OCR_FALLBACK:     bool
    LR_DIGITS:               int
    LR_PRIORITY_START:       Tuple[str, ...]
    INVOICE_PREFIX:          str
    QR_JWT_MIN_LEN:          int
    RENAMED_SUFFIX:          str
    MASTER_FILENAME:         str
    STITCH_TEMP_PREFIX:      str
    MIN_OCR_CONFIDENCE:      float

#CUSTOMIZE YOUR CONFIGURATIONS HERE ON FORWARD
CONFIG: AppConfig = {
    "APP_NAME":                 "CROM",
    "APP_VERSION":              "10.3",
    "LOG_FILENAME":             "crom_audit.log",
    "WORKER_THREADS":           2,
    "QUEUE_MAX_SIZE":           20,
    "FILE_LOCK_RETRIES":        6,
    "FILE_LOCK_DELAY":          0.6,
    "QUEUE_POLL_DELAY":         0.12,
    "OCR_ZOOM":                 4.0,
    "RENDER_THRESHOLD":         1600,
    "MIN_TEXT_FOR_FAST_ACCEPT": 35,
    "PREFERRED_PREFIX":         "img_",
    "WATCH_EXTENSIONS":         (".pdf",),
    "TESSERACT_FAST":           "--oem 1 --psm 6",
    "TESSERACT_ACCURATE":       "--oem 1 --psm 11",
    "TESSERACT_CANDIDATES": (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ),
    "ENABLE_QR_PRIORITY":       True,
    "ENABLE_OCR_FALLBACK":      True,
    "LR_DIGITS":                6,
    "LR_PRIORITY_START":        ("1", "2", "3"),
    "INVOICE_PREFIX":           "INV-",
    "QR_JWT_MIN_LEN":           16,
    "RENAMED_SUFFIX":           ".pdf",
    "MASTER_FILENAME":          "Master.pdf",
    # Prefix does NOT start with "img_" — crash-orphaned temp files
    # are therefore invisible to the directory watcher.
    "STITCH_TEMP_PREFIX":       "_stitch_tmp_",
    # Minimum OCR confidence required to auto-rename. Results below this
    # threshold are flagged for manual review instead. QR results always
    # pass (confidence is always 100.0 from the QR path).
    "MIN_OCR_CONFIDENCE":       25.0,
}

# =============================================================================
# Dependency bootstrap — only when run as __main__
# =============================================================================

_REQUIRED: dict[str, str] = {
    "rich": "rich", "fitz": "pymupdf", "PIL": "pillow",
    "pytesseract": "pytesseract", "pyzbar": "pyzbar",
}


def _bootstrap() -> None:
    missing = [pip for mod, pip in _REQUIRED.items() if not _available(mod)]
    if not missing:
        return
    print(f"[CROM] Installing: {', '.join(missing)}")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", *missing],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Auto-install failed. Run: pip install {' '.join(missing)}")


def _available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


# =============================================================================
# Late imports
# =============================================================================

import fitz                                                     # type: ignore
from PIL import Image, ImageEnhance, ImageFilter, ImageOps      # type: ignore
import pytesseract                                              # type: ignore
from pyzbar.pyzbar import decode as zbar_decode                 # type: ignore
import tkinter as tk
from tkinter import filedialog

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

# =============================================================================
# Platform setup
# =============================================================================

if os.name == "nt":
    os.system("")  # enable ANSI escape codes on Windows

try:
    import msvcrt   # type: ignore
except ImportError:
    msvcrt = None   # type: ignore[assignment]

try:
    import select
except ImportError:
    select = None   # type: ignore[assignment]

# =============================================================================
# Tesseract setup
# =============================================================================

def _configure_tesseract() -> None:
    candidates: List[Path] = []
    env = os.environ.get("TESSERACT_CMD")
    if env:
        candidates.append(Path(env))
    candidates.extend(Path(p) for p in CONFIG["TESSERACT_CANDIDATES"])
    for p in candidates:
        if p.exists():
            pytesseract.pytesseract.tesseract_cmd = str(p)
            return
    import warnings
    warnings.warn(
        "Tesseract not found. Set TESSERACT_CMD env var to the full path.",
        RuntimeWarning, stacklevel=2,
    )


_configure_tesseract()

# =============================================================================
# Data models
# =============================================================================

@dataclass(frozen=True)
class PatternRule:
    name:     str
    doc_type: str
    patterns: Tuple[str, ...]
    keywords: Tuple[str, ...] = ()
    priority: int = 0


@dataclass(frozen=True)
class CompiledRule:
    rule:     PatternRule
    compiled: Tuple[re.Pattern[str], ...]


@dataclass
class EventRecord:
    time:    str
    level:   str
    message: str


@dataclass
class HistoryRecord:
    time:     str
    doc_type: str
    doc_id:   str
    size:     str
    status:   str


@dataclass
class DetectionResult:
    doc_type:   Optional[str]
    doc_id:     Optional[str]
    confidence: float = 0.0
    source:     str   = "unknown"

# =============================================================================
# Pattern rules — compiled once at import
# =============================================================================

_RAW_RULES: Tuple[PatternRule, ...] = (
    PatternRule(
        name="LR_LINKED",
        doc_type="LR",
        priority=100,
        patterns=(
            r"(?i)\bLR\s*[-_.]?\s*([1-9]\d{5})\b",
            r"(?i)\bLR\s*NO\.?\s*[:\-]?\s*([1-9]\d{5})\b",
            r"(?i)\bLR[\s\-:]*([1-9]\d{5})\b",
        ),
        keywords=(
            "CONSIGNMENT NOTE", "TRANSPORTER COPY", "LR NO", 
            "LOGISTICS", "WAYBILL", "EPOD COPY", "SUPPLY CHAIN",
        ),
    ),
    # Invoice IDs: 4163 + exactly 5-6 digits (9-10 chars total).
    # (?!\d) prevents "416381665" absorbing a trailing "20" into "41638166520".
    PatternRule(
        name="INVOICE",
        doc_type="INVOICE",
        priority=80,
        patterns=(
            r"(?i)\bINVOICE\s*(?:SR\.?\s*NO\.?|NO\.?|NUMBER|#)?\s*[:\-]?\s*("
            + re.escape(CONFIG["INVOICE_PREFIX"]) + r"\d{5,6})(?!\d)",
            r"(?i)\bSR\.?\s*NO\.?\s*[:\-]?\s*("
            + re.escape(CONFIG["INVOICE_PREFIX"]) + r"\d{5,6})(?!\d)",
        ),
        keywords=(
            "TAX INVOICE", "BILL TO", "ORIGINAL FOR RECIPIENT",
            "DUPLICATE FOR TRANSPORTER", "GSTIN",
        ),
    ),
    PatternRule(
        name="REPL_CHALLAN",
        doc_type="REPL_CHALLAN",
        priority=70,
        patterns=(
            r"(?i)\bReplacement\s+Challan\s*(?:Sr\.?\s*No\.?|No\.?|#|:)?\s*[:\-]?\s*(RC\d{6})\b",
            r"(?<!\d)(RC\d{6})(?!\d)",
        ),
        keywords=(
            "REPLACEMENT CHALLAN", "REPL CHALLAN", "REPLACEMENT", "CHALLAN",
        ),
    ),
    PatternRule(
        name="DELIV_CHALLAN",
        doc_type="DELIV_CHALLAN",
        priority=60,
        patterns=(
            r"(?i)\bDelivery\s+Challan\s*(?:Sr\.?\s*No\.?|No\.?|#|:)?\s*[:\-]?\s*(DC\d{6})\b",
            r"(?<!\d)(DC\d{6})(?!\d)",
        ),
        keywords=(
            "DELIVERY CHALLAN", "DELIV CHALLAN", "DISPATCH", 
        ),
    ),
)


def _compile_rules(rules: Tuple[PatternRule, ...]) -> Tuple[CompiledRule, ...]:
    return tuple(
        CompiledRule(
            rule=rule,
            compiled=tuple(re.compile(p) for p in rule.patterns),
        )
        for rule in sorted(rules, key=lambda r: r.priority, reverse=True)
    )


COMPILED_RULES: Tuple[CompiledRule, ...] = _compile_rules(_RAW_RULES)

# =============================================================================
# Status style helper
# =============================================================================

_STATUS_STYLE: dict[str, str] = {
    "AUTO": "green", "MERGED": "magenta", "MANUAL": "yellow", "SET": "cyan",
}


def status_style(s: str) -> str:
    return _STATUS_STYLE.get(s, "red")

# =============================================================================
# Utility functions
# =============================================================================

@contextmanager
def _suppress_stderr() -> Generator[None, None, None]:
    with open(os.devnull, "w") as devnull:
        old = os.dup(2)
        try:
            os.dup2(devnull.fileno(), 2)
            yield
        finally:
            os.dup2(old, 2)
            os.close(old)


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def now_full() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name or "")
    return re.sub(r"\s+", " ", cleaned).strip()[:180]


def pretty_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}PB"


def file_size(path: str) -> str:
    try:
        return pretty_size(os.path.getsize(path))
    except OSError:
        return "?"


def safe_unlink(path: Optional[str]) -> None:
    if path:
        with suppress(OSError):
            os.remove(path)


def unique_target_path(base_dir: str, doc_id: str, suffix: Optional[str] = None) -> str:
    sfx    = suffix if suffix is not None else CONFIG["RENAMED_SUFFIX"]
    target = Path(base_dir) / f"{doc_id}{sfx}"
    if not target.exists():
        return str(target)
    idx = 1
    while True:
        candidate = Path(base_dir) / f"{doc_id}_{idx}{sfx}"
        if not candidate.exists():
            return str(candidate)
        idx += 1

# =============================================================================
# Application state (thread-safe)
# =============================================================================

class AppState:
    _VALID_FLAGS: frozenset[str] = frozenset({"stitch", "failsafe", "paused"})

    def __init__(self) -> None:
        self._lock              = threading.RLock()
        self.logs:              Deque[EventRecord]  = deque(maxlen=40)
        self.history:           Deque[HistoryRecord] = deque(maxlen=30)
        self.stats:             dict[str, int] = {
            "processed": 0, "success": 0, "failed": 0,
            "manual": 0, "merged": 0, "skipped": 0,
        }
        self.worker_status:     dict[int, str] = {}
        self.current_directory: str            = "---"
        self.queue_depth:       int            = 0
        self.flags:             dict[str, bool] = dict.fromkeys(self._VALID_FLAGS, False)
        self.last_detected:     str            = "None"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "logs":              list(self.logs),
                "history":           list(self.history),
                "stats":             dict(self.stats),
                "worker_status":     dict(self.worker_status),
                "queue_depth":       self.queue_depth,
                "flags":             dict(self.flags),
                "current_directory": self.current_directory,
                "last_detected":     self.last_detected,
            }

    def log_event(self, level: str, message: str) -> None:
        with self._lock:
            self.logs.appendleft(EventRecord(now_hms(), level.upper(), message))

    def update_worker(self, wid: int, msg: str) -> None:
        with self._lock:
            self.worker_status[wid] = msg

    def set_flag(self, name: str, value: bool) -> None:
        if name not in self._VALID_FLAGS:
            raise KeyError(f"Unknown flag: {name!r}")
        with self._lock:
            self.flags[name] = value

    def toggle_flag(self, name: str) -> bool:
        if name not in self._VALID_FLAGS:
            raise KeyError(f"Unknown flag: {name!r}")
        with self._lock:
            self.flags[name] = not self.flags[name]
            return self.flags[name]

    def set_last_detected(self, value: str) -> None:
        with self._lock:
            self.last_detected = value

    def record_history(self, doc_type: str, doc_id: str, size: str, status: str) -> None:
        _MAP = {
            "AUTO": "success", "MERGED": "merged", "MANUAL": "manual",
            "FAIL": "failed",  "SKIP":   "skipped",
        }
        with self._lock:
            self.history.appendleft(
                HistoryRecord(now_hms(), doc_type[:16], doc_id[:32], size, status)
            )
            self.stats["processed"] += 1
            key = _MAP.get(status)
            if key:
                self.stats[key] += 1


STATE = AppState()

# =============================================================================
# Audit manager
# =============================================================================

class AuditManager:
    _log_path: Optional[str] = None

    @classmethod
    def initialize(cls, base_dir: str) -> None:
        if not base_dir:
            cls._log_path = None
            return
        path = Path(base_dir) / CONFIG["LOG_FILENAME"]
        try:
            path.touch(exist_ok=True)
            cls._log_path = str(path)
        except OSError as exc:
            STATE.log_event("ERROR", f"Cannot create audit log: {exc}")
            cls._log_path = None

    @classmethod
    def write_entry(cls, doc_id: str, doc_type: str, size: str, method: str = "AUTO") -> None:
        if cls._log_path:
            try:
                with open(cls._log_path, "a", encoding="utf-8") as fh:
                    fh.write(
                        f"[{now_full()}] ID:{doc_id:<24} TYPE:{doc_type:<12} "
                        f"SIZE:{size:<10} METHOD:{method}\n"
                    )
            except OSError as exc:
                STATE.log_event("ERROR", f"Audit write failed: {exc}")
        STATE.record_history(doc_type, doc_id, size, method)
        if method == "AUTO":
            STATE.log_event("SUCCESS", f"Renamed \u2192 {doc_id} ({doc_type})")
        elif method == "MERGED":
            STATE.log_event("SUCCESS", f"Merged into {doc_id}")

# =============================================================================
# Vision system
# =============================================================================

class VisionSystem:
    @staticmethod
    def render_page(file_path: str, page_index: int = 0) -> Optional[Image.Image]:
        try:
            doc = fitz.open(file_path)
            try:
                if not (0 <= page_index < doc.page_count):
                    return None
                page = doc[page_index]
                zoom = 3.0 if page.rect.width > CONFIG["RENDER_THRESHOLD"] else CONFIG["OCR_ZOOM"]
                pix  = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            finally:
                doc.close()
        except Exception:
            return None

# =============================================================================
# OCR engine
# =============================================================================

class OCREngine:

    @staticmethod
    def scan_qr(img: Image.Image) -> Optional[str]:
        try:
            objs = zbar_decode(img)
            if not objs:
                w, h  = img.size
                small = img.resize(
                    (max(180, w // 2), max(180, h // 2)), Image.Resampling.BILINEAR
                )
                objs = zbar_decode(ImageOps.grayscale(small))
            for obj in objs:
                payload = obj.data.decode("utf-8", errors="ignore").strip()
                if payload:
                    return payload
        except Exception:
            pass  # zbar raises on corrupt data — None is the correct fallback
        return None

    @staticmethod
    def _auto_orient(img: Image.Image) -> Image.Image:
        try:
            with _suppress_stderr():
                osd = pytesseract.image_to_osd(img)
            m = re.search(r"Rotate: (\d+)", osd)
            if m:
                angle = int(m.group(1))
                if angle:
                    return img.rotate(angle, expand=True)
        except Exception:
            pass  # OSD fails on low-quality images — original orientation is the safe fallback
        return img

    @staticmethod
    def _preprocess(img: Image.Image) -> List[Tuple[str, Image.Image, str]]:
        oriented = OCREngine._auto_orient(img)
        gray     = oriented.convert("L")
        sharp    = ImageEnhance.Sharpness(gray).enhance(1.7)
        high     = ImageEnhance.Contrast(sharp).enhance(2.1)
        denoise  = high.filter(ImageFilter.MedianFilter(size=3))
        return [
            ("fast",  high,    CONFIG["TESSERACT_FAST"]),
            ("clean", denoise, CONFIG["TESSERACT_ACCURATE"]),
        ]

    @staticmethod
    def extract_text(img: Image.Image) -> str:
        best = ""
        for label, prepared, cfg in OCREngine._preprocess(img):
            try:
                text = pytesseract.image_to_string(prepared, config=cfg).strip()
                if len(text) > len(best):
                    best = text
                if label == "fast" and len(text) >= CONFIG["MIN_TEXT_FOR_FAST_ACCEPT"]:
                    return text
            except Exception:
                continue
        return best

# =============================================================================
# QR / JWT parser
# =============================================================================

class QRParser:

    @staticmethod
    def _numeric_candidates(text: str) -> List[str]:
        if not text:
            return []
        parts = [p.strip() for p in re.split(r"\$\|\$|\|", text) if p.strip()]
        out: List[str] = []
        for part in parts:
            if re.fullmatch(r"\d+", part):
                out.append(part)
            else:
                out.extend(re.findall(r"\b\d+\b", part))
        return out

    @staticmethod
    def parse_lr(qr_text: str) -> Optional[Tuple[str, str]]:
        if not qr_text:
            return None
        candidates = QRParser._numeric_candidates(qr_text)
        target     = CONFIG["LR_DIGITS"]
        priority   = CONFIG["LR_PRIORITY_START"]

        def _best(pool: List[str]) -> str:
            preferred = [c for c in pool if c.startswith(priority)]
            return preferred[0] if preferred else pool[0]

        exact = [c for c in candidates if len(c) == target]
        if exact:
            return "LR", _best(exact)

        near = [c for c in candidates if target <= len(c) <= target + 2]
        if near:
            return "LR", _best(near)

        for pat in (
            r"(?i)\bLR[\s\-:]*([0-9]{6})\b",
            r"(?i)\bLR\s*NO\.?\s*[:\-]?\s*([0-9]{6})\b",
        ):
            m = re.search(pat, qr_text)
            if m:
                return "LR", m.group(1)

        return None

    @staticmethod
    def _decode_jwt(jwt_text: str) -> Optional[dict[str, Any]]:
        if not jwt_text or len(jwt_text) < CONFIG["QR_JWT_MIN_LEN"]:
            return None
        try:
            parts = jwt_text.split(".")
            if len(parts) < 2:
                return None
            seg  = parts[1]
            seg += "=" * (-len(seg) % 4)
            return json.loads(
                base64.urlsafe_b64decode(seg.encode()).decode("utf-8", errors="ignore")
            )
        except Exception:
            return None

    @staticmethod
    def _match_invoice(value: str) -> Optional[str]:
        r"""Match 1234 + 5-6 digits using (?!\d) not \b.

        \b won't stop "1234567890" because both sides are digits.
        (?!\d) rejects the whole token if a digit follows, so "1234567890"
        (1234 + 7 digits) fails entirely rather than being silently truncated.
        """
        prefix = CONFIG["INVOICE_PREFIX"]
        m = re.search(r"(?<!\d)(" + re.escape(prefix) + r"\d{5,6})(?!\d)", value)
        return m.group(1) if m else None

    @staticmethod
    def parse_invoice(qr_text: str) -> Optional[Tuple[str, str]]:
        if not qr_text:
            return None
        payload = QRParser._decode_jwt(qr_text)
        if not payload:
            return None
        for source in (payload.get("data"), payload):
            if isinstance(source, str):
                with suppress(Exception):
                    source = json.loads(source)
            if not isinstance(source, dict):
                continue
            raw = str(source.get("DocNo") or source.get("docno") or "").strip()
            if raw:
                result = QRParser._match_invoice(raw)
                if result:
                    return "INVOICE", result
        return None

    @staticmethod
    def parse(qr_text: str) -> Optional[Tuple[str, str]]:
        if not qr_text:
            return None
        return QRParser.parse_invoice(qr_text) or QRParser.parse_lr(qr_text)

    @staticmethod
    def decode_from_image(img: Image.Image) -> Optional[Tuple[str, str]]:
        raw = OCREngine.scan_qr(img)
        if not raw:
            return None
        return QRParser.parse(raw)

# =============================================================================
# Brain — OCR-based document identification
# =============================================================================

class BrainSystem:
    """Score-based document identification from OCR text.

    ALL class-level attributes are defined together here — this prevents
    AttributeError crashes that occur when patches add attributes in the
    wrong order relative to method definitions that reference them.
    """

    # Populated by _init_brain() after COMPILED_RULES exists at module level.
    _KEYWORD_SETS: dict[str, frozenset[str]] = {}

    # Bonus scores awarded when strong type-confirming keywords are present.
    _TYPE_BONUS: dict[str, Tuple[Tuple[str, ...], int]] = {
        "LR":      (("TRANSPORTER COPY", "CONSIGNMENT NOTE", "WAYBILL", "BILTY"), 20),
        "INVOICE": (("TAX INVOICE", "BILL TO", "GSTIN", "ORIGINAL FOR RECIPIENT"), 18),
    }

    # Dummy regional pincode ranges for false-positive OCR rejection.
    # Any 6-digit candidate matching these is rejected as a postal code 
    # rather than accepted as a valid logistics tracking number.
    _PINCODE_RANGES: Tuple[Tuple[int, int], ...] = (
        (100000, 100150),  # Region A
        (200000, 200500),  # Region B
        (300000, 300999),  # Region C
    )

    @classmethod
    def _is_pincode(cls, candidate: str) -> bool:
        if len(candidate) != 6 or not candidate.isdigit():
            return False
        n = int(candidate)
        return any(lo <= n <= hi for lo, hi in cls._PINCODE_RANGES)

    @staticmethod
    def _clean(raw: str) -> str:
        if not raw:
            return ""
        text = re.sub(r"(?<=\d)\s+(?=\d)", "", raw)   
        text = re.sub(r"(?<=\d)-(?=\d)",   "", text)   
        text = re.sub(r"(?<=\d),(?=\d)",   "", text)   
        text = text.replace("|", " ")
        text = re.sub(r"[\t\r\f]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _score(compiled: CompiledRule, text: str) -> DetectionResult:
        upper  = BrainSystem._clean(text).upper()
        score  = 0.0
        doc_id: Optional[str] = None
        rule   = compiled.rule

        # Keyword bonus
        for kw in BrainSystem._KEYWORD_SETS.get(rule.name, frozenset()):
            if kw in upper:
                score += 12.0

        # Pattern match — first valid, non-pincode match wins..
        for pat in compiled.compiled:
            m = pat.search(text)
            if m:
                score += 30.0
                token   = m.group(1) if m.groups() else m.group(0)
                cleaned = re.sub(r"[^A-Z0-9]", "", token.upper())
                if cleaned:
                    if rule.doc_type == "LR" and BrainSystem._is_pincode(cleaned):
                        score -= 30.0  # cancel bonus; try next pattern
                        continue
                    doc_id = cleaned
                break

        # Type-specific bonus
        bonus = BrainSystem._TYPE_BONUS.get(rule.doc_type)
        if bonus:
            bonus_kws, bonus_val = bonus
            if any(k in upper for k in bonus_kws):
                score += bonus_val

        if len(upper) < 20:
            score *= 0.6  # penalise very short / garbage OCR output

        return DetectionResult(rule.doc_type, doc_id, score, source="ocr")

    @staticmethod
    def identify(text: str) -> Tuple[Optional[str], Optional[str], float]:
        cleaned = BrainSystem._clean(text)
        if not cleaned:
            return None, None, 0.0
        results = [BrainSystem._score(cr, cleaned) for cr in COMPILED_RULES]
        results = [r for r in results if r.doc_type and r.doc_id]
        if not results:
            return None, None, 0.0
        results.sort(key=lambda r: r.confidence, reverse=True)
        best = results[0]
        return best.doc_type, best.doc_id, round(best.confidence, 2)


def _init_brain() -> None:
    """Populate BrainSystem._KEYWORD_SETS after COMPILED_RULES exists."""
    BrainSystem._KEYWORD_SETS = {
        cr.rule.name: frozenset(cr.rule.keywords)
        for cr in COMPILED_RULES
    }


_init_brain()

# =============================================================================
# Document engine
# =============================================================================

class DocumentEngine:

    def __init__(self, base_dir: str, worker_count: int) -> None:
        self.base_dir             = base_dir
        self.worker_count         = worker_count
        self.task_queue: queue.Queue[str] = queue.Queue()
        self.stop_signal          = threading.Event()
        self.cancel_signal        = threading.Event()
        self.current_master_path: Optional[str] = None
        self.last_renamed_file:   Optional[str] = None
        self._workers: List[threading.Thread]   = []

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    def _wait_for_unlock(self, path: str, wid: int) -> bool:
        retries = CONFIG["FILE_LOCK_RETRIES"]
        for attempt in range(1, retries + 1):
            if self.cancel_signal.is_set():
                return False
            STATE.update_worker(wid, f"Awaiting file lock ({attempt}/{retries})")
            try:
                with open(path, "rb"):
                    return True
            except OSError:
                time.sleep(CONFIG["FILE_LOCK_DELAY"])
        return False

    def _merge_pdfs(self, target: str, source: str) -> bool:
        """Append source to target using an atomic temp-file swap."""
        tmp = f"{target}.tmp"
        try:
            with fitz.open(target) as tgt, fitz.open(source) as src:
                tgt.insert_pdf(src)
                tgt.save(tmp)
            os.replace(tmp, target)
            return True
        except Exception as exc:
            STATE.log_event("ERROR", f"Merge failed: {exc}")
            safe_unlink(tmp)
            return False

    # ------------------------------------------------------------------
    # Text extraction (page object used while document is still open)
    # ------------------------------------------------------------------

    def _page_candidates(
        self,
        doc: fitz.Document,
        page_index: int,
        page_image: Image.Image,
    ) -> List[Tuple[str, str]]:
        """Collect text candidates from embedded layer + OCR crops.
        Page object accessed inside the open-document block — fixes the
        use-after-close bug from the original code."""
        candidates: List[Tuple[str, str]] = []

        try:
            embedded = doc[page_index].get_text("text") or ""
            if embedded.strip():
                candidates.append(("embedded", embedded))
        except Exception:
            pass  # encrypted/damaged page — OCR below compensates

        full_text = OCREngine.extract_text(page_image)
        if full_text.strip():
            candidates.append(("full", full_text))

        # Early exit if full OCR is already confident
        _, _, conf = BrainSystem.identify(full_text)
        if conf > 60.0:
            return candidates

        w, h = page_image.size
        for label, crop in (
            ("left",   page_image.crop((0,      0,      w // 2, h))),
            ("right",  page_image.crop((w // 2, 0,      w,      h))),
            ("top",    page_image.crop((0,      0,      w,      h // 2))),
            ("bottom", page_image.crop((0,      h // 2, w,      h))),
        ):
            t = OCREngine.extract_text(crop)
            if t.strip():
                candidates.append((label, t))

        return candidates

    # ------------------------------------------------------------------
    # QR fast path
    # ------------------------------------------------------------------

    def _try_qr(self, page_image: Image.Image) -> Optional[Tuple[str, str, str, float]]:
        if not CONFIG["ENABLE_QR_PRIORITY"]:
            return None
        result = QRParser.decode_from_image(page_image)
        if not result:
            return None
        doc_type, doc_id = result
        if doc_type not in ("LR", "INVOICE"):
            return None
        tag = "LR detected via QR" if doc_type == "LR" else "Invoice detected via QR"
        STATE.log_event("INFO", f"{tag}: {doc_id}")
        STATE.set_last_detected(f"{doc_type}:{doc_id} [QR] 100.0")
        return doc_type, doc_id, "QR", 100.0

    # ------------------------------------------------------------------
    # Main analysis
    # ------------------------------------------------------------------

    def analyze(
        self, file_path: str, wid: int
    ) -> Tuple[Optional[str], Optional[str], str, str, float]:
        """Returns (doc_type, doc_id, context, working_path, confidence)."""

        if self.cancel_signal.is_set():
            return "CANCELLED", None, "CANCEL", file_path, 0.0

        if not self._wait_for_unlock(file_path, wid):
            return None, None, "LOCKED", file_path, 0.0

        if not os.path.exists(file_path):
            return None, None, "MISSING", file_path, 0.0

        flags        = STATE.snapshot()["flags"]
        page_index   = 0
        working_path = file_path

        # --- Failsafe: blind-append to last renamed file ---
        if flags["failsafe"] and self.last_renamed_file and os.path.exists(self.last_renamed_file):
            STATE.update_worker(wid, "Failsafe merge\u2026")
            if self._merge_pdfs(self.last_renamed_file, file_path):
                return "MERGED", os.path.basename(self.last_renamed_file), "FAILSAFE", file_path, 100.0

        # --- Stitch mode ---
        if flags["stitch"]:
            if not (self.current_master_path and os.path.exists(self.current_master_path)):
                # First file → becomes the master
                master = os.path.join(self.base_dir, CONFIG["MASTER_FILENAME"])
                safe_unlink(master)
                try:
                    os.replace(file_path, master)
                    self.current_master_path = master
                    return "MASTER_SET", CONFIG["MASTER_FILENAME"], "STITCH-INIT", master, 100.0
                except OSError as exc:
                    STATE.log_event("ERROR", f"Master set failed: {exc}")
                    return None, None, "ERROR", file_path, 0.0
            else:
                # Subsequent file → merge onto master, then overwrite original path.
                #
                # WHY os.replace() instead of os.remove() + rename:
                # On Windows, fitz holds a brief OS-level lock after .close().
                # os.remove() on a just-closed file raises "Permission denied".
                # os.replace() (a rename operation) succeeds even then.
                # The temp file uses "_stitch_tmp_" which does NOT start with "img_",
                # so the watcher can never queue it even if a crash orphans it.
                tmp = os.path.join(
                    self.base_dir,
                    f"{CONFIG['STITCH_TEMP_PREFIX']}{time.time():.0f}.pdf",
                )
                try:
                    with fitz.open() as doc, \
                         fitz.open(self.current_master_path) as src1, \
                         fitz.open(file_path) as src2:
                        doc.insert_pdf(src1)
                        doc.insert_pdf(src2)
                        doc.save(tmp)
                    os.replace(tmp, file_path)  # atomic; safe on Windows
                    working_path = file_path
                    page_index   = 1
                except Exception as exc:
                    safe_unlink(tmp)
                    STATE.log_event("ERROR", f"Stitch merge failed: {exc}")
                    return None, None, "MERGE_FAIL", file_path, 0.0

        # --- Landscape detection (HARD RULE: landscape page = LR, always) ---
        # Checked before rendering to avoid wasting OCR time on non-LR paths.
        # This rule has no exceptions: the 3SC LR form is always A4 landscape;
        # invoices and challans are always A4 portrait.
        force_lr = False
        try:
            with fitz.open(working_path) as _probe:
                if _probe.page_count > 0:
                    _r = _probe[0].rect
                    if _r.width > _r.height:
                        force_lr = True
                        STATE.log_event("INFO", "Landscape page detected — forcing LR analysis")
        except Exception:
            pass  # if we can't probe geometry, proceed normally

        # --- Render page ---
        STATE.update_worker(wid, f"Rendering page {page_index}")
        page_image = VisionSystem.render_page(working_path, page_index)
        if page_image is None and page_index > 0:
            page_image = VisionSystem.render_page(working_path, 0)
        if page_image is None:
            return None, None, "CORRUPT", working_path, 0.0

        # --- QR fast path ---
        # For landscape (LR-forced) documents we still try QR — it works fine,
        # but we only accept an LR result from it.
        qr = self._try_qr(page_image)
        if qr:
            dt, di, ctx, conf = qr
            if force_lr and dt != "LR":
                # Landscape page must be LR — ignore a non-LR QR result and fall through.
                STATE.log_event("WARN", f"Landscape page: ignoring QR result {dt}:{di}, forcing OCR LR path")
            else:
                return dt, di, ctx, working_path, conf

        if not CONFIG["ENABLE_OCR_FALLBACK"]:
            return None, None, "NOQR", working_path, 0.0

        # --- OCR slow path ---
        # For landscape pages only LR rules are evaluated; this avoids false
        # positives from pincode or challan numbers printed elsewhere on the form.
        STATE.update_worker(wid, "OCR analysis\u2026")

        def _run_ocr_on_page(pg_index: int) -> List[Tuple[str, str]]:
            """Extract OCR candidates from a single page index."""
            pg_image = VisionSystem.render_page(working_path, pg_index)
            if pg_image is None:
                return []
            cands: List[Tuple[str, str]] = []
            try:
                with fitz.open(working_path) as _d:
                    cands = self._page_candidates(_d, pg_index, pg_image)
            except Exception:
                t = OCREngine.extract_text(pg_image)
                if t.strip():
                    cands = [("full", t)]
            return cands

        candidates: List[Tuple[str, str]] = _run_ocr_on_page(page_index)

        # --- Multi-page fallback ---
        # If page 0 (or the stitch page) gave no usable result, try pages 1 and 2.
        # Some LR documents are 2-page with the ID only on the second page.
        if not candidates:
            try:
                with fitz.open(working_path) as _probe2:
                    total_pages = _probe2.page_count
            except Exception:
                total_pages = 1

            for fallback_page in range(1, min(3, total_pages)):
                if fallback_page == page_index:
                    continue
                STATE.update_worker(wid, f"Trying page {fallback_page} fallback")
                extra = _run_ocr_on_page(fallback_page)
                if extra:
                    candidates.extend(extra)
                    break  # stop at first page that yields something

        # Score all candidates; if force_lr, filter to LR results only.
        ranked: List[Tuple[Optional[str], Optional[str], float, str]] = []
        for src, txt in candidates:
            if force_lr:
                # Only score against LR rules to avoid challan/invoice false positives.
                lr_rules = [cr for cr in COMPILED_RULES if cr.rule.doc_type == "LR"]
                results  = [BrainSystem._score(cr, BrainSystem._clean(txt)) for cr in lr_rules]
                results  = [r for r in results if r.doc_type and r.doc_id]
                if results:
                    results.sort(key=lambda r: r.confidence, reverse=True)
                    best = results[0]
                    ranked.append((best.doc_type, best.doc_id, best.confidence, src))
            else:
                dt, di, conf = BrainSystem.identify(txt)
                if dt and di:
                    ranked.append((dt, di, conf, src))

        if ranked:
            ranked.sort(key=lambda x: (x[2], 1 if x[0] == "LR" else 0), reverse=True)
            dt, di, conf, src = ranked[0]
            STATE.set_last_detected(f"{dt}:{di} [{src}] {conf:.1f}")

            # --- Confidence threshold (Priority 4) ---
            # Low-confidence OCR results go to manual review rather than
            # auto-renaming to prevent mis-identification in production.
            # QR results always bypass this check (they arrive via _try_qr above).
            if conf < CONFIG["MIN_OCR_CONFIDENCE"]:
                STATE.log_event(
                    "WARN",
                    f"OCR confidence too low ({conf:.1f} < {CONFIG['MIN_OCR_CONFIDENCE']}) "
                    f"for {dt}:{di} — flagged for manual review",
                )
                return None, None, "LOW_CONF", working_path, conf

            return dt, di, "OCR", working_path, conf

        combined = "\n".join(txt for _, txt in candidates)
        dt, di, conf = BrainSystem.identify(combined)
        if dt and di:
            STATE.set_last_detected(f"{dt}:{di} [combined] {conf:.1f}")
            if conf < CONFIG["MIN_OCR_CONFIDENCE"]:
                STATE.log_event(
                    "WARN",
                    f"OCR confidence too low ({conf:.1f}) for {dt}:{di} — flagged for manual review",
                )
                return None, None, "LOW_CONF", working_path, conf
        return dt, di, "OCR", working_path, conf

    # ------------------------------------------------------------------
    # Rename
    # ------------------------------------------------------------------

    def execute_rename(
        self, original_path: str, doc_type: str, doc_id: str, confidence: float = 0.0
    ) -> bool:
        if not isinstance(original_path, (str, os.PathLike)):
            STATE.log_event("ERROR", f"Invalid path type: {type(original_path)}")
            return False
        safe_id = sanitize_filename(doc_id)
        if not safe_id:
            return False
        target = unique_target_path(self.base_dir, safe_id)
        try:
            os.replace(original_path, target)
            self.last_renamed_file = target
            AuditManager.write_entry(safe_id, doc_type, file_size(target), method="AUTO")
            if confidence:
                STATE.log_event("INFO", f"{doc_type} \u2022 confidence {confidence:.1f}")
            return True
        except OSError as exc:
            STATE.log_event("ERROR", f"Rename failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _worker_loop(self, wid: int) -> None:
        filename = "(none)"
        while not self.stop_signal.is_set():
            try:
                STATE.update_worker(wid, "Idle")
                filename = self.task_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                fpath = os.path.join(self.base_dir, filename)
                STATE.update_worker(wid, f"Opening {filename[:20]}")
                dt, di, ctx, analyzed_path, conf = self.analyze(fpath, wid)

                if dt == "MASTER_SET":
                    STATE.log_event("INFO", "Stitch master set")
                    STATE.record_history("MSTR", CONFIG["MASTER_FILENAME"], "-", "SET")

                elif dt == "MERGED":
                    with suppress(OSError):
                        if (os.path.exists(analyzed_path)
                                and analyzed_path != self.last_renamed_file):
                            os.remove(analyzed_path)
                    AuditManager.write_entry(di or "merged", "MERGED", "N/A", "MERGED")

                elif dt and di:
                    STATE.update_worker(wid, f"Renaming \u2192 {di}")
                    if not self.execute_rename(analyzed_path, dt, di, conf):
                        STATE.record_history(dt, di, "-", "FAIL")

                else:
                    STATE.record_history("UNK", filename[:24], "-", "MANUAL")
                    STATE.log_event("WARN", f"Manual review required: {filename}")

            except Exception as exc:
                STATE.log_event("ERROR", f"Worker {wid} crash: {exc}")
                STATE.record_history("ERR", filename[:24], "-", "FAIL")
            finally:
                with suppress(Exception):
                    self.task_queue.task_done()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.stop_signal.clear()
        self._workers = [
            threading.Thread(
                target=self._worker_loop, args=(i + 1,),
                daemon=True, name=f"crom-worker-{i + 1}",
            )
            for i in range(self.worker_count)
        ]
        for t in self._workers:
            t.start()
        STATE.log_event(
            "INFO",
            f"Engine started \u2014 {self.worker_count} workers | "
            f"stitch_prefix={CONFIG['STITCH_TEMP_PREFIX']} | "
            f"v{CONFIG['APP_VERSION']}",
        )

    def stop(self) -> None:
        self.stop_signal.set()
        for t in self._workers:
            t.join(timeout=2.0)

# =============================================================================
# Dashboard (Rich TUI)
# =============================================================================

_LOG_STYLE: dict[str, str] = {
    "SUCCESS": "bold green", "ERROR": "bold red",
    "WARN":    "bold yellow", "INFO":  "bold cyan",
    "CMD":     "bold magenta",
}


class Dashboard:
    """Live terminal dashboard.
    Progress widget is created once in __init__ and updated each tick so
    the elapsed-time column accumulates correctly."""

    def __init__(self, console: Console) -> None:
        self.console   = console
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            expand=True,
        )
        self._task_id = self._progress.add_task("Processing", total=100, completed=0)

    def _header(self, snap: dict[str, Any]) -> Panel:
        flags = snap["flags"]
        t = Text()
        t.append(f" {CONFIG['APP_NAME']} v{CONFIG['APP_VERSION']} ", style="bold white on blue")
        t.append("  ")
        t.append("STITCH ON"   if flags["stitch"]   else "STITCH OFF",
                 style="bold cyan"   if flags["stitch"]   else "dim")
        t.append(" | ")
        t.append("FAILSAFE ON" if flags["failsafe"] else "FAILSAFE OFF",
                 style="bold red"    if flags["failsafe"] else "dim")
        t.append(" | ")
        t.append("PAUSED"      if flags["paused"]   else "RUNNING",
                 style="bold yellow" if flags["paused"]   else "bold green")
        t.append(f"  \u2022  Last: {snap['last_detected']}", style="bold magenta")
        return Panel(t, border_style="blue", padding=(0, 1))

    def _workers_panel(self, snap: dict[str, Any]) -> Panel:
        tbl = Table(expand=True, show_header=True, header_style="bold cyan")
        tbl.add_column("Worker", style="bold")
        tbl.add_column("State", overflow="fold")
        for wid in range(1, CONFIG["WORKER_THREADS"] + 1):
            tbl.add_row(f"W{wid}", snap["worker_status"].get(wid, "Idle"))
        return Panel(tbl, title="Workers", border_style="cyan")

    def _stats_panel(self, snap: dict[str, Any]) -> Panel:
        stats = snap["stats"]
        rate  = (stats["success"] + stats["merged"]) / max(1, stats["processed"]) * 100.0
        tbl   = Table.grid(padding=(0, 1))
        for label, value in (
            ("Queue",     str(snap["queue_depth"])),
            ("Processed", str(stats["processed"])),
            ("Auto",      str(stats["success"])),
            ("Merged",    str(stats["merged"])),
            ("Manual",    str(stats["manual"])),
            ("Failed",    str(stats["failed"])),
            ("Skipped",   str(stats["skipped"])),
            ("Win rate",  f"{rate:.1f}%"),
        ):
            tbl.add_row(label, value)
        return Panel(tbl, title="Metrics", border_style="magenta")

    def _controls_panel(self, _snap: dict[str, Any]) -> Panel:
        t = Text()
        for key, desc in (
            ("Q",         "Quit"),
            ("P / Space", "Pause"),
            ("S",         "Stitch toggle"),
            ("F",         "Failsafe toggle"),
            ("I",         "Panic stop"),
        ):
            t.append(key, style="bold")
            t.append(f"  {desc}\n")
        t.append("Tip:", style="bold yellow")
        t.append(" watch img_ PDFs in the folder")
        return Panel(t, title="Hotkeys", border_style="yellow")

    def _history_panel(self, snap: dict[str, Any]) -> Panel:
        tbl = Table(expand=True, show_header=True, header_style="bold green")
        tbl.add_column("Time",   width=9)
        tbl.add_column("Type",   width=12)
        tbl.add_column("ID",     overflow="fold")
        tbl.add_column("Status", width=9)
        for item in list(snap["history"])[:12]:
            tbl.add_row(
                item.time, item.doc_type, item.doc_id,
                Text(item.status, style=status_style(item.status)),
            )
        return Panel(tbl, title="Recent Activity", border_style="green")

    def _logs_panel(self, snap: dict[str, Any]) -> Panel:
        logs = snap["logs"][:8]
        if not logs:
            return Panel(Text("No events yet", style="dim"), title="Logs", border_style="white")
        t = Text()
        for rec in logs:
            t.append(f"{rec.time} | ", style="dim")
            t.append(f"{rec.level:<7}", style=_LOG_STYLE.get(rec.level, "white"))
            t.append(f" | {rec.message}\n")
        return Panel(t, title="Logs", border_style="white")

    def _progress_panel(self, snap: dict[str, Any]) -> Panel:
        stats = snap["stats"]
        pct   = min(100.0, (stats["success"] + stats["merged"]) / max(1, stats["processed"]) * 100.0)
        self._progress.update(self._task_id, completed=pct)
        return Panel(self._progress, title="Progress", border_style="cyan")

    def render(self) -> Layout:
        snap   = STATE.snapshot()
        layout = Layout(name="root")
        layout.split_column(
            Layout(self._header(snap),     name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(self._logs_panel(snap), name="footer", size=10),
        )
        layout["body"].split_row(
            Layout(name="left",  ratio=2),
            Layout(name="mid",   ratio=2),
            Layout(name="right", ratio=1),
        )
        layout["left"].split_column(
            Layout(self._workers_panel(snap), name="workers", size=8),
            Layout(self._history_panel(snap), name="history"),
        )
        layout["mid"].update(
            Panel(
                Group(self._stats_panel(snap), self._progress_panel(snap)),
                border_style="magenta",
            )
        )
        layout["right"].split_column(
            Layout(self._controls_panel(snap), name="controls", size=11),
            Layout(
                Panel.fit(
                    Text(
                        f"Folder: {snap['current_directory']}\n"
                        f"Queue depth: {snap['queue_depth']}",
                        justify="left",
                    ),
                    title="Status", border_style="blue",
                ),
                name="status",
            ),
        )
        return layout

# =============================================================================
# Keyboard input
# =============================================================================

class InputController:
    def __init__(self, engine: DocumentEngine) -> None:
        self.engine  = engine
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(
            target=self._listen, daemon=True, name="crom-input"
        )
        self._thread.start()

    def _listen(self) -> None:
        while True:
            char: Optional[str] = None
            try:
                if msvcrt and msvcrt.kbhit():
                    char = msvcrt.getch().decode("utf-8", errors="ignore").lower()
                elif select and select.select([sys.stdin], [], [], 0.1)[0]:
                    char = sys.stdin.read(1).lower()
            except Exception:
                pass  # stdin errors (redirected input etc.) — keep looping
            if char:
                self._queue.put(char)
            time.sleep(0.04)

    def process(self) -> bool:
        """Drain key queue. Returns False when Q is pressed."""
        while not self._queue.empty():
            key = self._queue.get_nowait()
            if key == "q":
                return False
            if key in ("p", " "):
                STATE.toggle_flag("paused")
                self.engine.cancel_signal.clear()
                STATE.log_event("CMD", "Pause toggled")
            elif key == "s":
                on = STATE.toggle_flag("stitch")
                if not on:
                    self.engine.current_master_path = None
                STATE.log_event("CMD", f"Stitch {'ON' if on else 'OFF'}")
            elif key == "f":
                on = STATE.toggle_flag("failsafe")
                STATE.log_event("CMD", f"Failsafe {'ON' if on else 'OFF'}")
            elif key == "i":
                STATE.set_flag("paused", True)
                self.engine.cancel_signal.set()
                STATE.log_event("CMD", "Panic stop triggered")
        return True

# =============================================================================
# Directory watcher
# =============================================================================

class DirectoryWatcher:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self._seen:   Set[str] = set()

    def scan(self) -> List[str]:
        try:
            current = set(os.listdir(self.base_dir))
        except PermissionError as exc:
            STATE.log_event("ERROR", f"Watch dir unreadable: {exc}")
            return []
        except OSError as exc:
            STATE.log_event("WARN", f"Scan error: {exc}")
            return []

        self._seen &= current  # drop entries that no longer exist

        stitch_prefix = CONFIG["STITCH_TEMP_PREFIX"].lower()
        master_name   = CONFIG["MASTER_FILENAME"].lower()

        new_files = [
            f for f in current
            if f.lower().endswith(CONFIG["WATCH_EXTENSIONS"])
            and CONFIG["PREFERRED_PREFIX"] in f.lower()
            and not f.lower().startswith(stitch_prefix)  # never queue stitch temps
            and f.lower() != master_name                  # never queue the master PDF
            and f not in self._seen
        ]
        return sorted(new_files)

    def mark_seen(self, filename: str) -> None:
        self._seen.add(filename)

# =============================================================================
# Watch mode orchestration
# =============================================================================

def run_watch_mode(base_dir: str) -> None:
    base_dir = os.path.abspath(base_dir)
    STATE.current_directory = os.path.basename(base_dir)
    AuditManager.initialize(base_dir)

    console   = Console()
    engine    = DocumentEngine(base_dir, CONFIG["WORKER_THREADS"])
    watcher   = DirectoryWatcher(base_dir)
    inputs    = InputController(engine)
    dashboard = Dashboard(console)

    engine.start()
    console.clear()

    try:
        with Live(dashboard.render(), console=console, refresh_per_second=10, screen=True) as live:
            running = True
            while running:
                running = inputs.process()
                STATE.queue_depth = engine.task_queue.qsize()

                if not STATE.snapshot()["flags"]["paused"]:
                    for filename in watcher.scan():
                        if STATE.queue_depth >= CONFIG["QUEUE_MAX_SIZE"]:
                            break
                        engine.task_queue.put(filename)
                        watcher.mark_seen(filename)
                        STATE.log_event("INFO", f"Queued: {filename}")

                live.update(dashboard.render())
                time.sleep(CONFIG["QUEUE_POLL_DELAY"])
    finally:
        engine.stop()
        console.clear()

# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(f"\n  {CONFIG['APP_NAME']} v{CONFIG['APP_VERSION']} \u2014 Automated Document Pipeline")
        print("  [1] Watch mode")
        print("  [2] Exit")
        choice = input("\n  Command > ").strip()
        if choice == "2":
            return
        if choice == "1":
            root = tk.Tk()
            root.withdraw()
            base_dir = filedialog.askdirectory(title="Select Target Directory")
            root.destroy()
            if base_dir:
                run_watch_mode(base_dir)


if __name__ == "__main__":
    _bootstrap()
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
