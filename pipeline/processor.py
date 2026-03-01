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
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import Config, PROJECT_ROOT
from models.result import InvoiceProcessingResult
from .database import Database
from .custom_field_extractor import CustomFieldExtractor, load_custom_fields
from .extractor import DoclingExtractor, PlainTextExtractor, TableLineItemExtractor
from .llm_parser import LLMParser
from .supplier_matcher import SupplierMatcher
from .internal_company_manager import InternalCompanyManager
from .po_matcher import POMatcher
from .field_config import reload_field_config
from .validator import InvoiceValidator
from .backup import BackupService
from .email_ingest import EmailIngestService

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


class TextStreamMap:
    """
    Builds a continuous stream of text from document elements and maintains
    a mapping back to the source elements and their coordinates.
    """
    def __init__(self, elements: list[dict]):
        self.full_text = ""
        self.offsets = [] # List of (start, end, element_index)
        
        for i, el in enumerate(elements):
            text = el["text"]
            start = len(self.full_text)
            self.full_text += text + " " # Space to prevent merging words incorrectly
            end = len(self.full_text)
            self.offsets.append((start, end, i))

    def find_matches(self, value: str) -> list[dict]:
        """
        Finds the best match for value in the stream and returns the 
        participating elements/boxes.
        """
        if not value or len(str(value).strip()) < 2:
            return []

        val_str = str(value).strip()
        
        # Helper to normalize for fuzzy matching
        def normalize(s):
            return re.sub(r'[^a-zA-Z0-9]', '', s.lower())

        norm_target = normalize(val_str)
        if not norm_target:
            return []

        # We normalize the entire stream but keep track of indices.
        # To handle spaces/newlines correctly, we search the full text.
        # But wait - a simpler robust way: 
        # Search for the normalized target within a normalized version of the full text.
        
        # Build normalized stream with index mapping
        norm_stream = ""
        index_map = [] # maps norm_stream index back to full_text index
        for i, char in enumerate(self.full_text):
            norm_char = char.lower()
            if norm_char.isalnum():
                index_map.append(i)
                norm_stream += norm_char

        # Find first match in normalized stream
        match_idx = norm_stream.find(norm_target)
        if match_idx == -1:
            return []

        # Map back to full_text range
        stream_start = index_map[match_idx]
        stream_end = index_map[match_idx + len(norm_target) - 1] + 1
        
        # Identify which elements overlap this range
        participating_elements = []
        for start, end, el_idx in self.offsets:
            # Check for overlap
            if max(start, stream_start) < min(end, stream_end):
                participating_elements.append(el_idx)
        
        return participating_elements


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
        self.supplier_matcher = SupplierMatcher(
            self.config.suppliers_csv,
            fuzzy_threshold=self.config.supplier_fuzzy_threshold
        )
        self.internal_company_manager = InternalCompanyManager(self.config.internal_companies_json)
        self.po_matcher = POMatcher(
            self.config.po_csv,
            self.config.po_lines_csv,
            line_fuzzy_threshold=self.config.po_line_fuzzy_threshold
        )
        self._csv_mtimes = self._current_csv_mtimes()
        self.validator = InvoiceValidator(
            max_days_past=self.config.max_invoice_age_days,
            max_days_future=self.config.max_future_days,
            arithmetic_tolerance=self.config.arithmetic_tolerance,
            po_total_tolerance_pct=self.config.po_total_tolerance_pct,
        )
        self.backup_service = BackupService(self.config)
        self._last_backup_run = self.backup_service.get_last_backup_time()
        
        self.email_ingest_service = EmailIngestService(self.config)
        self._last_email_poll: Optional[datetime] = None

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
        self.db.set_pipeline_status("processing", current_file=pdf_path.name)
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
        
        buyer_info = None
        if self.config.use_anchoring:
            buyer_info = self.internal_company_manager.get_all()
            
        invoice = self.llm.parse(
            extraction,
            pre_extracted_line_items=pre_extracted_items,
            custom_fields=self._custom_fields,
            buyer_info=buyer_info,
        )

        # (NEW) Second-pass correction if the LLM self-identified as supplier
        if self.config.use_anchoring and invoice.supplier:
            abn = invoice.supplier.abn or invoice.supplier.acn
            if abn and self.internal_company_manager.is_internal_abn(abn):
                logger.info("Self-identification detected for %s. Triggering second-pass correction...", invoice.supplier.name)
                
                correction_hint = f"In the previous attempt, you identified '{invoice.supplier.name}' (ABN: {abn}) as the supplier. " \
                                  f"This is INCORRECT. '{invoice.supplier.name}' is the BUYER (the customer). " \
                                  f"Please re-examine the document and identify the ACTUAL supplier who is sending the invoice."
                
                # Re-run LLM with correction hint
                invoice = self.llm.parse(
                    extraction,
                    pre_extracted_line_items=pre_extracted_items,
                    custom_fields=self._custom_fields,
                    buyer_info=buyer_info,
                    correction_hint=correction_hint
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

        internal_manager = self.internal_company_manager if self.config.use_anchoring else None
        discrepancies = self.validator.validate(
            invoice, matched_po, matched_supplier, po_record,
            internal_company_manager=internal_manager
        )

        # Step 6: Map extracted fields to coordinates (Docling only)
        field_coordinates = {}
        if extraction.elements:
            logger.info("Step 6/5: Mapping fields to coordinates (Stream Offset Map)")
            field_coordinates = self._map_fields_to_coordinates(invoice, extraction)

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
            field_coordinates=field_coordinates,
            page_dimensions=extraction.page_dimensions,
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
        self.db.set_pipeline_status("idle")
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
                self._run_periodic_email_poll()
                self._reload_matchers_if_changed()
                new_pdfs = self._find_new_pdfs(directory)

                if new_pdfs:
                    logger.info("Found %d new invoice(s) to process.", len(new_pdfs))
                    self.db.set_pipeline_queue_length(len(new_pdfs))
                    for i, pdf in enumerate(new_pdfs):
                        if _shutdown["requested"]:
                            logger.info(
                                "Shutdown requested — deferring remaining %d file(s).",
                                len(new_pdfs) - i,
                            )
                            self.db.set_pipeline_queue_length(len(new_pdfs) - i)
                            break
                        try:
                            self.process(pdf)
                            total_processed += 1
                            self.db.set_pipeline_queue_length(len(new_pdfs) - i - 1)
                        except Exception as exc:
                            logger.error(
                                "Failed to process %s: %s", pdf.name, exc, exc_info=True
                            )
                            self.db.record_failure(
                                pdf.stem, str(pdf), pdf.stat().st_mtime, str(exc)
                            )
                            self.db.set_pipeline_status("error", error=str(exc)[:500])
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
        """Return a mtime snapshot for all data/config files."""
        paths = [
            self.config.suppliers_csv, 
            self.config.po_csv, 
            self.config.po_lines_csv,
            self.config.internal_companies_json,
            PROJECT_ROOT / "config" / "standard_fields.json",
            PROJECT_ROOT / "config" / "custom_fields.json"
        ]
        return {str(p): p.stat().st_mtime for p in paths if p.exists()}

    def _reload_matchers_if_changed(self) -> None:
        """Reload supplier and PO matchers if any CSV file has changed on disk."""
        current = self._current_csv_mtimes()
        if current == self._csv_mtimes:
            return
        changed = [p for p in current if current[p] != self._csv_mtimes.get(p)]
        logger.info("CSV/JSON file(s) changed, reloading matchers: %s", changed)
        self.supplier_matcher = SupplierMatcher(self.config.suppliers_csv)
        self.internal_company_manager = InternalCompanyManager(self.config.internal_companies_json)
        self.po_matcher = POMatcher(self.config.po_csv, self.config.po_lines_csv)
        
        # Reload field config and custom fields
        reload_field_config()
        self._custom_fields_title, self._custom_fields = load_custom_fields()
        self._custom_field_extractor = CustomFieldExtractor(self._custom_fields)
        
        # Re-init validator to ensure it uses the fresh field config
        self.validator = InvoiceValidator(
            max_days_past=self.config.max_invoice_age_days,
            max_days_future=self.config.max_future_days,
            arithmetic_tolerance=self.config.arithmetic_tolerance,
            po_total_tolerance_pct=self.config.po_total_tolerance_pct,
        )
        
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

    def _run_periodic_email_poll(self) -> None:
        """Trigger an email mailbox poll if the interval has elapsed."""
        if not self.config.email_ingest_enabled:
            return

        now = datetime.now()
        interval = self.config.email_check_interval_minutes
        
        # If no last poll or interval elapsed, run now
        if not self._last_email_poll or (now - self._last_email_poll).total_seconds() >= (interval * 60):
            try:
                self.email_ingest_service.poll_mailbox()
                self._last_email_poll = now
            except Exception as e:
                logger.error("Automated email poll failed: %s", e)

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

    def _map_fields_to_coordinates(self, invoice, extraction) -> dict:
        """
        Map extracted invoice fields to their physical coordinates in the PDF.
        Returns a dict mapping field paths to a LIST of {page, bbox} objects.
        """
        elements = extraction.elements
        if not elements:
            return {}
            
        stream = TextStreamMap(elements)
        coords = {}

        def get_boxes(field_path, value, preferred_type=None):
            if value is None: return None
            
            logger.debug("Mapping field: '%s' (value: '%s', pref=%s)", field_path, value, preferred_type)
            el_indices = stream.find_matches(value)
            
            # Fallback for numbers: try matching just the integer part
            if not el_indices and (isinstance(value, (int, float)) or (isinstance(value, str) and value.replace('.','',1).isdigit())):
                int_part = str(value).split('.')[0]
                if len(int_part) > 1:
                    el_indices = stream.find_matches(int_part)

            if not el_indices:
                return None
            
            # Prioritize elements of the preferred type if multiple matches found
            if preferred_type:
                pref_indices = [idx for idx in el_indices if elements[idx].get("type") == preferred_type]
                if pref_indices:
                    el_indices = pref_indices

            boxes = []
            matching_texts = []
            for idx in el_indices:
                el = elements[idx]
                boxes.append({
                    "page": el["page"], 
                    "bbox": el["bbox"],
                    "origin": el.get("origin")
                })
                matching_texts.append(el["text"])
            
            logger.debug("  - [OK] Field '%s' matched elements: %s", field_path, matching_texts)
            return boxes

        # 1. Top-level fields
        for field in ["invoice_number", "invoice_date", "po_number", "due_date"]:
            val = getattr(invoice, field, None)
            boxes = get_boxes(field, val, preferred_type="text")
            if boxes: coords[field] = boxes

        # Totals can be in tables OR text - try both
        for field in ["total", "subtotal", "tax_amount"]:
            val = getattr(invoice, field, None)
            boxes = get_boxes(field, val) # No preference
            if boxes: coords[field] = boxes

        # 2. Supplier fields
        if invoice.supplier:
            for field in ["name", "abn", "email", "phone"]:
                val = getattr(invoice.supplier, field, None)
                boxes = get_boxes(f"supplier.{field}", val, preferred_type="text")
                if boxes: coords[f"supplier.{field}"] = boxes

        # 3. Line items - STRONGLY prefer table cells and lock by row
        for i, item in enumerate(invoice.line_items):
            # First, find the anchor element (description) to identify the table row
            description_boxes = get_boxes(f"line_items[{i}].description", item.description, preferred_type="table_cell")
            
            row_anchor = None
            if description_boxes:
                coords[f"line_items[{i}].description"] = description_boxes
                # Find the first table_cell element index to get its row/table IDs
                # (We search our elements list for one of the matched bboxes)
                for el_idx in stream.find_matches(item.description):
                    el = elements[el_idx]
                    if el.get("type") == "table_cell":
                        row_anchor = {"table": el.get("table_index"), "row": el.get("row_index")}
                        break

            item_fields = {
                "sku": item.sku,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "total": item.total
            }
            
            for field_key, val in item_fields.items():
                field_path = f"line_items[{i}].{field_key}"
                if val is None: continue
                
                # If we have a row anchor, try to find the match ONLY in that specific table row
                if row_anchor is not None:
                    # Find elements matching value that are also in the same table and row
                    possible_indices = stream.find_matches(val)
                    row_matched_indices = [
                        idx for idx in possible_indices 
                        if elements[idx].get("type") == "table_cell" and 
                           elements[idx].get("table_index") == row_anchor["table"] and 
                           elements[idx].get("row_index") == row_anchor["row"]
                    ]
                    
                    if row_matched_indices:
                        boxes = []
                        for idx in row_matched_indices:
                            el = elements[idx]
                            boxes.append({"page": el["page"], "bbox": el["bbox"], "origin": el.get("origin")})
                        coords[field_path] = boxes
                        logger.debug("  - [OK] Field '%s' row-locked to table %d row %d", field_path, row_anchor["table"], row_anchor["row"])
                        continue

                # Fallback to general search if row locking failed or no anchor found
                boxes = get_boxes(field_path, val, preferred_type="table_cell")
                if boxes:
                    coords[field_path] = boxes

        # 4. Custom fields
        if hasattr(invoice, "custom_fields") and invoice.custom_fields:
            for key, val in invoice.custom_fields.items():
                field_path = f"custom_fields.{key}"
                boxes = get_boxes(field_path, val)
                if boxes:
                    coords[field_path] = boxes

        logger.debug("Coordinate mapping complete: %d fields mapped successfully", len(coords))
        return coords


class TextStreamMap:
    """
    Builds a continuous stream of text from document elements and maintains
    a mapping back to the source elements and their coordinates.
    """
    def __init__(self, elements: list[dict]):
        self.elements = elements
        self.full_text = ""
        self.offsets = [] # List of (start, end, element_index)
        
        for i, el in enumerate(elements):
            text = el["text"]
            start = len(self.full_text)
            self.full_text += text + " " # Space to prevent merging words incorrectly
            end = len(self.full_text)
            self.offsets.append((start, end, i))

    def find_matches(self, value: str) -> list[int]:
        if value is None: return []
        val_str = str(value).strip()
        if not val_str: return []

        # Strategy 1: Exact element match (Highest priority)
        # We try the raw string first to see if it exists exactly in an element
        for i, el in enumerate(self.elements):
            if val_str in el["text"]:
                # If numeric, check boundaries
                if re.match(r'^-?\d+\.?\d*$', val_str):
                    pattern = r'(?<![0-9.])(' + re.escape(val_str) + r')(?![0-9.])'
                    if not re.search(pattern, el["text"]):
                        continue
                return [i]

        # Strategy 2: Date component matching
        if re.match(r'^\d{4}-\d{2}-\d{2}$', val_str):
            y, m, d = val_str.split('-')
            permutations = [f"{d}/{m}/{y}", f"{d}-{m}-{y}", f"{d} {m} {y}", f"{d}{m}{y}"]
            for p in permutations:
                for i, el in enumerate(self.elements):
                    if p in el["text"]: return [i]
            for p in [f"{d}{m}{y}", f"{m}{d}{y}"]:
                res = self._find_raw(p)
                if res: return res

        # Strategy 3: Numeric normalization matching (PRESERVE DOTS)
        if re.match(r'^-?\d+\.?\d*$', val_str):
            # Normalize: keep digits and dots only
            def norm_num(s):
                # Remove currency symbols and commas, but KEEP the dot
                s = re.sub(r'[^0-9.]', '', str(s))
                # Canonicalize: "100.0" -> "100", "100.00" -> "100"
                if '.' in s:
                    s = s.rstrip('0').rstrip('.')
                return s

            target_num = norm_num(val_str)
            if target_num:
                # Try exact match on normalized element text
                for i, el in enumerate(self.elements):
                    if target_num == norm_num(el["text"]):
                        return [i]
                
                # Fallback to stream match with dot preservation
                return self._find_raw(target_num, is_numeric=True, preserve_dot=True)

        # Strategy 4: Default alphanumeric search
        return self._find_raw(val_str)

    def _find_raw(self, target: str, is_numeric: bool = False, preserve_dot: bool = False) -> list[int]:
        def normalize(s):
            pattern = r'[^a-zA-Z0-9.]' if preserve_dot else r'[^a-zA-Z0-9]'
            return re.sub(pattern, '', str(s).lower())

        norm_target = normalize(target)
        if not norm_target: return []

        norm_stream = ""
        index_map = []
        for i, char in enumerate(self.full_text):
            norm_char = char.lower()
            # If preserving dots, treat dot as an alphanumeric for mapping
            if norm_char.isalnum() or (preserve_dot and norm_char == '.'):
                index_map.append(i)
                norm_stream += norm_char

        # Find first match
        idx = norm_stream.find(norm_target)
        while idx != -1:
            s_start = index_map[idx]
            s_end = index_map[idx + len(norm_target) - 1] + 1
            
            before = self.full_text[s_start-1] if s_start > 0 else ' '
            after = self.full_text[s_end] if s_end < len(self.full_text) else ' '
            
            is_valid = True
            if is_numeric:
                # Ensure we aren't matching a partial number
                if (before.isdigit()) or (after.isdigit()):
                    is_valid = False
            
            if is_valid:
                participating = []
                for start, end, el_idx in self.offsets:
                    if max(start, s_start) < min(end, s_end):
                        participating.append(el_idx)
                return participating
            
            idx = norm_stream.find(norm_target, idx + 1)

        return []

