"""
Invoice Pipeline Dashboard — FastAPI backend.

Serves a single-page dashboard for viewing, reviewing, correcting, and
exporting extracted invoice results alongside the original PDF.

All invoice state lives in a single SQLite database (output/pipeline.db).
No individual JSON files are written; the export/ folder only ever contains
files that the operator has explicitly approved for the backend system.

Endpoints
---------
  GET  /                                → SPA HTML shell
  GET  /api/stats                       → aggregate counts by status
  GET  /api/invoices                    → list summaries (supports ?status= and ?search=)
  GET  /api/invoices/{stem}             → full result for one invoice
  GET  /api/invoices/{stem}/pages       → PDF pages as base64 JPEG images
  POST /api/invoices/{stem}/export      → approve: write export files, move PDF
  POST /api/invoices/{stem}/reprocess   → reset mtime so pipeline re-runs LLM extraction
  DELETE /api/invoices/{stem}           → delete PDF + DB record (non-exported only)
  POST /api/bulk-export                 → export ALL ready invoices in one call
  PATCH /api/invoices/{stem}/status     → set status (needs_review / ready)
  PATCH /api/invoices/{stem}/corrections → save operator field corrections
  PATCH /api/invoices/{stem}/notes      → save operator notes
  POST /api/upload                      → upload a new invoice PDF to the inbox
  GET  /api/health                      → liveness probe
"""
import base64
import io
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Bootstrap: add pipeline package to path so we can import Database
# ---------------------------------------------------------------------------
_PIPELINE_DIR = Path(__file__).parent.parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

from pipeline.database import Database, STATUS_NEEDS_REVIEW, STATUS_READY  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Corrections helpers
# ---------------------------------------------------------------------------

def _apply_corrections(extracted: dict, corrections: dict) -> dict:
    """
    Return a deep copy of *extracted* with operator corrections applied.

    Corrections dict keys (all optional):
      invoice_number, invoice_date, due_date, po_number, currency,
      subtotal, tax_amount, total          → extracted_invoice.<key>
      supplier_name, supplier_abn,
      supplier_email, supplier_phone,
      supplier_address                     → extracted_invoice.supplier.<key>
      line_items                           → extracted_invoice.line_items (full replacement)
    """
    import copy
    if not corrections:
        return extracted

    result = copy.deepcopy(extracted)
    inv = result.setdefault("extracted_invoice", {})
    sup = inv.setdefault("supplier", {})

    # Scalar invoice fields
    for key in ("invoice_number", "invoice_date", "due_date", "po_number",
                "currency", "subtotal", "tax_amount", "total"):
        if key in corrections:
            inv[key] = corrections[key]

    # Supplier sub-fields
    sup_map = {
        "supplier_name":    "name",
        "supplier_abn":     "abn",
        "supplier_email":   "email",
        "supplier_phone":   "phone",
        "supplier_address": "address",
    }
    for corr_key, inv_key in sup_map.items():
        if corr_key in corrections:
            sup[inv_key] = corrections[corr_key]

    # Line items — full replacement when present
    if "line_items" in corrections:
        inv["line_items"] = corrections["line_items"]

    return result

