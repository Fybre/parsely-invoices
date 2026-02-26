"""
SQLite persistence layer for the invoice processing pipeline.

Replaces the flat JSON file + .pipeline_state.json approach with a single
database file (output/pipeline.db) that:

  - Tracks processing state so watch mode never reprocesses an unchanged file
  - Stores the full extracted result for the dashboard to query
  - Supports status-based filtering (needs_review / ready / exported)
  - Stores operator corrections and notes
  - Scales to millions of invoices without any directory explosion

Status values
-------------
  needs_review  Pipeline flagged issues (discrepancies, unmatched supplier/PO,
                extraction errors). Operator must review before export.
  ready         Clean extraction — no issues found. Can be exported directly.
  exported      Export files have been written to output/export/ and the source
                PDF has been moved there.  Record kept permanently for audit.
"""
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATUS_NEEDS_REVIEW = "needs_review"
STATUS_READY        = "ready"
STATUS_EXPORTED     = "exported"
ALL_STATUSES        = {STATUS_NEEDS_REVIEW, STATUS_READY, STATUS_EXPORTED}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    stem              TEXT PRIMARY KEY,
    source_file       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'needs_review',

    -- Key fields (denormalised for fast filtering / sorting)
    invoice_number    TEXT,
    invoice_date      TEXT,
    due_date          TEXT,
    supplier_name     TEXT,
    matched_supplier  TEXT,
    total             REAL,
    currency          TEXT,
    po_number         TEXT,
    matched_po        TEXT,

    -- Quality counts
    error_count       INTEGER NOT NULL DEFAULT 0,
    warning_count     INTEGER NOT NULL DEFAULT 0,
    discrepancy_count INTEGER NOT NULL DEFAULT 0,

    -- Full result payload (InvoiceProcessingResult serialised as JSON)
    extracted_data    TEXT NOT NULL,

    -- Operator field corrections: JSON object  { "field_path": "corrected_value", … }
    corrections       TEXT,

    -- Timestamps (ISO-8601 strings)
    processed_at      TEXT NOT NULL,
    processing_time_seconds REAL,
    llm_model_used    TEXT,
    exported_at       TEXT,

    -- Operator notes (free text)
    notes             TEXT,

    -- Source file mtime at processing time (for reprocess detection)
    source_mtime      REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_invoices_status       ON invoices (status);
CREATE INDEX IF NOT EXISTS idx_invoices_processed_at ON invoices (processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_invoices_supplier     ON invoices (supplier_name);
CREATE INDEX IF NOT EXISTS idx_invoices_invoice_num  ON invoices (invoice_number);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stem        TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,   -- ISO-8601 UTC
    action      TEXT    NOT NULL,   -- processed | processing_failed | status_changed |
                                    -- corrections_saved | notes_updated | exported | deleted
    actor       TEXT    NOT NULL DEFAULT 'system',  -- username, 'system', or 'anonymous'
    detail      TEXT                -- optional JSON blob with action-specific context
);

