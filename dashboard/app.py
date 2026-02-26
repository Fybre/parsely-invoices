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
  GET  /admin                           → Admin management page
  GET  /login                           → Login form (always public)
  POST /login                           → Validate credentials, set session cookie
  POST /logout                          → Clear session cookie
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

Authentication (AUTH_MODE env var)
-----------------------------------
  disabled    — no auth, open access (default)
  admin_only  — /admin and /api/admin/* require admin role; dashboard is open
  full        — all routes require login; admin routes additionally require admin role

Users are stored in config/users.json with bcrypt-hashed passwords.
Generate a hash:
  docker compose run --rm dashboard python3 -c \\
    "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
"""
import base64
import csv
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# Configure logging to output to stdout for Docker
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import BaseLoader, Environment, FileSystemLoader
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Bootstrap: add pipeline package to path so we can import Database
# ---------------------------------------------------------------------------
_PIPELINE_DIR = Path(__file__).parent.parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

from pipeline.database import Database, STATUS_NEEDS_REVIEW, STATUS_READY  # noqa: E402
from config import Config  # noqa: E402
from pipeline.webhook_export import WebhookExportService  # noqa: E402
from pipeline.email_ingest import EmailIngestService  # noqa: E402
from pipeline.csv_manager import csv_manager  # noqa: E402

# Import dashboard services
from dashboard.models import (
    StatusUpdate,
    CorrectionsUpdate,
    NotesUpdate,
    AdminDataUpdate,
    UserCreate,
    UserUpdate,
    SupplierCreate,
)
from dashboard.services import (
    apply_corrections,
    build_normalized_supplier,
    build_normalized_line_items,
    render_export_xml,
)
from dashboard.services import pdf as pdf_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth config
# ---------------------------------------------------------------------------
AUTH_MODE            = os.getenv("AUTH_MODE", "disabled")   # disabled|admin_only|full
AUTH_SECRET_KEY      = os.getenv("AUTH_SECRET_KEY", "")
AUTH_SESSION_MINUTES = int(os.getenv("AUTH_SESSION_MINUTES", "480"))
AUTH_USERS_FILE      = Path(os.getenv("CONFIG_DIR", str(_PIPELINE_DIR / "config"))) / "users.json"
_AUTH_SESSION_COOKIE = "parsely_session"

# ---------------------------------------------------------------------------
# Supplier creation config
# ---------------------------------------------------------------------------
ALLOW_CREATE_SUPPLIER = os.getenv("ALLOW_CREATE_SUPPLIER", "false").lower() == "true"
SUPPLIER_CODE_PREFIX  = os.getenv("SUPPLIER_CODE_PREFIX", "SUP-")

# ---------------------------------------------------------------------------
# Pipeline settings (admin-editable, persisted to config/pipeline_settings.json)
# ---------------------------------------------------------------------------
_PIPELINE_SETTINGS_FILE = (
    Path(os.getenv("CONFIG_DIR", str(_PIPELINE_DIR / "config"))) / "pipeline_settings.json"
)

_SETTINGS_SCHEMA: list[dict] = [
    {
        "key":         "arithmetic_tolerance",
        "group":       "Pipeline Thresholds",
        "type":        "float",
        "label":       "Arithmetic Tolerance",
        "description": "Maximum rounding error (dollars) allowed in invoice totals before flagging a discrepancy",
        "unit":        "$",
        "default":     0.05,
        "min":         0.0,
        "max":         10.0,
        "step":        0.01,
    },
    {
        "key":         "max_invoice_age_days",
        "group":       "Pipeline Thresholds",
        "type":        "int",
        "label":       "Max Invoice Age",
        "description": "Warn if invoice date is older than this many days",
        "unit":        "days",
        "default":     90,
        "min":         1,
        "max":         3650,
        "step":        1,
    },
    {
        "key":         "max_future_days",
        "group":       "Pipeline Thresholds",
        "type":        "int",
        "label":       "Max Future Date",
        "description": "Flag an error if invoice date is this many days ahead of today",
        "unit":        "days",
        "default":     7,
        "min":         0,
        "max":         365,
        "step":        1,
    },
    {
        "key":         "supplier_fuzzy_threshold",
        "group":       "Pipeline Thresholds",
        "type":        "int",
        "label":       "Supplier Match Threshold",
        "description": "Minimum fuzzy similarity score (0–100) for supplier name matching",
        "unit":        "/ 100",
        "default":     75,
        "min":         0,
        "max":         100,
        "step":        1,
    },
    {
        "key":         "po_line_fuzzy_threshold",
        "group":       "Pipeline Thresholds",
        "type":        "int",
        "label":       "PO Line Match Threshold",
        "description": "Minimum fuzzy similarity score (0–100) for PO line item description matching",
        "unit":        "/ 100",
        "default":     65,
        "min":         0,
        "max":         100,
        "step":        1,
    },
    {
        "key":         "webhook_export_enabled",
        "group":       "Webhook Export",
        "type":        "bool",
        "ui_type":     "bool",
        "label":       "Enable Webhook Export",
        "description": "Globally enable or disable the external integration push upon export",
        "default":     True,
    },
    {
        "key":         "webhook_export_url",
        "group":       "Webhook Export",
        "type":        "str",
        "ui_type":     "long_str",
        "label":       "Webhook Export URL",
        "description": "Full REST API endpoint (e.g. https://acme.thereforeonline.com/theservice/v0001/restun/CreateDocument)",
        "default":     "",
    },
    {
        "key":         "webhook_export_method",
        "group":       "Webhook Export",
        "type":        "str",
        "ui_type":     "dropdown",
        "options":     ["POST", "PUT", "PATCH", "GET"],
        "label":       "Webhook Export Method",
        "description": "HTTP verb (POST, PUT, etc.)",
        "default":     "POST",
    },
    {
        "key":         "webhook_export_headers_json",
        "group":       "Webhook Export",
        "type":        "str",
        "ui_type":     "textarea",
        "label":       "Webhook Export Headers (JSON)",
        "description": "Custom HTTP headers as a JSON object (e.g. {\"TenantName\": \"acme\", \"X-API-Key\": \"...\"})",
        "default":     "{}",
    },
    {
        "key":         "webhook_export_template",
        "group":       "Webhook Export",
        "type":        "str",
        "ui_type":     "dropdown",
        "options_key": "webhook_export_templates",
        "label":       "Webhook Export Template",
        "description": "Jinja2 template filename located in the config folder",
        "default":     "webhook_export_template.json.j2",
    },
    {
        "key":         "webhook_export_enable_pdf",
        "group":       "Webhook Export",
        "type":        "bool",
        "ui_type":     "bool",
        "label":       "Send PDF in Webhook Export",
        "description": "Whether to include the Base64-encoded PDF in the payload context (pdf_base64)",
        "default":     False,
    },
    {
        "key":         "backup_enabled",
        "group":       "Automated Backups",
        "type":        "bool",
        "ui_type":     "bool",
        "label":       "Enable Automated Backups",
        "description": "Automatically create a timestamped ZIP backup of database, config, and data files",
        "default":     True,
    },
    {
        "key":         "backup_interval_hours",
        "group":       "Automated Backups",
        "type":        "int",
        "label":       "Backup Interval (Hours)",
        "description": "How often to run the automated backup",
        "unit":        "hours",
        "default":     24,
        "min":         1,
        "max":         8760,
        "step":        1,
    },
    {
        "key":         "backup_retention_count",
        "group":       "Automated Backups",
        "type":        "int",
        "label":       "Backup Retention",
        "description": "Number of recent backup archives to keep",
        "unit":        "files",
        "default":     7,
        "min":         1,
        "max":         100,
        "step":        1,
    },
    {
        "key":         "email_ingest_enabled",
        "group":       "Email Ingestion",
        "type":        "bool",
        "ui_type":     "bool",
        "label":       "Enable Email Ingestion",
        "description": "Poll a mailbox for new invoices arriving as PDF attachments",
        "default":     False,
    },
    {
        "key":         "email_imap_host",
        "group":       "Email Ingestion",
        "type":        "str",
        "ui_type":     "long_str",
        "label":       "IMAP Host",
        "description": "Hostname of your IMAP server (e.g. imap.gmail.com)",
        "default":     "",
    },
    {
        "key":         "email_imap_port",
        "group":       "Email Ingestion",
        "type":        "int",
        "label":       "IMAP Port",
        "description": "IMAP server port (usually 993 for SSL)",
        "default":     993,
    },
    {
        "key":         "email_imap_user",
        "group":       "Email Ingestion",
        "type":        "str",
        "ui_type":     "long_str",
        "label":       "IMAP Username",
        "description": "Your full email address or username",
        "default":     "",
    },
    {
        "key":         "email_imap_password",
        "group":       "Email Ingestion",
        "type":        "str",
        "ui_type":     "password",
        "label":       "IMAP Password",
        "description": "Mailbox password (use App Passwords for Gmail/M365)",
        "default":     "",
    },
    {
        "key":         "email_use_ssl",
        "group":       "Email Ingestion",
        "type":        "bool",
        "ui_type":     "bool",
        "label":       "Use SSL/TLS",
        "description": "Whether to use a secure connection (recommended)",
        "default":     True,
    },
    {
        "key":         "email_mailbox",
        "group":       "Email Ingestion",
        "type":        "str",
        "ui_type":     "str",
        "label":       "Mailbox Folder",
        "description": "The folder to scan for unread messages",
        "default":     "INBOX",
    },
    {
        "key":         "email_search_criteria",
        "group":       "Email Ingestion",
        "type":        "str",
        "ui_type":     "dropdown",
        "options":     ["UNSEEN", "ALL"],
        "label":       "Search Criteria",
        "description": "UNSEEN = unread only; ALL = process everything in folder (requires Processed Folder to avoid loops)",
        "default":     "UNSEEN",
    },
    {
        "key":         "email_processed_mailbox",
        "group":       "Email Ingestion",
        "type":        "str",
        "ui_type":     "str",
        "label":       "Processed Folder",
        "description": "Move emails here after successful extraction (e.g. 'Processed'). Recommended for robustness.",
        "default":     "",
    },
    {
        "key":         "email_check_interval_minutes",
        "group":       "Email Ingestion",
        "type":        "int",
        "label":       "Poll Interval (Minutes)",
        "description": "How often to check for new emails",
        "unit":        "min",
        "default":     10,
        "min":         1,
        "max":         1440,
        "step":        1,
    },
]


def _load_pipeline_settings() -> dict:
    """Return current pipeline settings, merging stored values over schema defaults."""
    defaults = {s["key"]: s["default"] for s in _SETTINGS_SCHEMA}
    if not _PIPELINE_SETTINGS_FILE.exists():
        return defaults
    try:
        with open(_PIPELINE_SETTINGS_FILE, encoding="utf-8") as f:
            stored = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        return {**defaults, **{k: v for k, v in stored.items() if k in defaults}}
    except Exception:
        return defaults


def _save_pipeline_settings(values: dict) -> None:
    """Write pipeline settings to the JSON file, preserving the README key."""
    readme = "Runtime-tunable pipeline settings. Edit via the admin page or directly. Changes apply on the next pipeline run."
    _PIPELINE_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_PIPELINE_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"_README": readme, **values}, f, indent=2)


def _get_env_snapshot() -> list[dict]:
    """Return non-sensitive environment/config values for troubleshooting."""
    return [
        {"group": "LLM",          "key": "LLM_BASE_URL",        "value": os.getenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1")},
        {"group": "LLM",          "key": "LLM_MODEL",            "value": os.getenv("LLM_MODEL", "llama3.2")},
        {"group": "Extraction",   "key": "USE_DOCLING",          "value": os.getenv("USE_DOCLING", "true")},
        {"group": "Pipeline",     "key": "WATCH_MODE",           "value": os.getenv("WATCH_MODE", "false")},
        {"group": "Pipeline",     "key": "POLL_INTERVAL",        "value": os.getenv("POLL_INTERVAL", "30") + "s"},
        {"group": "Auth",         "key": "AUTH_MODE",            "value": AUTH_MODE},
        {"group": "Auth",         "key": "AUTH_SESSION_MINUTES", "value": os.getenv("AUTH_SESSION_MINUTES", "480")},
        {"group": "Supplier",     "key": "ALLOW_CREATE_SUPPLIER","value": str(ALLOW_CREATE_SUPPLIER).lower()},
        {"group": "Supplier",     "key": "SUPPLIER_CODE_PREFIX", "value": SUPPLIER_CODE_PREFIX},
        {"group": "Paths",        "key": "OUTPUT_DIR",           "value": os.getenv("OUTPUT_DIR", "/app/output")},
        {"group": "Paths",        "key": "INVOICES_DIR",         "value": os.getenv("INVOICES_DIR", "/app/invoices")},
        {"group": "Paths",        "key": "DATA_DIR",             "value": os.getenv("DATA_DIR", "/app/data")},
        {"group": "Paths",        "key": "EXPORT_DIR",           "value": os.getenv("EXPORT_DIR", "/app/output/export")},
        {"group": "Paths",        "key": "CONFIG_DIR",           "value": os.getenv("CONFIG_DIR", "/app/config")},
        {"group": "Export",       "key": "EXPORT_FORMAT",        "value": EXPORT_FORMAT},
        {"group": "Export",       "key": "XML_TEMPLATE",         "value": "config/export_template.xml.j2 (" + ("found" if _EXPORT_XML_TEMPLATE_FILE.exists() else "not found — using built-in default") + ")"},
        {"group": "Webhook Export", "key": "WEBHOOK_EXPORT_URL",      "value": os.getenv("WEBHOOK_EXPORT_URL", "not set")},
        {"group": "Webhook Export", "key": "WEBHOOK_EXPORT_TEMPLATE", "value": os.getenv("WEBHOOK_EXPORT_TEMPLATE", "webhook_export_template.json.j2")},
    ]


def _get_build_commit() -> str:
    """Determine the current build commit hash from multiple sources."""
    # 1. Explicit env var (set in CI/CD or docker-compose)
    if (v := os.getenv("BUILD_COMMIT", "").strip()):
        return v[:8]
    # 2. Git CLI
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=str(_PIPELINE_DIR),
        ).decode().strip()
    except Exception:
        pass
    # 3. Read .git/HEAD directly (works without git CLI)
    git_head = _PIPELINE_DIR / ".git" / "HEAD"
    if git_head.exists():
        content = git_head.read_text().strip()
        if content.startswith("ref:"):
            ref_path = _PIPELINE_DIR / ".git" / content[5:].strip()
            if ref_path.exists():
                return ref_path.read_text().strip()[:8]
        elif len(content) >= 7:
            return content[:8]
    return "unknown"


_BUILD_COMMIT = _get_build_commit()

# Validate at startup
if AUTH_MODE not in ("disabled", "admin_only", "full"):
    logger.warning("AUTH_MODE=%r is invalid; falling back to 'disabled'", AUTH_MODE)
    AUTH_MODE = "disabled"

if AUTH_MODE != "disabled" and not AUTH_SECRET_KEY:
    logger.warning(
        "AUTH_MODE=%r but AUTH_SECRET_KEY is not set — sessions will be insecure. "
        "Set AUTH_SECRET_KEY to a random secret string.",
        AUTH_MODE,
    )


# ---------------------------------------------------------------------------
# User store — loaded once at module import (fast enough for small files)
# ---------------------------------------------------------------------------
_USERS: dict[str, dict] = {}   # username → {username, password_hash, role}

def _load_users() -> dict[str, dict]:
    if not AUTH_USERS_FILE.exists():
        if AUTH_MODE != "disabled":
            logger.warning("Auth enabled but users file not found: %s", AUTH_USERS_FILE)
        return {}
    try:
        data = json.loads(AUTH_USERS_FILE.read_text(encoding="utf-8"))
        # Store usernames in lowercase for case-insensitive lookup
        return {u["username"].lower(): u for u in data.get("users", []) if "username" in u}
    except Exception as exc:
        logger.warning("Failed to load users file %s: %s", AUTH_USERS_FILE, exc)
        return {}

_USERS = _load_users()


def _write_users_file(users_list: list) -> None:
    """Persist users list to users.json and hot-reload _USERS."""
    global _USERS
    try:
        data = json.loads(AUTH_USERS_FILE.read_text(encoding="utf-8")) if AUTH_USERS_FILE.exists() else {}
    except Exception:
        data = {}
    data["users"] = users_list
    AUTH_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_USERS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _USERS = _load_users()


# ---------------------------------------------------------------------------
# Session helpers (itsdangerous URLSafeTimedSerializer)
# ---------------------------------------------------------------------------
def _get_serializer():
    from itsdangerous import URLSafeTimedSerializer
    secret = AUTH_SECRET_KEY or "insecure-default-key"
    return URLSafeTimedSerializer(secret, salt="parsely-session")


def _make_session_token(username: str, role: str) -> str:
    return _get_serializer().dumps({"username": username, "role": role})


def _verify_session_token(token: str) -> Optional[dict]:
    """Return {username, role} or None if invalid/expired."""
    try:
        return _get_serializer().loads(token, max_age=AUTH_SESSION_MINUTES * 60)
    except Exception:
        return None


def _check_password(plain: str, hashed: str) -> bool:
    try:
        import bcrypt
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def _is_valid_bcrypt_hash(h: str) -> bool:
    return isinstance(h, str) and h.startswith(("$2b$", "$2a$", "$2y$"))


def _setup_required() -> bool:
    """True when auth is enabled but no admin user has a real bcrypt password set."""
    if AUTH_MODE == "disabled":
        return False
    return not any(
        u.get("role") == "admin" and _is_valid_bcrypt_hash(u.get("password_hash", ""))
        for u in _USERS.values()
    )


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
_ALWAYS_PUBLIC = {"/login", "/logout", "/setup", "/api/health", "/api/auth/me"}

def _requires_admin(path: str) -> bool:
    return path == "/admin" or path.startswith("/api/admin/") or path == "/api/admin"

def _is_api_request(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return request.url.path.startswith("/api/") or "application/json" in accept


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # First-run: redirect everything to setup until an admin password is set
        if _setup_required() and path not in _ALWAYS_PUBLIC:
            return RedirectResponse("/setup", status_code=303)

        # Always-public paths
        if path in _ALWAYS_PUBLIC:
            return await call_next(request)

        # No auth needed
        if AUTH_MODE == "disabled":
            return await call_next(request)

        admin_required = _requires_admin(path)

        # admin_only: non-admin paths are public
        if AUTH_MODE == "admin_only" and not admin_required:
            return await call_next(request)

        # Validate session
        token = request.cookies.get(_AUTH_SESSION_COOKIE)
        user = _verify_session_token(token) if token else None

        if user is None:
            # Not authenticated
            if _is_api_request(request):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            next_path = quote(str(request.url.path), safe="")
            return RedirectResponse(f"/login?next={next_path}", status_code=303)

        if admin_required and user.get("role") != "admin":
            # Authenticated but wrong role
            if _is_api_request(request):
                return JSONResponse({"detail": "Forbidden"}, status_code=403)
            return RedirectResponse("/?error=forbidden", status_code=303)

        # Attach user info for downstream handlers
        request.state.user = user
        return await call_next(request)



# ---------------------------------------------------------------------------
# Corrections helpers
# ---------------------------------------------------------------------------

def apply_corrections(extracted: dict, corrections: dict) -> dict:
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
    inv = result.get("extracted_invoice") or {}
    result["extracted_invoice"] = inv
    sup = inv.get("supplier") or {}
    inv["supplier"] = sup

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

    # Custom fields — full replacement when present
    if "custom_fields" in corrections:
        inv["custom_fields"] = corrections["custom_fields"]

    # Supplier ID override — set when operator uses the lookup button to pick a different
    # supplier.  Overwrite matched_supplier.supplier_id (and supplier_name) so the export
    # JSON carries the correct supplier ID even if the pipeline match was wrong or absent.
    if "corrected_supplier_id" in corrections:
        ms = result.get("matched_supplier") or {}
        result["matched_supplier"] = ms
        ms["supplier_id"]   = corrections["corrected_supplier_id"]
        ms["supplier_name"] = (
            corrections.get("supplier_name")
            or ms.get("supplier_name")
            or sup.get("name")
        )
        ms["match_method"] = "operator_override"

    return result


def build_normalized_supplier(data: dict, corrections: dict) -> dict:
    """
    Build a normalized supplier object for easy downstream consumption.
    
    Returns a single supplier object that consolidates information from:
    - matched_supplier (if available)
    - extracted_invoice.supplier (fallback)
    - operator corrections (highest priority)
    
    The 'source' field indicates where the data originated:
    - "corrected" - operator manually set the supplier_id
    - "matched"   - automatically matched from supplier database
    - "extracted" - only extracted from the invoice (no match)
    
    The 'matched_on' field (when available) indicates which specific field/value
    was used to identify the supplier:
    - "abn_exact": matched by ABN number
    - "name_exact": matched by exact name match
    - "name_fuzzy": matched by fuzzy name similarity
    - "email_domain": matched by email domain
    """
    inv = data.get("extracted_invoice") or {}
    extracted_sup = inv.get("supplier") or {}
    matched = data.get("matched_supplier") or {}
    match_method = matched.get("match_method")
    
    # Build matched_on detail based on match method
    matched_on = None
    if match_method == "abn_exact":
        matched_on = {
            "field": "abn",
            "value": extracted_sup.get("abn") or extracted_sup.get("acn"),
        }
    elif match_method == "name_exact":
        matched_on = {
            "field": "name",
            "value": extracted_sup.get("name"),
        }
    elif match_method == "name_fuzzy":
        matched_on = {
            "field": "name",
            "value": extracted_sup.get("name"),
            "fuzzy_score": matched.get("confidence"),
        }
    elif match_method == "email_domain":
        # Extract domain from email
        email = extracted_sup.get("email") or ""
        domain = email.split("@")[1] if "@" in email else None
        matched_on = {
            "field": "email_domain",
            "value": domain,
        }
    
    # Determine source and build normalized object
    if corrections.get("corrected_supplier_id"):
        # Operator manually corrected the supplier
        return {
            "id": corrections.get("corrected_supplier_id") or matched.get("supplier_id"),
            "name": corrections.get("supplier_name") or matched.get("supplier_name") or extracted_sup.get("name"),
            "abn": corrections.get("supplier_abn") or matched.get("abn") or extracted_sup.get("abn"),
            "acn": extracted_sup.get("acn"),
            "email": corrections.get("supplier_email") or extracted_sup.get("email"),
            "phone": corrections.get("supplier_phone") or extracted_sup.get("phone"),
            "address": corrections.get("supplier_address") or extracted_sup.get("address"),
            "source": "corrected",
            "match_confidence": matched.get("confidence"),
            "match_method": "operator_override",
            "matched_on": None,  # Manual override, no automatic matching
        }
    elif matched.get("supplier_id"):
        # Successfully matched to supplier database
        return {
            "id": matched.get("supplier_id"),
            "name": matched.get("supplier_name") or extracted_sup.get("name"),
            "abn": matched.get("abn") or extracted_sup.get("abn"),
            "acn": extracted_sup.get("acn"),
            "email": extracted_sup.get("email"),
            "phone": extracted_sup.get("phone"),
            "address": extracted_sup.get("address"),
            "source": "matched",
            "match_confidence": matched.get("confidence"),
            "match_method": match_method,
            "matched_on": matched_on,
        }
    else:
        # Only extracted data available
        return {
            "id": None,
            "name": extracted_sup.get("name"),
            "abn": extracted_sup.get("abn"),
            "acn": extracted_sup.get("acn"),
            "email": extracted_sup.get("email"),
            "phone": extracted_sup.get("phone"),
            "address": extracted_sup.get("address"),
            "source": "extracted",
            "match_confidence": None,
            "match_method": None,
            "matched_on": None,
        }


def build_normalized_line_items(data: dict) -> list:
    """
    Build a normalized line_items array with embedded PO match info.
    
    Each line item includes:
    - Original extracted fields (description, quantity, unit_price, etc.)
    - po_match object with match status and PO details (if available)
    
    This eliminates the need for downstream apps to cross-reference
    matched_po.line_matches by invoice_line_index.
    """
    inv = data.get("extracted_invoice") or {}
    extracted_items = inv.get("line_items") or []
    matched_po = data.get("matched_po") or {}
    line_matches = matched_po.get("line_matches") or []
    
    # Build lookup map: invoice_line_index -> match info
    match_map = {m.get("invoice_line_index"): m for m in line_matches if m.get("invoice_line_index") is not None}
    
    normalized = []
    for idx, item in enumerate(extracted_items):
        match = match_map.get(idx)
        
        normalized_item = {
            "line_number": item.get("line_number") or (idx + 1),
            "description": item.get("description"),
            "sku": item.get("sku"),
            "quantity": item.get("quantity"),
            "unit": item.get("unit"),
            "unit_price": item.get("unit_price"),
            "line_total": item.get("line_total"),
            "tax_amount": item.get("tax_amount"),
            "po_match": None,
        }
        
        if match:
            normalized_item["po_match"] = {
                "matched": match.get("matched", False),
                "po_line_number": match.get("po_line_number"),
                "po_description": match.get("po_description"),
                "match_score": match.get("match_score", 0.0),
                "quantity_matches": match.get("quantity_matches"),
                "price_matches": match.get("price_matches"),
                "total_matches": match.get("total_matches"),
            }
        
        normalized.append(normalized_item)
    
    return normalized


def _get_actor(request: Request) -> str:
    """Return the username of the authenticated user, or 'anonymous'."""
    user = getattr(request.state, "user", None)
    if user:
        return user.get("username", "anonymous")
    return "anonymous"





# ---------------------------------------------------------------------------
# Config from environment (mirrors pipeline config so volumes line up)
# ---------------------------------------------------------------------------
OUTPUT_DIR    = Path(os.getenv("OUTPUT_DIR",   str(_PIPELINE_DIR / "output")))
INVOICES_DIR  = Path(os.getenv("INVOICES_DIR", str(_PIPELINE_DIR / "invoices")))
DATA_DIR      = Path(os.getenv("DATA_DIR",     str(_PIPELINE_DIR / "data")))
EXPORT_DIR    = Path(os.getenv("EXPORT_DIR",   str(OUTPUT_DIR / "export")))
DB_PATH       = Path(os.getenv("DB_PATH",      str(OUTPUT_DIR / "pipeline.db")))
CONFIG_DIR    = Path(os.getenv("CONFIG_DIR",   str(_PIPELINE_DIR / "config")))

# Export format: json | xml | both  (default: json)
EXPORT_FORMAT             = os.getenv("EXPORT_FORMAT", "json").lower()
_EXPORT_XML_TEMPLATE_FILE = CONFIG_DIR / "export_template.xml.j2"
DASHBOARD_DIR = Path(__file__).parent

# Data files
SUPPLIERS_CSV = DATA_DIR / "suppliers.csv"
PO_CSV        = DATA_DIR / "purchase_orders.csv"
PO_LINES_CSV  = DATA_DIR / "purchase_order_lines.csv"

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
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Invoice Pipeline Dashboard", docs_url=None, redoc_url=None)
app.add_middleware(AuthMiddleware)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/auth/me")
def auth_me(request: Request):
    """Return current session user, or null if not logged in / auth disabled."""
    _config = {
        "allow_create_supplier": ALLOW_CREATE_SUPPLIER,
        "supplier_code_prefix":  SUPPLIER_CODE_PREFIX,
    }
    if AUTH_MODE == "disabled":
        return {"auth_mode": "disabled", "user": None, "config": _config}
    token = request.cookies.get(_AUTH_SESSION_COOKIE)
    user = _verify_session_token(token) if token else None
    return {"auth_mode": AUTH_MODE, "user": user, "config": _config}


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
    extracted: dict = {}
    try:
        parsed = json.loads(rec.get("extracted_data") or "{}")
        if isinstance(parsed, dict):
            extracted = parsed
    except Exception:
        pass

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
    pdf_path = pdf_service.find_pdf(source_file, stem, INVOICES_DIR, EXPORT_DIR)
    if pdf_path is None:
        return []

    mtime = pdf_path.stat().st_mtime
    cached = pdf_service.get_cached_pages(stem, mtime)
    if cached:
        return cached

    pages = pdf_service.render_pages(pdf_path)
    pdf_service.cache_pages(stem, mtime, pages)
    return pages


@app.get("/api/invoices/{stem}/pdf")
def get_raw_pdf(stem: str):
    """Serve the raw PDF file for native browser viewing."""
    from fastapi.responses import FileResponse
    rec = get_db().get_invoice(stem)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Invoice not found: {stem}")

    pdf_path = pdf_service.find_pdf(rec.get("source_file", ""), stem, INVOICES_DIR, EXPORT_DIR)
    if pdf_path is None or not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=f"{stem}.pdf",
        content_disposition_type="inline"
    )


@app.post("/api/invoices/{stem}/export")
def export_invoice(stem: str, request: Request):
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
    pdf_path = pdf_service.find_pdf(rec.get("source_file", ""), stem, INVOICES_DIR, EXPORT_DIR)
    if pdf_path is None:
        raise HTTPException(404, "Source PDF not found — cannot export")

    # Build export payload: full extracted data with corrections applied
    extracted: dict = {}
    try:
        parsed = json.loads(rec.get("extracted_data") or "{}")
        if isinstance(parsed, dict):
            extracted = parsed
    except Exception:
        pass

    corrections = {}
    if rec.get("corrections"):
        try:
            corrections = json.loads(rec["corrections"])
        except Exception:
            pass

    # Apply corrections to get the final data
    final_data = apply_corrections(extracted, corrections)

    # Build normalized objects for easy downstream consumption
    normalized_supplier = build_normalized_supplier(final_data, corrections)
    normalized_line_items = build_normalized_line_items(final_data)

    export_payload = {
        "stem":                stem,
        "exported_at":         datetime.now(timezone.utc).isoformat(),
        "pdf_file":            f"{stem}.pdf",
        "corrections_applied": bool(corrections),
        "corrections":         corrections,
        "operator_notes":      rec.get("notes"),
        "supplier":            normalized_supplier,
        "line_items":          normalized_line_items,
        **final_data,
    }

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    export_pdf_path = EXPORT_DIR / f"{stem}.pdf"
    export_json_path = EXPORT_DIR / f"{stem}.json"

    # Write JSON and/or XML depending on EXPORT_FORMAT
    if EXPORT_FORMAT in ("json", "both"):
        with open(export_json_path, "w", encoding="utf-8") as f:
            json.dump(export_payload, f, indent=2, default=str)
    if EXPORT_FORMAT in ("xml", "both"):
        with open(EXPORT_DIR / f"{stem}.xml", "w", encoding="utf-8") as f:
            f.write(render_export_xml(export_payload, _EXPORT_XML_TEMPLATE_FILE))

    # Move PDF
    shutil.move(str(pdf_path), str(export_pdf_path))

    # Invalidate PDF render cache (path changed)
    pdf_service.invalidate_cache(stem)

    # Update DB
    db.update_status(stem, "exported")
    db.log_audit(stem, "exported", actor=_get_actor(request),
                 detail={"format": EXPORT_FORMAT})

    # Trigger external webhook export if configured
    cfg = Config()
    if cfg.webhook_export_enabled:
        webhook_export_service = WebhookExportService(cfg)
        webhook_export_res = webhook_export_service.send_webhook_export(stem, export_payload, export_pdf_path)
        if webhook_export_res.get("status") != "skipped":
            db.log_audit(stem, "webhook_export_triggered", actor=_get_actor(request),
                         detail=webhook_export_res)

    logger.info("Exported: %s → %s", stem, EXPORT_DIR)
    result = {"status": "exported", "export_pdf": str(export_pdf_path)}
    if EXPORT_FORMAT in ("json", "both"):
        result["export_json"] = str(export_json_path)
    return result


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
    pdf_service.invalidate_cache(stem)
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
    pdf_path = pdf_service.find_pdf(rec.get("source_file", ""), stem, INVOICES_DIR, EXPORT_DIR)
    if pdf_path and pdf_path.exists():
        # Safety: only delete from the invoices inbox, not from export/
        if EXPORT_DIR not in pdf_path.parents:
            pdf_path.unlink()
            logger.info("Deleted PDF: %s", pdf_path)

    # Remove DB record and invalidate cache
    db.delete_invoice(stem)
    pdf_service.invalidate_cache(stem)
    logger.info("Deleted invoice record: %s", stem)
    return {"stem": stem, "deleted": True}


@app.patch("/api/invoices/{stem}/status")
def update_status(stem: str, body: StatusUpdate, request: Request):
    """
    Set status back to needs_review or ready.
    (Cannot set to 'exported' — use the /export endpoint for that.)
    """
    allowed = {STATUS_NEEDS_REVIEW, STATUS_READY}
    if body.status not in allowed:
        raise HTTPException(400, f"Status must be one of: {allowed}")

    db = get_db()
    rec = db.get_invoice(stem)
    if not rec:
        raise HTTPException(404, f"Invoice not found: {stem}")
    old_status = rec["status"]
    db.update_status(stem, body.status)
    db.log_audit(stem, "status_changed", actor=_get_actor(request),
                 detail={"from": old_status, "to": body.status})
    return {"stem": stem, "status": body.status}


@app.patch("/api/invoices/{stem}/corrections")
def update_corrections(stem: str, body: CorrectionsUpdate, request: Request):
    """Save operator field corrections for an invoice."""
    db = get_db()
    if not db.update_corrections(stem, body.corrections):
        raise HTTPException(404, f"Invoice not found: {stem}")
    db.log_audit(stem, "corrections_saved", actor=_get_actor(request),
                 detail={"fields": sorted(body.corrections.keys())})
    return {"stem": stem, "corrections": body.corrections}


@app.post("/api/bulk-export")
def bulk_export(request: Request):
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
    
    # Initialize webhook export service once for bulk operation if enabled
    cfg = Config()
    webhook_export_service = WebhookExportService(cfg) if cfg.webhook_export_enabled else None

    for inv in ready_invoices:
        stem = inv["stem"]
        try:
            rec = db.get_invoice(stem)
            if not rec or rec["status"] != STATUS_READY:
                continue  # race condition: another request got there first

            pdf_path = pdf_service.find_pdf(rec.get("source_file", ""), stem, INVOICES_DIR, EXPORT_DIR)
            if pdf_path is None:
                errors.append(f"{stem}: PDF not found")
                continue

            extracted: dict = {}
            try:
                parsed = json.loads(rec.get("extracted_data") or "{}")
                if isinstance(parsed, dict):
                    extracted = parsed
            except Exception:
                pass

            corrections: dict = {}
            if rec.get("corrections"):
                try:
                    corrections = json.loads(rec["corrections"])
                except Exception:
                    pass

            final_data = apply_corrections(extracted, corrections)
            normalized_supplier = build_normalized_supplier(final_data, corrections)
            normalized_line_items = build_normalized_line_items(final_data)

            export_payload = {
                "stem":                stem,
                "exported_at":         now,
                "pdf_file":            f"{stem}.pdf",
                "corrections_applied": bool(corrections),
                "corrections":         corrections,
                "operator_notes":      rec.get("notes"),
                "supplier":            normalized_supplier,
                "line_items":          normalized_line_items,
                **final_data,
            }

            export_pdf_path = EXPORT_DIR / f"{stem}.pdf"

            if EXPORT_FORMAT in ("json", "both"):
                with open(EXPORT_DIR / f"{stem}.json", "w", encoding="utf-8") as f:
                    json.dump(export_payload, f, indent=2, default=str)
            if EXPORT_FORMAT in ("xml", "both"):
                with open(EXPORT_DIR / f"{stem}.xml", "w", encoding="utf-8") as f:
                    f.write(render_export_xml(export_payload, _EXPORT_XML_TEMPLATE_FILE))

            shutil.move(str(pdf_path), str(export_pdf_path))
            pdf_service.invalidate_cache(stem)
            db.update_status(stem, "exported")
            db.log_audit(stem, "exported", actor=_get_actor(request),
                         detail={"format": EXPORT_FORMAT, "bulk": True})
            
            # Trigger external webhook export if enabled
            if webhook_export_service:
                webhook_export_res = webhook_export_service.send_webhook_export(stem, export_payload, export_pdf_path)
                if webhook_export_res.get("status") != "skipped":
                    db.log_audit(stem, "webhook_export_triggered", actor=_get_actor(request),
                                 detail=webhook_export_res)
                             
            exported += 1

        except Exception as e:
            errors.append(f"{stem}: {e}")
            logger.error("Bulk export failed for %s: %s", stem, e)

    logger.info("Bulk export complete: %d exported, %d errors", exported, len(errors))
    return {"exported": exported, "errors": errors}


@app.patch("/api/invoices/{stem}/notes")
def update_notes(stem: str, body: NotesUpdate, request: Request):
    """Save operator free-text notes."""
    db = get_db()
    if not db.update_notes(stem, body.notes):
        raise HTTPException(404, f"Invoice not found: {stem}")
    db.log_audit(stem, "notes_updated", actor=_get_actor(request))
    return {"stem": stem, "notes": body.notes}


@app.get("/api/invoices/{stem}/audit")
def get_invoice_audit(stem: str):
    """Return the full audit trail for a single invoice, oldest first."""
    db = get_db()
    if not db.get_invoice(stem):
        raise HTTPException(404, f"Invoice not found: {stem}")
    entries = db.get_audit_log(stem)
    for e in entries:
        if e.get("detail"):
            try:
                e["detail"] = json.loads(e["detail"])
            except Exception:
                pass
    return entries


@app.get("/api/admin/audit")
def get_audit_log(limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)):
    """Return recent audit entries across all invoices, newest first."""
    db = get_db()
    entries = db.get_recent_audit_log(limit=limit, offset=offset)
    for e in entries:
        if e.get("detail"):
            try:
                e["detail"] = json.loads(e["detail"])
            except Exception:
                pass
    return entries


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


# ── Admin API ────────────────────────────────────────────────────────────────
# NOTE: Specific routes (/status, /reload, /database/clear) must be defined
# BEFORE generic routes (/{tab}) or FastAPI will match them incorrectly.

def _get_webhook_export_templates() -> list[str]:
    """List all .j2 template files in the config directory."""
    return sorted([
        f.name for f in CONFIG_DIR.glob("*.j2") 
        if f.is_file() and f.name != "export_template.xml.j2"
    ])


@app.get("/api/admin/status")
def get_admin_status():
    """Get status of CSV files - modification times, row counts, etc."""
    return {
        "suppliers": csv_manager.get_metadata(SUPPLIERS_CSV),
        "purchase_orders": csv_manager.get_metadata(PO_CSV),
        "purchase_order_lines": csv_manager.get_metadata(PO_LINES_CSV),
    }


@app.post("/api/admin/reload")
def reload_csv_files():
    """Acknowledge a CSV reload request and return fresh file metadata.

    The pipeline process detects CSV changes automatically via mtime and
    reloads its own matchers at the start of the next poll cycle — no
    explicit signal is needed.  This endpoint just clears the dashboard's
    metadata cache so the UI shows up-to-date file info immediately.
    """
    csv_manager.clear_cache()
    logger.info("CSV metadata cache cleared (pipeline will reload on next poll cycle)")
    return {
        "status": "ok",
        "files": {
            "suppliers": csv_manager.get_metadata(SUPPLIERS_CSV),
            "purchase_orders": csv_manager.get_metadata(PO_CSV),
            "purchase_order_lines": csv_manager.get_metadata(PO_LINES_CSV),
        }
    }


@app.post("/api/admin/database/clear")
def clear_database():
    global _db
    if DB_PATH.exists():
        try:
            # Try to remove WAL/SHM files too
            for suffix in ["", "-wal", "-shm"]:
                p = DB_PATH.with_suffix(DB_PATH.suffix + suffix)
                if p.exists():
                    p.unlink()
            _db = None
            return {"status": "ok"}
        except Exception as e:
            raise HTTPException(500, f"Failed to clear database: {e}")
    else:
        _db = None
        return {"status": "ok", "message": "Database file did not exist"}


# ── User management API ───────────────────────────────────────────────────────

_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


@app.get("/api/admin/users")
def list_users():
    return {
        "auth_mode": AUTH_MODE,
        "users": [
            {"username": u["username"], "role": u["role"]}
            for u in _USERS.values()
        ],
    }


@app.post("/api/admin/users", status_code=201)
def create_user(body: UserCreate):
    username = body.username.lower()
    if not _USERNAME_RE.match(username):
        raise HTTPException(400, "Username must be 1–64 characters: letters, digits, _ or -")
    if username in _USERS:
        raise HTTPException(409, f"User '{username}' already exists")
    if body.role not in ("admin", "user"):
        raise HTTPException(400, "Role must be 'admin' or 'user'")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    import bcrypt
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    users_list = [
        {"username": u["username"], "password_hash": u["password_hash"], "role": u["role"]}
        for u in _USERS.values()
    ]
    users_list.append({"username": username, "password_hash": hashed, "role": body.role})
    _write_users_file(users_list)
    logger.info("User created: %s (role=%s)", username, body.role)
    return {"username": username, "role": body.role}


@app.patch("/api/admin/users/{username}")
def update_user(username: str, body: UserUpdate):
    username = username.lower()
    if username not in _USERS:
        raise HTTPException(404, f"User '{username}' not found")
    if body.role is not None and body.role not in ("admin", "user"):
        raise HTTPException(400, "Role must be 'admin' or 'user'")
    if body.password is not None and len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    # Protect: cannot demote the last admin
    current_role = _USERS[username]["role"]
    if body.role == "user" and current_role == "admin":
        admin_count = sum(
            1 for u in _USERS.values()
            if u["role"] == "admin" and _is_valid_bcrypt_hash(u.get("password_hash", ""))
        )
        if admin_count <= 1:
            raise HTTPException(400, "Cannot change role: this is the only admin account")

    import bcrypt
    users_list = []
    for u in _USERS.values():
        entry = {"username": u["username"], "password_hash": u["password_hash"], "role": u["role"]}
        if u["username"] == username:
            if body.role is not None:
                entry["role"] = body.role
            if body.password is not None:
                entry["password_hash"] = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
        users_list.append(entry)

    _write_users_file(users_list)
    logger.info("User updated: %s", username)
    updated = _USERS[username]
    return {"username": updated["username"], "role": updated["role"]}


@app.delete("/api/admin/users/{username}")
def delete_user(username: str):
    username = username.lower()
    if username not in _USERS:
        raise HTTPException(404, f"User '{username}' not found")

    # Protect: cannot delete the last admin
    if _USERS[username]["role"] == "admin":
        admin_count = sum(
            1 for u in _USERS.values()
            if u["role"] == "admin" and _is_valid_bcrypt_hash(u.get("password_hash", ""))
        )
        if admin_count <= 1:
            raise HTTPException(400, "Cannot delete the last admin account")

    users_list = [
        {"username": u["username"], "password_hash": u["password_hash"], "role": u["role"]}
        for u in _USERS.values()
        if u["username"] != username
    ]
    _write_users_file(users_list)
    logger.info("User deleted: %s", username)
    return {"deleted": True}


# ── Pipeline settings (admin-editable) ────────────────────────────────────────

@app.post("/api/admin/webhook-export/test")
async def test_webhook_export(request: Request):
    """
    Test the webhook export with transient configuration and dummy data.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    # Create a temporary config object from the request body
    from types import SimpleNamespace
    mock_cfg = SimpleNamespace(
        webhook_export_url=body.get("webhook_export_url"),
        webhook_export_method=body.get("webhook_export_method", "POST"),
        webhook_export_headers_json=body.get("webhook_export_headers_json"),
        webhook_export_template=body.get("webhook_export_template"),
        webhook_export_enable_pdf=body.get("webhook_export_enable_pdf", False),
    )

    # Use a dummy payload for testing
    dummy_payload = {
        "stem": "test-invoice",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "corrections_applied": False,
        "operator_notes": "This is a test webhook message from the Parsely Admin Dashboard.",
        "supplier": {
            "id": "SUP-TEST",
            "name": "Test Supplier Corp",
            "abn": "00 000 000 000",
            "email": "test@example.com",
            "source": "test"
        },
        "line_items": [
            {
                "line_number": 1,
                "description": "Consulting Services (Test)",
                "quantity": 1,
                "unit_price": 500.0,
                "line_total": 500.0,
                "po_match": None
            }
        ],
        "extracted_invoice": {
            "invoice_number": "INV-TEST-001",
            "invoice_date": datetime.now(timezone.utc).date().isoformat(),
            "total": 550.0,
            "currency": "USD",
            "tax_amount": 50.0,
            "subtotal": 500.0
        }
    }

    # Initialize service and send
    service = WebhookExportService(mock_cfg)
    # We don't provide a PDF path for the test
    res = service.send_webhook_export("test-invoice", dummy_payload)
    
    return res


@app.post("/api/admin/email-ingest/test")
async def test_email_ingest(request: Request):
    """
    Test the email ingestion with transient configuration.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    # Handle masked password: if UI sends ********, use the actual stored password
    imap_password = body.get("email_imap_password")
    if imap_password == "********":
        current_settings = _load_pipeline_settings()
        imap_password = current_settings.get("email_imap_password", "")

    from types import SimpleNamespace
    mock_cfg = SimpleNamespace(
        email_ingest_enabled=True,
        email_imap_host=body.get("email_imap_host"),
        email_imap_port=int(body.get("email_imap_port", 993)),
        email_imap_user=body.get("email_imap_user"),
        email_imap_password=imap_password,
        email_use_ssl=body.get("email_use_ssl", True),
        email_mailbox=body.get("email_mailbox", "INBOX"),
        email_search_criteria=body.get("email_search_criteria", "UNSEEN"),
        email_processed_mailbox=body.get("email_processed_mailbox"),
    )

    service = EmailIngestService(mock_cfg)
    try:
        count = service.poll_mailbox()
        return {"status": "success", "pdfs_found": count}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/admin/settings")
def get_admin_settings():
    """Return editable pipeline settings with schema, plus read-only env snapshot."""
    current = _load_pipeline_settings()
    
    # Mask sensitive values before sending to the UI
    schema_with_values = []
    for s in _SETTINGS_SCHEMA:
        val = current.get(s["key"], s["default"])
        if s.get("ui_type") == "password" and val:
            val = "********"
        schema_with_values.append({**s, "value": val})

    return {
        "build":    {"commit": _BUILD_COMMIT, "python": sys.version.split()[0]},
        "settings": schema_with_values,
        "env":      _get_env_snapshot(),
        "webhook_export_templates": _get_webhook_export_templates(),
    }


@app.post("/api/admin/settings")
async def save_admin_settings(request: Request):
    """Validate and persist pipeline settings to pipeline_settings.json."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected JSON object")

    _type_fns = {"float": float, "int": int, "str": str, "bool": bool}
    current = _load_pipeline_settings()

    for field_schema in _SETTINGS_SCHEMA:
        key = field_schema["key"]
        if key not in body:
            continue
        
        raw = body[key]
        
        # Skip updating if it's a masked password (preserve existing secret)
        if field_schema.get("ui_type") == "password" and raw == "********":
            continue

        try:
            val = _type_fns[field_schema["type"]](raw)
        except (TypeError, ValueError, KeyError):
            raise HTTPException(400, f"Invalid value for '{key}': expected {field_schema['type']}")
        
        # Range check only applies to numbers
        if field_schema["type"] in ("int", "float"):
            lo, hi = field_schema.get("min"), field_schema.get("max")
            if lo is not None and val < lo:
                raise HTTPException(400, f"'{key}' must be at least {lo}")
            if hi is not None and val > hi:
                raise HTTPException(400, f"'{key}' must be at most {hi}")
        
        current[key] = val

    _save_pipeline_settings(current)
    logger.info("Pipeline settings saved: %s", current)
    return {"saved": True}


# ── Supplier quick-create ─────────────────────────────────────────────────────

@app.post("/api/suppliers/create", status_code=201)
def create_supplier(body: SupplierCreate):
    """Create a new supplier record in the suppliers CSV with an auto-generated ID."""
    if not ALLOW_CREATE_SUPPLIER:
        raise HTTPException(403, "Supplier creation is disabled (ALLOW_CREATE_SUPPLIER=false)")

    fieldnames = ["id", "name", "abn", "acn", "email", "phone", "address", "aliases"]
    rows: list[dict] = []
    if SUPPLIERS_CSV.exists():
        with open(SUPPLIERS_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    # Find next numeric ID with the configured prefix
    prefix = SUPPLIER_CODE_PREFIX
    max_num = 0
    for row in rows:
        rid = row.get("id", "")
        if rid.startswith(prefix):
            suffix = rid[len(prefix):]
            try:
                max_num = max(max_num, int(suffix))
            except ValueError:
                pass
    new_id = f"{prefix}{max_num + 1}"

    # Duplicate detection (non-blocking — returned as a warning)
    duplicate_warning: Optional[str] = None
    name_lower = body.name.strip().lower()
    abn_clean  = body.abn.replace(" ", "").strip()
    for row in rows:
        if abn_clean and row.get("abn", "").replace(" ", "").strip() == abn_clean:
            duplicate_warning = f"A supplier with this ABN already exists: {row.get('name', '')} ({row.get('id', '')})"
            break
        if row.get("name", "").strip().lower() == name_lower:
            duplicate_warning = f"A supplier with this name already exists ({row.get('id', '')})"
            break

    new_row = {
        "id":      new_id,
        "name":    body.name.strip(),
        "abn":     body.abn.strip(),
        "acn":     body.acn.strip(),
        "email":   body.email.strip(),
        "phone":   body.phone.strip(),
        "address": body.address.strip(),
        "aliases": "",
    }
    rows.append(new_row)

    with open(SUPPLIERS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Supplier created: %s (%s)", new_id, body.name)
    return {"id": new_id, "duplicate_warning": duplicate_warning}


# Generic tab routes (defined AFTER specific routes above)

@app.get("/api/admin/{tab}")
def get_admin_data(tab: str):
    path = {
        "suppliers": SUPPLIERS_CSV,
        "purchase_orders": PO_CSV,
        "purchase_order_lines": PO_LINES_CSV
    }.get(tab)

    if not path:
        raise HTTPException(404, "Invalid tab")

    headers = {
        "suppliers": ["id", "name", "abn", "acn", "email", "phone", "address", "aliases"],
        "purchase_orders": [
            "po_number", "supplier_id", "supplier_name", "issue_date",
            "expected_delivery", "subtotal", "tax_amount", "total",
            "currency", "status", "notes"
        ],
        "purchase_order_lines": [
            "po_number", "line_number", "sku", "description",
            "quantity", "unit", "unit_price", "total"
        ]
    }[tab]

    rows = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

    return {"headers": headers, "rows": rows}


@app.post("/api/admin/{tab}")
def update_admin_data(tab: str, body: AdminDataUpdate):
    path = {
        "suppliers": SUPPLIERS_CSV,
        "purchase_orders": PO_CSV,
        "purchase_order_lines": PO_LINES_CSV
    }.get(tab)

    if not path:
        raise HTTPException(404, "Invalid tab")

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=body.headers)
        writer.writeheader()
        writer.writerows(body.rows)

    return {"status": "ok"}


@app.post("/api/admin/{tab}/upload")
async def upload_admin_csv(tab: str, file: UploadFile = File(...)):
    """Upload and overwrite a CSV data file."""
    path = {
        "suppliers": SUPPLIERS_CSV,
        "purchase_orders": PO_CSV,
        "purchase_order_lines": PO_LINES_CSV
    }.get(tab)

    if not path:
        raise HTTPException(404, "Invalid tab")

    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV files are accepted")

    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(400, "Uploaded file is empty")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    
    logger.info("Admin CSV uploaded and overwritten: %s", path)
    return {"status": "ok", "filename": file.filename}


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
def login_page():
    html_path = DASHBOARD_DIR / "templates" / "login.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="Login template not found")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/login")
async def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
):
    username = username.lower()
    user = _USERS.get(username)
    if user and _check_password(password, user.get("password_hash", "")):
        token = _make_session_token(username, user["role"])
        # Sanitise redirect target — only allow relative paths
        redirect_to = next if (next.startswith("/") and not next.startswith("//")) else "/"
        response = RedirectResponse(redirect_to, status_code=303)
        response.set_cookie(
            key=_AUTH_SESSION_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            max_age=AUTH_SESSION_MINUTES * 60,
        )
        return response
    return RedirectResponse(f"/login?error=1&next={quote(next, safe='')}", status_code=303)


@app.post("/logout")
def logout():
    # In full mode the dashboard requires login, so send to /login.
    # In admin_only mode the dashboard is public — send straight there.
    dest = "/login" if AUTH_MODE == "full" else "/"
    response = RedirectResponse(dest, status_code=303)
    response.delete_cookie(_AUTH_SESSION_COOKIE)
    return response


@app.get("/setup")
def setup_page():
    if not _setup_required():
        return RedirectResponse("/", status_code=303)
    html_path = DASHBOARD_DIR / "templates" / "setup.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="Setup template not found")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/setup")
async def setup_submit(
    username: str = Form(default="admin"),
    password: str = Form(...),
    confirm: str = Form(...),
):
    global _USERS

    if not _setup_required():
        return RedirectResponse("/setup?error=unavailable", status_code=303)
    if len(password) < 8:
        return RedirectResponse("/setup?error=tooshort", status_code=303)
    if password != confirm:
        return RedirectResponse("/setup?error=mismatch", status_code=303)

    username = username.lower()
    import bcrypt
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    # Load existing users list (preserves entries other than the admin placeholder)
    try:
        existing = json.loads(AUTH_USERS_FILE.read_text(encoding="utf-8")) if AUTH_USERS_FILE.exists() else {}
    except Exception:
        existing = {}

    users: list = existing.get("users", [])

    # Update the first admin entry found, or prepend a new one
    for u in users:
        if u.get("role") == "admin" or u.get("username") == username:
            u["username"] = username
            u["password_hash"] = hashed
            u["role"] = "admin"
            break
    else:
        users.insert(0, {"username": username, "password_hash": hashed, "role": "admin"})

    _write_users_file(users)
    logger.info("Initial admin account created: %s", username)

    token = _make_session_token(username, "admin")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        key=_AUTH_SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=AUTH_SESSION_MINUTES * 60,
    )
    return response


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


@app.get("/admin")
def admin():
    html_path = DASHBOARD_DIR / "templates" / "admin.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="Admin template not found")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma":        "no-cache",
            "Expires":       "0",
        },
    )


@app.get("/help")
def help_page():
    html_path = DASHBOARD_DIR / "templates" / "help.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="Help template not found")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma":        "no-cache",
            "Expires":       "0",
        },
    )