# ---------------------------------------------------------------------------
# Config from environment (mirrors pipeline config so volumes line up)
# ---------------------------------------------------------------------------
OUTPUT_DIR    = Path(os.getenv("OUTPUT_DIR",   "/app/output"))
INVOICES_DIR  = Path(os.getenv("INVOICES_DIR", "/app/invoices"))
EXPORT_DIR    = Path(os.getenv("EXPORT_DIR",   str(OUTPUT_DIR / "export")))
DB_PATH       = Path(os.getenv("DB_PATH",      str(OUTPUT_DIR / "pipeline.db")))
DASHBOARD_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Database (lazy — opened on first request so startup doesn't fail if DB
# hasn't been created yet by the pipeline)
# ---------------------------------------------------------------------------
_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        _db = Database(DB_PATH)
    return _db


# ---------------------------------------------------------------------------
# In-memory PDF page-render cache  {stem: {"mtime": float, "pages": list}}
# ---------------------------------------------------------------------------
_PAGE_CACHE: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Invoice Pipeline Dashboard", docs_url=None, redoc_url=None)


# ── PDF helpers ──────────────────────────────────────────────────────────────

def _find_pdf(source_file: str, stem: str) -> Optional[Path]:
    """
    Locate the original PDF.

    Search order:
      1. The exact path recorded at extraction time
      2. INVOICES_DIR/<filename>   (PDF still in inbox)
      3. EXPORT_DIR/<stem>.pdf     (PDF moved to export folder after approval)
    """
    if source_file:
        p = Path(source_file)
        if p.exists():
            return p
        candidate = INVOICES_DIR / p.name
        if candidate.exists():
            return candidate

    # Try export dir (invoice was approved)
    export_pdf = EXPORT_DIR / f"{stem}.pdf"
    if export_pdf.exists():
        return export_pdf

    return None


def _render_pages(pdf_path: Path, max_pages: int = 12) -> list[dict]:
    """Render PDF pages to JPEG and return as base64-encoded dicts."""
    try:
        from pdf2image import convert_from_path  # type: ignore
        images = convert_from_path(
            str(pdf_path), dpi=150,
            first_page=1, last_page=max_pages,
        )
        pages = []
        for i, img in enumerate(images):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            pages.append({
                "page": i + 1,
                "mime": "image/jpeg",
                "data": base64.b64encode(buf.getvalue()).decode(),
            })
        return pages
    except ImportError:
        pass  # fall through to pdfplumber

    try:
        import pdfplumber  # type: ignore
        pages = []
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages[:max_pages]):
                img = page.to_image(resolution=150)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                pages.append({
                    "page": i + 1,
                    "mime": "image/png",
                    "data": base64.b64encode(buf.getvalue()).decode(),
                })
        return pages
    except Exception as e:
        logger.error("Failed to render PDF %s: %s", pdf_path, e)
        return []


# ── Request / response models ─────────────────────────────────────────────────

class StatusUpdate(BaseModel):
    status: str   # needs_review | ready


class CorrectionsUpdate(BaseModel):
    corrections: dict   # { "field_path": "corrected_value", … }


class NotesUpdate(BaseModel):
    notes: str


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "output_dir":   str(OUTPUT_DIR),
        "invoices_dir": str(INVOICES_DIR),
        "export_dir":   str(EXPORT_DIR),
        "db_path":      str(DB_PATH),
        "db_exists":    DB_PATH.exists(),
    }


@app.get("/api/stats")
def stats():
    return get_db().get_stats()


@app.get("/api/invoices")
def list_invoices(
    status: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    limit: int = Query(default=500, le=2000),
    offset: int = Query(default=0, ge=0),
):
    return get_db().list_invoices(
        status=status or None,
        search=search or None,
        limit=limit,
        offset=offset,
    )


@app.get("/api/invoices/{stem}")
def get_invoice(stem: str):
    rec = get_db().get_invoice(stem)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Invoice not found: {stem}")

    # Merge extracted_data blob with DB metadata so the frontend gets everything
    try:
        extracted = json.loads(rec.get("extracted_data") or "{}")
    except Exception:
        extracted = {}

    corrections = {}
    if rec.get("corrections"):
        try:
            corrections = json.loads(rec["corrections"])
        except Exception:
            pass

    return {
        **extracted,
        # DB-level fields overlay (authoritative)
        "stem":        stem,
        "status":      rec["status"],
        "corrections": corrections,
        "notes":       rec.get("notes"),
        "exported_at": rec.get("exported_at"),
        "processed_at": rec.get("processed_at") or extracted.get("processed_at"),
    }


@app.get("/api/invoices/{stem}/pages")
def get_pages(stem: str):
    rec = get_db().get_invoice(stem)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Invoice not found: {stem}")

    source_file = rec.get("source_file", "")
    pdf_path = _find_pdf(source_file, stem)
    if pdf_path is None:
        return []

    mtime = pdf_path.stat().st_mtime
    cached = _PAGE_CACHE.get(stem)
    if cached and abs(cached["mtime"] - mtime) < 0.5:
        return cached["pages"]

    pages = _render_pages(pdf_path)
    _PAGE_CACHE[stem] = {"mtime": mtime, "pages": pages}
    return pages


