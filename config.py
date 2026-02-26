"""
Central configuration for the invoice processing pipeline.

All paths, thresholds, and model settings are defined here.
Override via environment variables or by passing a Config instance directly.

Settings priority (highest wins):
  1. Environment variables
  2. config/pipeline_settings.json  (admin-editable, persisted)
  3. Hardcoded defaults in this file
"""
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Project root (directory containing this file)
PROJECT_ROOT = Path(__file__).parent

# Default data locations (relative to project root)
DEFAULT_SUPPLIERS_CSV  = PROJECT_ROOT / "data" / "suppliers.csv"
DEFAULT_PO_CSV         = PROJECT_ROOT / "data" / "purchase_orders.csv"
DEFAULT_PO_LINES_CSV   = PROJECT_ROOT / "data" / "purchase_order_lines.csv"
DEFAULT_OUTPUT_DIR     = PROJECT_ROOT / "output"
DEFAULT_INVOICES_DIR   = PROJECT_ROOT / "invoices"
DEFAULT_DB_PATH        = DEFAULT_OUTPUT_DIR / "pipeline.db"
DEFAULT_EXPORT_DIR     = DEFAULT_OUTPUT_DIR / "export"


@dataclass
class Config:
    # --- Extraction ---
    use_docling: bool = field(
        default_factory=lambda: os.getenv("USE_DOCLING", "true").lower() != "false"
    )
    # use_docling=True  → DoclingExtractor (accuracy-first, default)
    # use_docling=False → PlainTextExtractor / pdfplumber (speed-first, fallback)

    # --- LLM settings (OpenAI-compatible API) ---
    # Works with Ollama, OpenAI, Groq, Azure OpenAI, or any OpenAI-compatible backend.
    #
    # Ollama (default):   LLM_BASE_URL=http://host.docker.internal:11434/v1  LLM_API_KEY=ollama
    # OpenAI:             LLM_BASE_URL=https://api.openai.com/v1              LLM_API_KEY=sk-...
    # Groq:               LLM_BASE_URL=https://api.groq.com/openai/v1         LLM_API_KEY=gsk_...
    llm_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", os.getenv("OLLAMA_MODEL", "llama3.2"))
    )
    llm_base_url: str = field(
        default_factory=lambda: os.getenv(
            "LLM_BASE_URL",
            # Fall back to old OLLAMA_HOST if set, appending /v1 for the OpenAI-compat path
            (os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/v1"),
        )
    )
    llm_api_key: str = field(
        default_factory=lambda: os.getenv("LLM_API_KEY", "ollama")
    )

    # --- Data source paths ---
    suppliers_csv:  Path = field(default_factory=lambda: DEFAULT_SUPPLIERS_CSV)
    po_csv:         Path = field(default_factory=lambda: DEFAULT_PO_CSV)
    po_lines_csv:   Path = field(default_factory=lambda: DEFAULT_PO_LINES_CSV)

    # --- Output settings ---
    output_dir:   Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)
    db_path:      Path = field(
        default_factory=lambda: Path(os.getenv("DB_PATH", str(DEFAULT_DB_PATH)))
    )
    export_dir:   Path = field(
        default_factory=lambda: Path(os.getenv("EXPORT_DIR", str(DEFAULT_EXPORT_DIR)))
    )
    pretty_json:  bool = True       # Indent JSON output for human readability

    # --- Watch / polling mode ---
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL", "30"))
    )
    # How long (seconds) to wait between directory scans in watch mode.
    # State is persisted to output/.pipeline_state.json so restarts are safe.

    # --- Validation thresholds ---
    max_invoice_age_days:   int   = 90    # Warn if invoice is older than this
    max_future_days:        int   = 7     # Error if invoice date is this far ahead
    arithmetic_tolerance:   float = 0.05  # $0.05 rounding tolerance for totals

    # --- Supplier matching ---
    supplier_fuzzy_threshold: int = 75    # Minimum rapidfuzz score (0-100)

    # --- PO line matching ---
    po_line_fuzzy_threshold: int = 65     # Minimum rapidfuzz score for description

    # --- Webhook Export / Webhook settings ---
    webhook_export_enabled: bool = field(
        default_factory=lambda: os.getenv("WEBHOOK_EXPORT_ENABLED", "true").lower() != "false"
    )
    webhook_export_url: Optional[str] = field(
        default_factory=lambda: os.getenv("WEBHOOK_EXPORT_URL")
    )
    webhook_export_method: str = field(
        default_factory=lambda: os.getenv("WEBHOOK_EXPORT_METHOD", "POST")
    )
    webhook_export_headers_json: Optional[str] = field(
        default_factory=lambda: os.getenv("WEBHOOK_EXPORT_HEADERS")
    )
    webhook_export_template: Optional[str] = field(
        default_factory=lambda: os.getenv("WEBHOOK_EXPORT_TEMPLATE", "webhook_export_template.json.j2")
    )
    webhook_export_enable_pdf: bool = field(
        default_factory=lambda: os.getenv("WEBHOOK_EXPORT_ENABLE_PDF", "false").lower() == "true"
    )

    # --- Backup settings ---
    backup_enabled: bool = field(
        default_factory=lambda: os.getenv("BACKUP_ENABLED", "true").lower() != "false"
    )
    backup_interval_hours: int = field(
        default_factory=lambda: int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
    )
    backup_retention_count: int = field(
        default_factory=lambda: int(os.getenv("BACKUP_RETENTION_COUNT", "7"))
    )

    def __post_init__(self) -> None:
        """Overlay runtime-tunable settings from pipeline_settings.json if present."""
        config_dir = Path(os.getenv("CONFIG_DIR", str(PROJECT_ROOT / "config")))
        settings_file = config_dir / "pipeline_settings.json"
        if not settings_file.exists():
            return
        _type_map: dict[str, type] = {
            "arithmetic_tolerance":    float,
            "max_invoice_age_days":    int,
            "max_future_days":         int,
            "supplier_fuzzy_threshold":  int,
            "po_line_fuzzy_threshold":   int,
            "webhook_export_enabled":        bool,
            "webhook_export_url":            str,
            "webhook_export_method":         str,
            "webhook_export_headers_json":   str,
            "webhook_export_template":       str,
            "webhook_export_enable_pdf":     bool,
            "backup_enabled":                bool,
            "backup_interval_hours":         int,
            "backup_retention_count":        int,
        }
        try:
            with open(settings_file, encoding="utf-8") as f:
                overrides = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
            for key, val in overrides.items():
                if key in _type_map and hasattr(self, key):
                    setattr(self, key, _type_map[key](val))
        except Exception as exc:
            logger.warning("Failed to load pipeline_settings.json: %s", exc)

    def ensure_output_dir(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
