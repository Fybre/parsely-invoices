"""
Main pipeline orchestrator.

InvoiceProcessor ties together extraction, LLM parsing, supplier matching,
PO matching, and validation into a single process() call.

With Docling enabled (the default), the flow is:
  1. DoclingExtractor  -- layout-aware PDF -> Markdown + structured tables
  2. TableLineItemExtractor -- parse line items directly from tables (no LLM)
  3. LLMParser -- extract metadata fields from Markdown
                  (skips line_items if step 2 succeeded)
  4. SupplierMatcher -- identify supplier from master list
  5. POMatcher -- match PO, compare line items
  6. InvoiceValidator -- flag all discrepancies
  7. Upsert result into SQLite (output/pipeline.db)

Two top-level modes are exposed:
  - process_batch()     -- one-shot batch over a directory
  - watch_directory()   -- continuous polling loop; state is persisted to
                           SQLite so restarts are safe — no invoice is
                           processed twice unless its file has changed.
"""
import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import Config
from models.result import InvoiceProcessingResult
from .database import Database
from .custom_field_extractor import CustomFieldExtractor, load_custom_fields
from .extractor import DoclingExtractor, PlainTextExtractor, TableLineItemExtractor
from .llm_parser import LLMParser
from .supplier_matcher import SupplierMatcher
from .po_matcher import POMatcher
from .validator import InvoiceValidator
from .backup import BackupService

logger = logging.getLogger(__name__)


def _fill_computed_totals(invoice) -> None:
    """
    For any line item where total is None but both quantity and unit_price are
    known, compute total = round(quantity * unit_price, 2) and set the
    total_computed flag so the dashboard can show a subtle indicator.

    Discount is applied when present (treated as a multiplier, e.g. 0.10 = 10% off).
    """
    for item in invoice.line_items:
        if item.total is not None:
            continue
        if item.quantity is None or item.unit_price is None:
            continue
        computed = round(item.quantity * item.unit_price, 2)
        if item.discount:
            computed = round(computed * (1 - item.discount), 2)
        item.total = computed
        item.total_computed = True