@app.post("/api/invoices/{stem}/export")
def export_invoice(stem: str):
    """
    Approve an invoice for export.

    Writes <stem>.json and moves <stem>.pdf to output/export/.
    Updates DB status to 'exported'.  Idempotent — calling again on an
    already-exported invoice returns a 400.
    """
    db = get_db()
    rec = db.get_invoice(stem)
    if not rec:
        raise HTTPException(404, f"Invoice not found: {stem}")
    if rec["status"] == "exported":
        raise HTTPException(400, "Invoice is already exported")

    # Locate source PDF
    pdf_path = _find_pdf(rec.get("source_file", ""), stem)
    if pdf_path is None:
        raise HTTPException(404, "Source PDF not found — cannot export")

    # Build export payload: full extracted data with corrections applied
    try:
        extracted = json.loads(rec.get("extracted_data") or "{}")
    except Exception:
        extracted = {}

    corrections = {}
    if rec.get("corrections"):
        try:
            corrections = json.loads(rec["corrections"])
        except Exception:
            pass

    export_payload = {
        "stem":                stem,
        "exported_at":         datetime.now(timezone.utc).isoformat(),
        "corrections_applied": bool(corrections),
        "corrections":         corrections,
        "operator_notes":      rec.get("notes"),
        **_apply_corrections(extracted, corrections),
    }

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    export_json_path = EXPORT_DIR / f"{stem}.json"
    export_pdf_path  = EXPORT_DIR / f"{stem}.pdf"

    # Write JSON
    with open(export_json_path, "w", encoding="utf-8") as f:
        json.dump(export_payload, f, indent=2, default=str)

    # Move PDF
    shutil.move(str(pdf_path), str(export_pdf_path))

    # Invalidate PDF render cache (path changed)
    _PAGE_CACHE.pop(stem, None)

    # Update DB
    db.update_status(stem, "exported")

    logger.info("Exported: %s → %s", stem, EXPORT_DIR)
    return {
        "status":      "exported",
        "export_json": str(export_json_path),
        "export_pdf":  str(export_pdf_path),
    }


@app.post("/api/invoices/{stem}/reprocess")
def reprocess_invoice(stem: str):
    """
    Queue an invoice for re-processing by the pipeline.

    Deletes the DB record while leaving the source PDF in the invoices
    inbox.  The pipeline watcher will treat it as a brand-new file on its
    next poll cycle and re-run full LLM extraction from scratch.

    The invoice disappears from the dashboard until processing completes,
    then reappears as a fresh entry — just like a newly dropped file.

    Not permitted for exported invoices.
    """
    db = get_db()
    rec = db.get_invoice(stem)
    if not rec:
        raise HTTPException(404, f"Invoice not found: {stem}")
    if rec["status"] == "exported":
        raise HTTPException(400, "Cannot reprocess an exported invoice")

    # Remove the DB record — pipeline will re-pick-up the PDF on next poll
    db.delete_invoice(stem)
    _PAGE_CACHE.pop(stem, None)
    logger.info("Queued for reprocess (record cleared): %s", stem)
    return {"stem": stem, "status": "queued_for_reprocess"}


@app.delete("/api/invoices/{stem}")
def delete_invoice(stem: str):
    """
    Permanently delete an invoice — removes the source PDF and DB record.

    Not permitted for exported invoices (the export/ folder is the record
    of what has been approved; those files must be managed separately).
    """
    db = get_db()
    rec = db.get_invoice(stem)
    if not rec:
        raise HTTPException(404, f"Invoice not found: {stem}")
    if rec["status"] == "exported":
        raise HTTPException(400, "Cannot delete an exported invoice")

    # Delete source PDF if it still exists in the invoices inbox
    pdf_path = _find_pdf(rec.get("source_file", ""), stem)
    if pdf_path and pdf_path.exists():
        # Safety: only delete from the invoices inbox, not from export/
        if EXPORT_DIR not in pdf_path.parents:
            pdf_path.unlink()
            logger.info("Deleted PDF: %s", pdf_path)

    # Remove DB record and invalidate cache
    db.delete_invoice(stem)
    _PAGE_CACHE.pop(stem, None)
    logger.info("Deleted invoice record: %s", stem)
    return {"stem": stem, "deleted": True}


@app.patch("/api/invoices/{stem}/status")
def update_status(stem: str, body: StatusUpdate):
    """
    Set status back to needs_review or ready.
    (Cannot set to 'exported' — use the /export endpoint for that.)
    """
    allowed = {STATUS_NEEDS_REVIEW, STATUS_READY}
    if body.status not in allowed:
        raise HTTPException(400, f"Status must be one of: {allowed}")

    db = get_db()
    if not db.update_status(stem, body.status):
        raise HTTPException(404, f"Invoice not found: {stem}")
    return {"stem": stem, "status": body.status}


