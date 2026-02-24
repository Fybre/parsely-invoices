"""
Document extraction module.

Primary:  DoclingExtractor  -- layout-aware PDF parsing via IBM Docling.
          Understands tables, headings, and document structure.
          Outputs Markdown (for LLM) + structured table data (for direct line
          item parsing without involving the LLM at all).
          Handles scanned PDFs natively via built-in OCR -- no extra setup needed.

Fallback: PlainTextExtractor -- pdfplumber character-level extraction.
          Fast but blind to table structure; used only when Docling is disabled.

TableLineItemExtractor -- identifies the line items table in Docling's structured
          output and converts it to LineItem-compatible dicts, bypassing the LLM
          for that step entirely on well-structured invoices.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def _load_column_keys() -> dict[str, set[str]]:
    """
    Load column header keyword sets from config/column_keys.json.
    Falls back to the hardcoded defaults below if the file is missing or invalid.
    The config directory is resolved via the CONFIG_DIR env var (default: /app/config),
    which is mounted as a host volume so operators can edit it without a rebuild.
    """
    config_dir = Path(os.environ.get("CONFIG_DIR", Path(__file__).parent.parent / "config"))
    config_path = config_dir / "column_keys.json"
    if config_path.exists():
        try:
            with open(config_path) as fh:
                data = json.load(fh)
            keys = {
                k: set(str(v).lower() for v in vals)
                for k, vals in data.items()
                if isinstance(vals, list) and not k.startswith("_")
            }
            logger.info("Loaded column_keys.json from %s", config_path)
            return keys
        except Exception as exc:
            logger.warning("Could not load column_keys.json (%s) — using defaults", exc)
    return {}


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    """
    Unified output from any extractor.

    markdown:        Rich Markdown with tables, headings, and layout preserved.
                     This is the primary input fed to the LLM.
    raw_text:        Plain-text fallback (empty for Docling results).
    tables:          Structured table data as list-of-dicts (one list per table).
                     Empty when using the plain-text fallback.
    page_count:      Number of pages in the source document.
    extractor_name:  Which extractor produced this result.
    """
    markdown: str
    raw_text: str
    tables: list[list[dict]] = field(default_factory=list)
    page_count: int = 0
    extractor_name: str = "unknown"


# ---------------------------------------------------------------------------
# DoclingExtractor  (primary -- accuracy-first)
# ---------------------------------------------------------------------------

class DoclingExtractor:
    """
    Layout-aware extractor using IBM Docling.

    On first use, Docling downloads its layout and OCR models (~1 GB total,
    stored in ~/.cache/docling). Subsequent calls use the cached models.

    Key advantages over plain-text extraction for invoices:
      - Tables are preserved as proper Markdown pipe-tables, which LLMs
        parse far more reliably than raw character-stream text.
      - Structured table data is also returned so line items can be parsed
        directly -- no LLM tokens needed for that step.
      - Scanned PDFs are handled automatically via built-in OCR.
    """

    def __init__(self):
        self._converter = None

    def _get_converter(self):
        """Lazily initialise the DocumentConverter (loads ML models once)."""
        if self._converter is None:
            try:
                from docling.document_converter import DocumentConverter
                self._converter = DocumentConverter()
                logger.info("Docling DocumentConverter initialised")
            except ImportError:
                raise RuntimeError(
                    "docling is not installed. Run: pip install docling"
                )
        return self._converter

    def extract(self, pdf_path: str | Path) -> ExtractionResult:
        """
        Convert a PDF with Docling and return an ExtractionResult.

        The 'markdown' field contains the full document as Markdown with
        tables expressed as pipe-table syntax -- a much richer LLM input
        than raw extracted text.
        The 'tables' field contains each table as a list of row dicts keyed
        by column header, used by TableLineItemExtractor for direct parsing.
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        logger.info("Docling converting: %s", pdf_path.name)
        converter = self._get_converter()

        try:
            result = converter.convert(str(pdf_path))
        except Exception as e:
            raise RuntimeError(
                f"Docling conversion failed for {pdf_path.name}: {e}"
            ) from e

        doc = result.document

        # Markdown (primary LLM input)
        markdown = doc.export_to_markdown()
        logger.info(
            "Docling produced %d chars of Markdown from %s",
            len(markdown), pdf_path.name,
        )

        # Structured tables
        tables: list[list[dict]] = []
        for i, table in enumerate(doc.tables):
            try:
                df = table.export_to_dataframe()
                tables.append(df.to_dict(orient="records"))
                logger.debug(
                    "Table %d: %d rows x %d cols", i + 1, len(df), len(df.columns)
                )
            except Exception as e:
                logger.debug("Could not export table %d as dataframe: %s", i + 1, e)

        page_count = len(doc.pages) if hasattr(doc, "pages") else 0
        logger.info(
            "Extracted %d tables, %d pages from %s",
            len(tables), page_count, pdf_path.name,
        )

        return ExtractionResult(
            markdown=markdown,
            raw_text="",
            tables=tables,
            page_count=page_count,
            extractor_name="docling",
        )