CREATE INDEX IF NOT EXISTS idx_audit_stem      ON audit_log (stem);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log (timestamp DESC);
"""


class Database:
    """Thin wrapper around an SQLite database file for invoice pipeline state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
        logger.debug("Database schema ready: %s", self.db_path)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def upsert_invoice(
        self,
        stem: str,
        result_dict: dict,
        source_file: str,
        source_mtime: float,
    ) -> str:
        """
        Insert or replace an invoice record after successful extraction.

        On reprocess (same stem, changed mtime) the record is fully replaced
        and corrections/notes are cleared.

        Returns the assigned status ('needs_review' or 'ready').
        """
        inv      = result_dict.get("extracted_invoice") or {}
        supplier = inv.get("supplier") or {}
        ms       = result_dict.get("matched_supplier") or {}
        mp       = result_dict.get("matched_po") or {}
        discs    = result_dict.get("discrepancies") or []

        status = (
            STATUS_NEEDS_REVIEW
            if result_dict.get("requires_review", False)
            else STATUS_READY
        )

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO invoices (
                    stem, source_file, status,
                    invoice_number, invoice_date, due_date,
                    supplier_name, matched_supplier,
                    total, currency, po_number, matched_po,
                    error_count, warning_count, discrepancy_count,
                    extracted_data,
                    processed_at, processing_time_seconds, llm_model_used,
                    source_mtime
                ) VALUES (
                    :stem, :source_file, :status,
                    :invoice_number, :invoice_date, :due_date,
                    :supplier_name, :matched_supplier,
                    :total, :currency, :po_number, :matched_po,
                    :error_count, :warning_count, :discrepancy_count,
                    :extracted_data,
                    :processed_at, :processing_time_seconds, :llm_model_used,
                    :source_mtime
                )
                ON CONFLICT(stem) DO UPDATE SET
                    source_file             = excluded.source_file,
                    status                  = excluded.status,
                    invoice_number          = excluded.invoice_number,
                    invoice_date            = excluded.invoice_date,
                    due_date                = excluded.due_date,
                    supplier_name           = excluded.supplier_name,
                    matched_supplier        = excluded.matched_supplier,
                    total                   = excluded.total,
                    currency                = excluded.currency,
                    po_number               = excluded.po_number,
                    matched_po              = excluded.matched_po,
                    error_count             = excluded.error_count,
                    warning_count           = excluded.warning_count,
                    discrepancy_count       = excluded.discrepancy_count,
                    extracted_data          = excluded.extracted_data,
                    corrections             = NULL,
                    notes                   = NULL,
                    processed_at            = excluded.processed_at,
                    processing_time_seconds = excluded.processing_time_seconds,
                    llm_model_used          = excluded.llm_model_used,
                    exported_at             = NULL,
                    source_mtime            = excluded.source_mtime
                """,
                {
                    "stem":             stem,
                    "source_file":      source_file,
                    "status":           status,
                    "invoice_number":   inv.get("invoice_number"),
                    "invoice_date":     inv.get("invoice_date"),
                    "due_date":         inv.get("due_date"),
                    "supplier_name":    supplier.get("name"),
                    "matched_supplier": ms.get("supplier_name"),
                    "total":            inv.get("total"),
                    "currency":         inv.get("currency"),
                    "po_number":        inv.get("po_number"),
                    "matched_po":       mp.get("po_number"),
                    "error_count":      result_dict.get("error_count", 0),
                    "warning_count":    result_dict.get("warning_count", 0),
                    "discrepancy_count": len(discs),
                    "extracted_data":   json.dumps(result_dict),
                    "processed_at":     result_dict.get(
                        "processed_at",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                    "processing_time_seconds": result_dict.get("processing_time_seconds"),
                    "llm_model_used":   result_dict.get("llm_model_used"),
                    "source_mtime":     source_mtime,
                },
            )

        logger.info("DB upserted: %s  status=%s", stem, status)
        self.log_audit(stem, "processed", actor="system", detail={"status": status})
        return status

    def record_failure(
        self,
        stem: str,
        source_file: str,
        source_mtime: float,
        error: str,
    ) -> None:
        """
        Record a failed extraction.

        Stores a minimal record so the watch loop does not retry an
        unprocessable file on every poll cycle.  The record is treated as
        needs_review so the operator can see it in the dashboard.
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO invoices (
                    stem, source_file, status,
                    error_count, extracted_data,
                    processed_at, source_mtime
                ) VALUES (?, ?, 'needs_review', 1, ?, ?, ?)
                ON CONFLICT(stem) DO UPDATE SET
                    status         = 'needs_review',
                    error_count    = 1,
                    extracted_data = excluded.extracted_data,
                    corrections    = NULL,
                    processed_at   = excluded.processed_at,
                    source_mtime   = excluded.source_mtime
                """,
                (
                    stem,
                    source_file,
                    json.dumps({
                        "error": error,
                        "source_file": source_file,
                        "requires_review": True,
                        "error_count": 1,
                        "warning_count": 0,
                        "discrepancies": [],
                    }),
                    datetime.now(timezone.utc).isoformat(),
                    source_mtime,
                ),
            )
        self.log_audit(stem, "processing_failed", actor="system", detail={"error": error[:300]})

    def update_status(self, stem: str, status: str) -> bool:
        """
        Set the status of an invoice.  Records exported_at when moving to
        'exported'.  Returns True if the record was found.
        """
        if status not in ALL_STATUSES:
            raise ValueError(f"Invalid status {status!r}. Must be one of {ALL_STATUSES}")

        with self._conn() as conn:
            if status == STATUS_EXPORTED:
                conn.execute(
                    "UPDATE invoices SET status=?, exported_at=? WHERE stem=?",
                    (status, datetime.now(timezone.utc).isoformat(), stem),
                )
            else:
                conn.execute(
                    "UPDATE invoices SET status=? WHERE stem=?",
                    (status, stem),
                )
            changed = conn.execute("SELECT changes()").fetchone()[0]

        return changed > 0

    def update_corrections(self, stem: str, corrections: dict) -> bool:
        """Save operator field corrections (merged dict of field → value)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE invoices SET corrections=? WHERE stem=?",
                (json.dumps(corrections), stem),
            )
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def update_notes(self, stem: str, notes: str) -> bool:
        """Save operator free-text notes for an invoice."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE invoices SET notes=? WHERE stem=?",
                (notes, stem),
            )
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def log_audit(
        self,
        stem: str,
        action: str,
        actor: str = "system",
        detail: Optional[dict] = None,
    ) -> None:
        """Append one entry to the audit log."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO audit_log (stem, timestamp, action, actor, detail)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    stem,
                    datetime.now(timezone.utc).isoformat(),
                    action,
                    actor,
                    json.dumps(detail) if detail is not None else None,
                ),
            )

    def reset_for_reprocess(self, stem: str) -> bool:
        """
        Reset an invoice so the pipeline watcher picks it up for reprocessing.

        Sets source_mtime=0 so is_processed() returns False on next poll,
        resets status to needs_review, and clears corrections and exported_at.
        """
        with self._conn() as conn:
            conn.execute(
                """UPDATE invoices SET
                    source_mtime = 0,
                    status       = 'needs_review',
                    corrections  = NULL,
                    exported_at  = NULL
                WHERE stem = ?""",
                (stem,),
            )
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def delete_invoice(self, stem: str) -> bool:
        """Delete an invoice record from the database entirely."""
        with self._conn() as conn:
            conn.execute("DELETE FROM invoices WHERE stem = ?", (stem,))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def is_processed(self, stem: str, source_mtime: float) -> bool:
        """
        Return True if this invoice has already been processed with the same
        source file mtime.  A changed mtime means the file was re-dropped and
        should be reprocessed.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT source_mtime, status FROM invoices WHERE stem=?", (stem,)
            ).fetchone()

        if row is None:
            return False

        # Always reprocess if the file has changed
        if abs(row["source_mtime"] - source_mtime) >= 0.5:
            logger.info("File mtime changed — will reprocess: %s", stem)
            return False

        return True

    def get_invoice(self, stem: str) -> Optional[dict]:
        """Return the full invoice record (all columns) or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE stem=?", (stem,)
            ).fetchone()
        return dict(row) if row else None

    def list_invoices(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        """
        Return invoice summaries (no extracted_data blob) ordered newest-first.

        Args:
            status:  Filter by status value, or None for all.
            search:  Case-insensitive substring match on invoice_number,
                     supplier_name, or matched_supplier.
            limit:   Max rows to return.
            offset:  Pagination offset.
        """
        clauses: list[str] = []
        params: list = []

        if status:
            clauses.append("status = ?")
            params.append(status)
        if search:
            clauses.append(
                "(invoice_number LIKE ? OR supplier_name LIKE ? OR matched_supplier LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])

        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    stem, source_file, status,
                    invoice_number, invoice_date, due_date,
                    supplier_name, matched_supplier,
                    total, currency, po_number, matched_po,
                    error_count, warning_count, discrepancy_count,
                    processed_at, processing_time_seconds, llm_model_used,
                    exported_at, notes
                FROM invoices
                {where}
                ORDER BY processed_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()

        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Return aggregate counts by status plus totals."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*)  AS total,
                    SUM(CASE WHEN status = 'needs_review' THEN 1 ELSE 0 END) AS needs_review,
                    SUM(CASE WHEN status = 'ready'        THEN 1 ELSE 0 END) AS ready,
                    SUM(CASE WHEN status = 'exported'     THEN 1 ELSE 0 END) AS exported,
                    SUM(error_count)   AS total_errors,
                    SUM(warning_count) AS total_warnings,
                    MAX(processed_at)  AS last_processed
                FROM invoices
                """
            ).fetchone()
        return dict(row) if row else {}

    def get_audit_log(self, stem: str) -> list[dict]:
        """Return all audit entries for one invoice, oldest first."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, timestamp, action, actor, detail
                   FROM audit_log WHERE stem = ?
                   ORDER BY timestamp ASC, id ASC""",
                (stem,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_audit_log(self, limit: int = 200, offset: int = 0) -> list[dict]:
        """Return recent audit entries across all invoices, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, stem, timestamp, action, actor, detail
                   FROM audit_log
                   ORDER BY timestamp DESC, id DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