@app.patch("/api/invoices/{stem}/corrections")
def update_corrections(stem: str, body: CorrectionsUpdate):
    """Save operator field corrections for an invoice."""
    db = get_db()
    if not db.update_corrections(stem, body.corrections):
        raise HTTPException(404, f"Invoice not found: {stem}")
    return {"stem": stem, "corrections": body.corrections}


@app.post("/api/bulk-export")
def bulk_export():
    """
    Export all invoices currently in 'ready' status in one operation.

    For each ready invoice: writes <stem>.json, moves <stem>.pdf to export/,
    and sets status to 'exported'.  Failures on individual invoices are
    collected and returned without aborting the rest.
    """
    db = get_db()
    ready_invoices = db.list_invoices(status=STATUS_READY, limit=10_000)

    exported = 0
    errors: list[str] = []

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()

    for inv in ready_invoices:
        stem = inv["stem"]
        try:
            rec = db.get_invoice(stem)
            if not rec or rec["status"] != STATUS_READY:
                continue  # race condition: another request got there first

            pdf_path = _find_pdf(rec.get("source_file", ""), stem)
            if pdf_path is None:
                errors.append(f"{stem}: PDF not found")
                continue

            extracted: dict = {}
            try:
                extracted = json.loads(rec.get("extracted_data") or "{}")
            except Exception:
                pass

            corrections: dict = {}
            if rec.get("corrections"):
                try:
                    corrections = json.loads(rec["corrections"])
                except Exception:
                    pass

            export_payload = {
                "stem":                stem,
                "exported_at":         now,
                "corrections_applied": bool(corrections),
                "corrections":         corrections,
                "operator_notes":      rec.get("notes"),
                **_apply_corrections(extracted, corrections),
            }

            export_json_path = EXPORT_DIR / f"{stem}.json"
            export_pdf_path  = EXPORT_DIR / f"{stem}.pdf"

            with open(export_json_path, "w", encoding="utf-8") as f:
                json.dump(export_payload, f, indent=2, default=str)

            shutil.move(str(pdf_path), str(export_pdf_path))
            _PAGE_CACHE.pop(stem, None)
            db.update_status(stem, "exported")
            exported += 1

        except Exception as e:
            errors.append(f"{stem}: {e}")
            logger.error("Bulk export failed for %s: %s", stem, e)

    logger.info("Bulk export complete: %d exported, %d errors", exported, len(errors))
    return {"exported": exported, "errors": errors}


@app.patch("/api/invoices/{stem}/notes")
def update_notes(stem: str, body: NotesUpdate):
    """Save operator free-text notes."""
    db = get_db()
    if not db.update_notes(stem, body.notes):
        raise HTTPException(404, f"Invoice not found: {stem}")
    return {"stem": stem, "notes": body.notes}


@app.post("/api/upload")
async def upload_invoice(file: UploadFile = File(...)):
    """
    Upload a new invoice PDF to the invoices inbox.

    The file is saved to INVOICES_DIR with a sanitised filename.  If a file
    with the same name already exists a numeric suffix is appended
    (e.g. invoice_1.pdf).  The pipeline watcher will pick it up on its next
    poll cycle and process it automatically.
    """
    # Validate MIME / extension
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted (.pdf extension required)")

    # Sanitise the stem: keep word chars, hyphens, dots; replace everything else
    raw_stem = Path(filename).stem
    safe_stem = re.sub(r"[^\w\-.]", "_", raw_stem).strip("_") or "invoice"
    dest = INVOICES_DIR / f"{safe_stem}.pdf"

    # Avoid overwriting an existing file
    counter = 1
    while dest.exists():
        dest = INVOICES_DIR / f"{safe_stem}_{counter}.pdf"
        counter += 1

    INVOICES_DIR.mkdir(parents=True, exist_ok=True)

    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(400, "Uploaded file is empty")

    dest.write_bytes(contents)
    logger.info("Invoice uploaded to inbox: %s (%d bytes)", dest, len(contents))

    return {
        "stem":     dest.stem,
        "filename": dest.name,
        "size":     len(contents),
        "status":   "queued",
    }


# ── SPA ───────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    html_path = DASHBOARD_DIR / "templates" / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="Dashboard template not found")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma":        "no-cache",
            "Expires":       "0",
        },
    )