# ---------------------------------------------------------------------------
# PlainTextExtractor  (fallback -- speed-first)
# ---------------------------------------------------------------------------

class PlainTextExtractor:
    """
    Fast plain-text extraction using pdfplumber.
    No table structure is preserved.
    Used only when Docling is disabled (config.use_docling = False).
    """

    def extract(self, pdf_path: str | Path) -> ExtractionResult:
        try:
            import pdfplumber
        except ImportError:
            raise RuntimeError(
                "pdfplumber is not installed. Run: pip install pdfplumber"
            )

        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        pages_text: list[str] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            page_count = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text:
                    pages_text.append(text.strip())
                else:
                    logger.debug("Page %d yielded no text (may be scanned)", i + 1)

        raw_text = "\n\n".join(pages_text)

        if not raw_text.strip():
            logger.warning(
                "No text extracted from %s -- likely a scanned PDF. "
                "Enable Docling (config.use_docling=True) for automatic OCR support.",
                pdf_path.name,
            )

        logger.info(
            "pdfplumber extracted %d chars from %s (%d pages)",
            len(raw_text), pdf_path.name, page_count,
        )

        return ExtractionResult(
            markdown=raw_text,
            raw_text=raw_text,
            tables=[],
            page_count=page_count,
            extractor_name="pdfplumber",
        )


# ---------------------------------------------------------------------------
# TableLineItemExtractor  -- direct line item parsing from Docling tables
# ---------------------------------------------------------------------------

# Canonical column-name sets (normalised: lowercase, collapsed whitespace)
_BUILTIN_KEYS: dict[str, set[str]] = {
    "description": {"description", "item", "details", "product", "service",
                    "goods", "particulars", "desc", "name", "work", "task",
                    "item description", "product description"},
    "sku":         {"sku", "code", "item code", "part no", "part number",
                    "part#", "ref", "product code", "cat no", "cat#",
                    "item no", "item #", "item number", "job code"},
    "quantity":    {"qty", "quantity", "units", "no", "hours", "hrs", "count",
                    "no.", "qty.", "order", "ordered", "supply", "supplied",
                    "order qty", "supply qty", "delivered", "invoiced"},
    "unit":        {"unit", "uom", "each", "measure", "unit of measure"},
    "unit_price":  {"unit price", "unit_price", "rate", "price", "unitprice",
                    "unit cost", "each", "cost", "per unit", "charge",
                    "unit rate", "price each"},
    "total":       {"total", "amount", "line total", "linetotal", "ext",
                    "extended", "net", "line amount", "nett", "extended amount",
                    "total amount", "net amount"},
}

_loaded = _load_column_keys()
_DESCRIPTION_KEYS = _loaded.get("description") or _BUILTIN_KEYS["description"]
_SKU_KEYS         = _loaded.get("sku")         or _BUILTIN_KEYS["sku"]
_QTY_KEYS         = _loaded.get("quantity")    or _BUILTIN_KEYS["quantity"]
_UNIT_KEYS        = _loaded.get("unit")        or _BUILTIN_KEYS["unit"]
_UNIT_PRICE_KEYS  = _loaded.get("unit_price")  or _BUILTIN_KEYS["unit_price"]
_TOTAL_KEYS       = _loaded.get("total")       or _BUILTIN_KEYS["total"]


def _norm(key: str) -> str:
    """Normalise a column header for comparison."""
    return re.sub(r"[\s_\-\.]+", " ", str(key)).lower().strip()


def _matches(header, key_set: set[str]) -> bool:
    # Guard: tables without a header row get integer column indices — skip them
    if not isinstance(header, str):
        return False
    # Exact match on the full normalised header
    if _norm(header) in key_set:
        return True
    # Also check each slash-separated part independently.
    # Handles composite headers like "Item Description / Labour Description"
    # or "Delivered Quantity/Hours" where one part matches a known key.
    for part in re.split(r"\s*/\s*", header):
        part = part.strip()
        if part and _norm(part) in key_set:
            return True
    return False


