"""
Document data-extraction service for the chatbot knowledge base.

Supports PDF, TXT, CSV and XLSX. Designed for low latency:
- Streams/parses directly from in-memory bytes (no temp files on disk).
- Uses PyMuPDF (fitz) for fast PDF text extraction.
- Uses the stdlib `csv` module and openpyxl `read_only` mode for big sheets.
- Caps the amount of extracted text to keep memory and downstream LLM
  token usage predictable.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Hard cap on extracted characters to protect memory / downstream token usage.
MAX_CHARS = 2_000_000


class UnsupportedFileType(Exception):
    """Raised when a file extension is not supported by the extractor."""


class ExtractionError(Exception):
    """Raised when a supported file fails to parse."""


@dataclass
class ExtractionResult:
    """Outcome of extracting text from a single document."""

    text: str
    file_type: str
    char_count: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.char_count:
            self.char_count = len(self.text)


class DataExtractor:
    """
    Extract plain text from documents of different formats.

    Usage:
        extractor = DataExtractor()
        result = extractor.extract(file_bytes, filename="report.pdf")
        print(result.text)
    """

    SUPPORTED_EXTENSIONS = (".pdf", ".txt", ".csv", ".xlsx", ".xls", ".docx")

    def __init__(self, max_chars: int = MAX_CHARS) -> None:
        self.max_chars = max_chars

    # ── public API ──────────────────────────────────────────────────────────

    def extract(self, data: bytes, filename: str) -> ExtractionResult:
        """
        Dispatch to the correct parser based on the file extension.

        Args:
            data: Raw file bytes.
            filename: Original filename (used to detect the extension).

        Returns:
            ExtractionResult with the extracted text.

        Raises:
            UnsupportedFileType: extension not supported.
            ExtractionError: file could not be parsed.
        """
        ext = self._extension(filename)

        if ext == ".pdf":
            return self._extract_pdf(data, filename)
        if ext == ".txt":
            return self._extract_txt(data, filename)
        if ext == ".csv":
            return self._extract_csv(data, filename)
        if ext == ".xlsx":
            return self._extract_xlsx(data, filename)
        if ext == ".xls":
            return self._extract_xls(data, filename)
        if ext == ".docx":
            return self._extract_docx(data, filename)

        raise UnsupportedFileType(
            f"Unsupported file type '{ext}'. "
            f"Supported: {', '.join(self.SUPPORTED_EXTENSIONS)}"
        )

    def is_supported(self, filename: str) -> bool:
        return self._extension(filename) in self.SUPPORTED_EXTENSIONS

    # ── format parsers ──────────────────────────────────────────────────────

    def _extract_pdf(self, data: bytes, filename: str) -> ExtractionResult:
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:  # pragma: no cover
            raise ExtractionError("PyMuPDF (pymupdf) is not installed.") from exc

        parts: list[str] = []
        total = 0
        try:
            with fitz.open(stream=data, filetype="pdf") as doc:
                page_count = doc.page_count
                for page in doc:
                    text = page.get_text("text")
                    if not text:
                        continue
                    parts.append(text)
                    total += len(text)
                    if total >= self.max_chars:
                        break
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"Failed to parse PDF: {exc}") from exc

        text = self._truncate("\n".join(parts))
        return ExtractionResult(
            text=text,
            file_type="pdf",
            metadata={"pages": page_count},
        )

    def _extract_txt(self, data: bytes, filename: str) -> ExtractionResult:
        text = self._decode(data)
        return ExtractionResult(text=self._truncate(text), file_type="txt")

    def _extract_csv(self, data: bytes, filename: str) -> ExtractionResult:
        text = self._decode(data)
        rows = 0
        out = io.StringIO()
        try:
            reader = csv.reader(io.StringIO(text))
            for row in reader:
                out.write("\t".join(cell.strip() for cell in row))
                out.write("\n")
                rows += 1
                if out.tell() >= self.max_chars:
                    break
        except csv.Error as exc:
            raise ExtractionError(f"Failed to parse CSV: {exc}") from exc

        return ExtractionResult(
            text=self._truncate(out.getvalue()),
            file_type="csv",
            metadata={"rows": rows},
        )

    def _extract_xlsx(self, data: bytes, filename: str) -> ExtractionResult:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover
            raise ExtractionError("openpyxl is not installed.") from exc

        out = io.StringIO()
        sheets = 0
        rows = 0
        try:
            wb = load_workbook(
                filename=io.BytesIO(data), read_only=True, data_only=True
            )
            try:
                for ws in wb.worksheets:
                    sheets += 1
                    out.write(f"# Sheet: {ws.title}\n")
                    for row in ws.iter_rows(values_only=True):
                        cells = [
                            "" if v is None else str(v).strip() for v in row
                        ]
                        if not any(cells):
                            continue
                        out.write("\t".join(cells))
                        out.write("\n")
                        rows += 1
                        if out.tell() >= self.max_chars:
                            raise _StopExtraction
                    out.write("\n")
            finally:
                wb.close()
        except _StopExtraction:
            pass
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"Failed to parse XLSX: {exc}") from exc

        return ExtractionResult(
            text=self._truncate(out.getvalue()),
            file_type="xlsx",
            metadata={"sheets": sheets, "rows": rows},
        )

    def _extract_xls(self, data: bytes, filename: str) -> ExtractionResult:
        try:
            import xlrd
        except ImportError as exc:  # pragma: no cover
            raise ExtractionError("xlrd is not installed.") from exc

        out = io.StringIO()
        sheets = 0
        rows = 0
        try:
            book = xlrd.open_workbook(file_contents=data)
            try:
                for sheet in book.sheets():
                    sheets += 1
                    out.write(f"# Sheet: {sheet.name}\n")
                    for row_idx in range(sheet.nrows):
                        cells = [
                            "" if v is None else str(v).strip()
                            for v in sheet.row_values(row_idx)
                        ]
                        if not any(cells):
                            continue
                        out.write("\t".join(cells))
                        out.write("\n")
                        rows += 1
                        if out.tell() >= self.max_chars:
                            raise _StopExtraction
                    out.write("\n")
            finally:
                book.release_resources()
        except _StopExtraction:
            pass
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"Failed to parse XLS: {exc}") from exc

        return ExtractionResult(
            text=self._truncate(out.getvalue()),
            file_type="xls",
            metadata={"sheets": sheets, "rows": rows},
        )

    def _extract_docx(self, data: bytes, filename: str) -> ExtractionResult:
        try:
            from docx import Document
        except ImportError as exc:  # pragma: no cover
            raise ExtractionError("python-docx is not installed.") from exc

        out = io.StringIO()
        paragraphs = 0
        tables = 0
        try:
            document = Document(io.BytesIO(data))

            for para in document.paragraphs:
                text = (para.text or "").strip()
                if not text:
                    continue
                out.write(text)
                out.write("\n")
                paragraphs += 1
                if out.tell() >= self.max_chars:
                    break

            if out.tell() < self.max_chars:
                for table in document.tables:
                    tables += 1
                    for row in table.rows:
                        cells = [(cell.text or "").strip() for cell in row.cells]
                        if not any(cells):
                            continue
                        out.write("\t".join(cells))
                        out.write("\n")
                        if out.tell() >= self.max_chars:
                            break
                    if out.tell() >= self.max_chars:
                        break
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"Failed to parse DOCX: {exc}") from exc

        return ExtractionResult(
            text=self._truncate(out.getvalue()),
            file_type="docx",
            metadata={"paragraphs": paragraphs, "tables": tables},
        )

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _extension(filename: str) -> str:
        return os.path.splitext(filename or "")[1].lower()

    @staticmethod
    def _decode(data: bytes) -> str:
        """Decode bytes trying a few common encodings before giving up."""
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    def _truncate(self, text: str) -> str:
        if len(text) > self.max_chars:
            return text[: self.max_chars]
        return text


class _StopExtraction(Exception):
    """Internal signal to break out of nested xlsx iteration on size cap."""