class InvoiceProcessor:
    """
    Orchestrates the full invoice processing pipeline.

    Set config.use_docling = True  (default) for accuracy-first mode.
    Set config.use_docling = False for the fast pdfplumber fallback.
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.config.ensure_output_dir()

        # Database (single file, replaces flat JSON + .pipeline_state.json)
        self.db = Database(self.config.db_path)

        # Choose extractor based on config
        if self.config.use_docling:
            self.extractor = DoclingExtractor()
            logger.info("Using DoclingExtractor (accuracy-first mode)")
        else:
            self.extractor = PlainTextExtractor()
            logger.info("Using PlainTextExtractor (speed mode)")

        self.table_line_item_extractor = TableLineItemExtractor()

        # Custom fields (operator-defined via config/custom_fields.json)
        self._custom_fields_title, self._custom_fields = load_custom_fields()
        self._custom_field_extractor = CustomFieldExtractor(self._custom_fields)

        self.llm = LLMParser(
            model=self.config.llm_model,
            base_url=self.config.llm_base_url,
            api_key=self.config.llm_api_key,
        )
        self.supplier_matcher = SupplierMatcher(self.config.suppliers_csv)
        self.po_matcher = POMatcher(self.config.po_csv, self.config.po_lines_csv)
        self._csv_mtimes = self._current_csv_mtimes()
        self.validator = InvoiceValidator(
            max_days_past=self.config.max_invoice_age_days,
            max_days_future=self.config.max_future_days,
            arithmetic_tolerance=self.config.arithmetic_tolerance,
        )
        self.backup_service = BackupService(self.config)
        self._last_backup_run = self.backup_service.get_last_backup_time()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, pdf_path: str | Path) -> InvoiceProcessingResult:
        """
        Process a single invoice PDF end-to-end.

        Returns an InvoiceProcessingResult and writes it to the SQLite DB.
        The source PDF remains in the invoices directory until the operator
        approves the invoice and triggers export via the dashboard.
        """
        pdf_path = Path(pdf_path)
        logger.info("=== Processing: %s ===", pdf_path.name)
        start = time.monotonic()

        # Step 1: Extract document content
        logger.info("Step 1/5: Extracting document (%s)", self.extractor.__class__.__name__)
        extraction = self.extractor.extract(pdf_path)

        # Step 2: Try direct line item extraction from tables (Docling only)
        pre_extracted_items: Optional[list[dict]] = None
        if extraction.tables:
            logger.info(
                "Step 2/5: Attempting direct line item extraction from %d table(s)",
                len(extraction.tables),
            )
            pre_extracted_items = self.table_line_item_extractor.extract(extraction.tables)
            if pre_extracted_items:
                logger.info(
                    "Direct extraction succeeded: %d line items found (LLM will handle metadata only)",
                    len(pre_extracted_items),
                )
            else:
                logger.info("Direct extraction found no line items -- LLM will handle all fields")
        else:
            logger.info("Step 2/5: No structured tables available -- LLM will handle all fields")

        # Step 3: LLM extraction (metadata, or full if no pre-extracted items)
        logger.info("Step 3/5: LLM extraction (model=%s)", self.config.llm_model)
        invoice = self.llm.parse(
            extraction,
            pre_extracted_line_items=pre_extracted_items,
            custom_fields=self._custom_fields,
        )

        # Merge custom field results: regex/table override LLM (more deterministic)
        if self._custom_fields:
            regex_hits  = self._custom_field_extractor.extract_from_text(extraction.markdown)
            table_hits  = self._custom_field_extractor.extract_from_tables(extraction.tables)
            # Priority: regex > table > llm (already in invoice.custom_fields)
            merged = self._custom_field_extractor.merge(regex_hits, table_hits, invoice.custom_fields)
            invoice.custom_fields = merged
            invoice.custom_fields_title = self._custom_fields_title
            if merged:
                logger.info("Custom fields extracted: %s", list(merged.keys()))

        # Step 3b: Fill in computed totals where total is missing but qty × price are known
        _fill_computed_totals(invoice)

        # Step 4: Match supplier
        logger.info("Step 4/5: Matching supplier")
        matched_supplier = self.supplier_matcher.match(invoice)

        # Step 5: Match PO
        logger.info("Step 5/5: Matching PO and validating")
        matched_po = self.po_matcher.match(invoice)
        po_record = None
        if matched_po and matched_po.po_number:
            po_record = self.po_matcher.get_po(matched_po.po_number)

        discrepancies = self.validator.validate(
            invoice, matched_po, matched_supplier, po_record
        )

        elapsed = round(time.monotonic() - start, 3)

        result = InvoiceProcessingResult(
            source_file=str(pdf_path),
            processed_at=datetime.now(timezone.utc).isoformat(),
            processing_time_seconds=elapsed,
            extracted_invoice=invoice,
            raw_text_length=len(extraction.markdown),
            llm_model_used=self.config.llm_model,
            matched_supplier=matched_supplier,
            matched_po=matched_po,
            discrepancies=discrepancies,
        )
        result.compute_summary()

        # Persist to DB (replaces individual JSON files)
        self._save_result(result, pdf_path)

        logger.info(
            "Completed %s in %.2fs | extractor=%s | line_items=%s | errors=%d warnings=%d",
            pdf_path.name,
            elapsed,
            extraction.extractor_name,
            f"{len(pre_extracted_items)} (direct)" if pre_extracted_items else f"{len(invoice.line_items)} (llm)",
            result.error_count,
            result.warning_count,
        )
        return result

    def process_batch(self, directory: str | Path) -> list[InvoiceProcessingResult]:
        """
        Process all PDF files in a directory (one-shot batch mode).

        Already-processed files are skipped unless their mtime has changed.
        """
        directory = Path(directory)
        pdfs = sorted(directory.glob("*.pdf"))
        if not pdfs:
            logger.warning("No PDF files found in %s", directory)
            return []

        self._reload_matchers_if_changed()
        logger.info("Batch processing %d invoices from %s", len(pdfs), directory)
        results = []
        for i, pdf in enumerate(pdfs, 1):
            if self.db.is_processed(pdf.stem, pdf.stat().st_mtime):
                logger.info("[%d/%d] Skipping (already processed): %s", i, len(pdfs), pdf.name)
                continue
            logger.info("[%d/%d] %s", i, len(pdfs), pdf.name)
            try:
                result = self.process(pdf)
                results.append(result)
            except Exception as e:
                logger.error("Failed to process %s: %s", pdf.name, e, exc_info=True)
                self.db.record_failure(pdf.stem, str(pdf), pdf.stat().st_mtime, str(e))

        logger.info(
            "Batch complete: %d processed, %d skipped",
            len(results),
            len(pdfs) - len(results),
        )
        return results

    # ------------------------------------------------------------------
    # Watch / polling mode
    # ------------------------------------------------------------------

    def watch_directory(
        self,
        directory: str | Path,
        interval: Optional[int] = None,
    ) -> None:
        """
        Continuously poll *directory* for new PDF files and process them.

        Processing state is persisted in SQLite (output/pipeline.db) so the
        loop can be stopped and restarted safely — no invoice is processed
        twice unless the source file has changed (new mtime).

        Press Ctrl-C (or send SIGINT/SIGTERM) for a graceful shutdown.

        Args:
            directory: Directory to watch for PDF files.
            interval:  Seconds between scans (default: config.poll_interval_seconds).
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise ValueError(f"Watch target is not a directory: {directory}")

        interval = interval if interval is not None else self.config.poll_interval_seconds

        logger.info(
            "Watch mode started — directory=%s  interval=%ds  db=%s",
            directory, interval, self.config.db_path,
        )
        logger.info("Press Ctrl-C to stop.")

        # Graceful shutdown flag — set by SIGINT or SIGTERM
        _shutdown = {"requested": False}

        def _request_shutdown(signum, frame):  # noqa: ANN001
            _shutdown["requested"] = True
            logger.info("Shutdown signal received — finishing current invoice then exiting.")

        signal.signal(signal.SIGINT, _request_shutdown)
        signal.signal(signal.SIGTERM, _request_shutdown)

        total_processed = 0
        total_errors = 0

        try:
            while not _shutdown["requested"]:
                scan_start = time.monotonic()
                self._run_periodic_backup()
                self._reload_matchers_if_changed()
                new_pdfs = self._find_new_pdfs(directory)

                if new_pdfs:
                    logger.info("Found %d new invoice(s) to process.", len(new_pdfs))
                    for pdf in new_pdfs:
                        if _shutdown["requested"]:
                            logger.info(
                                "Shutdown requested — deferring remaining %d file(s).",
                                len(new_pdfs) - new_pdfs.index(pdf),
                            )
                            break
                        try:
                            self.process(pdf)
                            total_processed += 1
                        except Exception as exc:
                            logger.error(
                                "Failed to process %s: %s", pdf.name, exc, exc_info=True
                            )
                            self.db.record_failure(
                                pdf.stem, str(pdf), pdf.stat().st_mtime, str(exc)
                            )
                            total_errors += 1
                else:
                    logger.debug(
                        "No new invoices. Sleeping %ds (scan took %.2fs).",
                        interval, time.monotonic() - scan_start,
                    )

                if _shutdown["requested"]:
                    break

                # Interruptible sleep — check shutdown flag every second
                sleep_until = time.monotonic() + interval
                while time.monotonic() < sleep_until and not _shutdown["requested"]:
                    time.sleep(1)

        finally:
            logger.info(
                "Watch mode stopped. Session total: %d processed, %d errors.",
                total_processed, total_errors,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_csv_mtimes(self) -> dict[str, float]:
        """Return a mtime snapshot for all three CSV data files."""
        paths = [self.config.suppliers_csv, self.config.po_csv, self.config.po_lines_csv]
        return {str(p): p.stat().st_mtime for p in paths if p.exists()}

    def _reload_matchers_if_changed(self) -> None:
        """Reload supplier and PO matchers if any CSV file has changed on disk."""
        current = self._current_csv_mtimes()
        if current == self._csv_mtimes:
            return
        changed = [p for p in current if current[p] != self._csv_mtimes.get(p)]
        logger.info("CSV file(s) changed, reloading matchers: %s", changed)
        self.supplier_matcher = SupplierMatcher(self.config.suppliers_csv)
        self.po_matcher = POMatcher(self.config.po_csv, self.config.po_lines_csv)
        self._csv_mtimes = current

    def _run_periodic_backup(self) -> None:
        """Trigger an automated backup if the interval has elapsed."""
        if not self.config.backup_enabled:
            return

        now = datetime.now()
        interval = self.config.backup_interval_hours
        
        # If no last backup or interval elapsed, run now
        if not self._last_backup_run or (now - self._last_backup_run).total_seconds() >= (interval * 3600):
            try:
                self.backup_service.create_backup()
                self._last_backup_run = now
            except Exception as e:
                logger.error("Automated backup failed: %s", e)

    def _find_new_pdfs(self, directory: Path) -> list[Path]:
        """
        Return PDFs in *directory* that haven't been processed yet,
        or whose file mtime has changed since last processing.
        """
        new = []
        for pdf in sorted(directory.glob("*.pdf")):
            if not self.db.is_processed(pdf.stem, pdf.stat().st_mtime):
                new.append(pdf)
        return new

    def _save_result(self, result: InvoiceProcessingResult, pdf_path: Path) -> None:
        """Persist the result to SQLite."""
        result_dict = json.loads(result.model_dump_json())
        status = self.db.upsert_invoice(
            stem=pdf_path.stem,
            result_dict=result_dict,
            source_file=str(pdf_path),
            source_mtime=pdf_path.stat().st_mtime,
        )
        logger.info("Saved to DB: %s  status=%s", pdf_path.name, status)

    def check_setup(self) -> dict:
        """Verify that all dependencies and connections are ready."""
        status = {}

        # Docling availability
        if self.config.use_docling:
            try:
                import docling  # noqa: F401
                status["docling"] = {"ok": True}
            except ImportError:
                status["docling"] = {
                    "ok": False,
                    "error": "docling not installed. Run: pip install docling",
                }

        # Ollama
        status["llm"] = self.llm.check_connection()

        # Data files
        status["suppliers_csv"] = {
            "path": str(self.config.suppliers_csv),
            "exists": self.config.suppliers_csv.exists(),
            "count": len(self.supplier_matcher.suppliers),
        }
        status["po_csv"] = {
            "path": str(self.config.po_csv),
            "exists": self.config.po_csv.exists(),
            "count": len(self.po_matcher.purchase_orders),
        }
        status["output_dir"] = {
            "path": str(self.config.output_dir),
            "exists": self.config.output_dir.exists(),
        }
        status["database"] = {
            "path": str(self.config.db_path),
            "exists": self.config.db_path.exists(),
        }
        status["export_dir"] = {
            "path": str(self.config.export_dir),
            "exists": self.config.export_dir.exists(),
        }

        return status