class TableLineItemExtractor:
    """
    Identifies the invoice line items table from Docling's structured output
    and converts it directly to LineItem-compatible dicts.

    When it succeeds, the LLM is not needed for line items at all -- it only
    handles the header/metadata fields (dates, totals, ABN, PO number, etc.).
    This is both faster and more accurate for well-formatted PDFs.
    """

    # Minimum recognition score to qualify as a line items table
    MIN_SCORE = 2

    def extract(self, tables: list[list[dict]]) -> list[dict] | None:
        """
        Return parsed line items from the best candidate table, or None if
        no suitable table is identified.
        """
        best = self._find_line_items_table(tables)
        if best is None:
            logger.debug("No line items table found in Docling output")
            return None

        items = self._parse_table(best)
        if items:
            logger.info(
                "Direct table extraction: %d line items (LLM not needed for these)",
                len(items),
            )
        return items or None

    def _find_line_items_table(self, tables: list[list[dict]]) -> list[dict] | None:
        best_table: list[dict] | None = None
        best_score = 0
        for table in tables:
            if not table:
                continue
            score = self._score_table(list(table[0].keys()), len(table))
            if score > best_score:
                best_score = score
                best_table = table
        return best_table if best_score >= self.MIN_SCORE else None

    def _score_table(self, headers: list[str], row_count: int) -> int:
        score = 0
        for h in headers:
            if _matches(h, _DESCRIPTION_KEYS):
                score += 2
            elif _matches(h, _QTY_KEYS):
                score += 1
            elif _matches(h, _UNIT_PRICE_KEYS):
                score += 1
            elif _matches(h, _TOTAL_KEYS):
                score += 1
            elif _matches(h, _SKU_KEYS):
                score += 1
        if row_count >= 2:
            score += 1
        return score

    def _parse_table(self, table: list[dict]) -> list[dict]:
        if not table:
            return []

        col_map = self._build_col_map(list(table[0].keys()))
        items: list[dict] = []
        line_num = 0

        for row in table:
            # Expand rows where cells contain \n-separated stacked values
            # (e.g. Fresh Computer Systems invoices pack all items into one row)
            for sub_row in self._expand_stacked_row(row):
                first_val = _norm(str(list(sub_row.values())[0])) if sub_row else ""
                if first_val in _DESCRIPTION_KEYS:
                    continue  # skip embedded header rows

                line_num += 1
                item: dict = {"line_number": line_num}
                for col, field_name in col_map.items():
                    raw = sub_row.get(col)
                    if raw is None or str(raw).strip() in ("", "None", "-", "\u2013"):
                        continue
                    val_str = str(raw).strip()

                    if field_name in ("description", "sku", "unit"):
                        item[field_name] = val_str
                    elif field_name in ("quantity", "unit_price", "total"):
                        num = _to_float(val_str)
                        if num is not None:
                            item[field_name] = num

                # A valid line item needs:
                #   - total (mandatory — the line value)
                #   - quantity OR unit_price (at least one numeric dimension)
                #   - sku OR description (identification)
                # This filters out header echoes, subtotal/summary rows, blank rows.
                has_total      = "total" in item
                has_dimension  = "quantity" in item or "unit_price" in item
                has_identity   = "sku" in item or "description" in item
                if has_total and has_dimension and has_identity:
                    items.append(item)

        return items

    @staticmethod
    def _expand_stacked_row(row: dict) -> list[dict]:
        """
        Detect rows where cells contain newline-separated stacked values and
        expand them into individual rows.  Returns [row] unchanged when no
        stacking is found (the common case — zero overhead).
        """
        if not row:
            return [row]
        max_parts = max(
            (len(str(v).split('\n')) for v in row.values() if v is not None),
            default=1,
        )
        if max_parts <= 1:
            return [row]

        keys = list(row.keys())
        split_vals = [
            str(row[k]).split('\n') if row.get(k) is not None else []
            for k in keys
        ]
        return [
            {key: (split_vals[j][i].strip() if i < len(split_vals[j]) else '')
             for j, key in enumerate(keys)}
            for i in range(max_parts)
        ]

    def _build_col_map(self, headers: list[str]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        assigned: set[str] = set()
        priority = [
            ("description", _DESCRIPTION_KEYS),
            ("sku",         _SKU_KEYS),
            ("quantity",    _QTY_KEYS),
            ("unit",        _UNIT_KEYS),
            ("unit_price",  _UNIT_PRICE_KEYS),
            ("total",       _TOTAL_KEYS),
        ]
        for header in headers:
            for field_name, key_set in priority:
                if field_name not in assigned and _matches(header, key_set):
                    mapping[header] = field_name
                    assigned.add(field_name)
                    break
        return mapping


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(value: str) -> float | None:
    cleaned = re.sub(r"[^\d.\-]", "", value.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None
