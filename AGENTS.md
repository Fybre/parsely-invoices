# Parsely Invoice Processing Pipeline — Agent Guide

## Project Overview

Parsely is an AI-powered invoice processing pipeline that extracts structured data from PDF invoices, matches them against Purchase Orders and supplier master data, flags discrepancies, and surfaces everything through a web dashboard.

**Key Capabilities:**
- Layout-aware PDF extraction (Docling + pdfplumber)
- OCR support for scanned PDFs
- Structured LLM extraction (OpenAI-compatible API)
- Supplier matching (ABN → exact name → fuzzy → email domain)
- PO matching with line-item comparison
- Discrepancy detection (arithmetic, dates, PO mismatches)
- SQLite-backed state with web dashboard for review/approval

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Pipeline Flow                            │
├─────────────────────────────────────────────────────────────────┤
│  PDF → DoclingExtractor → Markdown + Tables                     │
│          ↓                                                       │
│  TableLineItemExtractor (direct table parsing)                  │
│          ↓                                                       │
│  LLMParser (metadata extraction) → ExtractedInvoice (Pydantic)  │
│          ↓                                                       │
│  SupplierMatcher → POMatcher → InvoiceValidator                 │
│          ↓                                                       │
│  SQLite Database (output/pipeline.db)                           │
│          ↓                                                       │
│  Dashboard (FastAPI) → Review → Export to output/export/        │
└─────────────────────────────────────────────────────────────────┘
```

### Key Components

| Component | File | Purpose |
|-----------|------|---------|
| **CLI Entry** | `main.py` | Click-based CLI: `check`, `process`, `watch` |
| **Config** | `config.py` | Central `Config` dataclass, env var overrides |
| **Processor** | `pipeline/processor.py` | Main `InvoiceProcessor` orchestrator |
| **Extractor** | `pipeline/extractor.py` | Docling & pdfplumber extraction |
| **LLM Parser** | `pipeline/llm_parser.py` | OpenAI-compatible API client |
| **Matcher** | `pipeline/supplier_matcher.py` | Fuzzy supplier identification |
| **PO Matcher** | `pipeline/po_matcher.py` | PO matching & line comparison |
| **Validator** | `pipeline/validator.py` | Discrepancy detection |
| **Database** | `pipeline/database.py` | SQLite persistence layer |
| **Dashboard** | `dashboard/app.py` | FastAPI backend + API |
| **Models** | `models/*.py` | Pydantic models for invoices, POs, suppliers |

---

## Development Workflow

### Docker is the Primary Runtime

All development and production use Docker Compose. Do NOT install dependencies directly on the host.

```bash
# Build the image
docker compose build

# Verify setup
docker compose run --rm pipeline check

# Process invoices (one-shot)
docker compose run --rm pipeline process /app/invoices

# Watch mode (continuous)
docker compose up -d

# Start dashboard
docker compose up -d dashboard
```

### Code Changes

When modifying Python files:
- No rebuild needed for `pipeline/` changes (mounted volume)
- No rebuild needed for `dashboard/` changes (mounted volume)
- Rebuild required for `requirements.txt` changes

### Project Structure

```
parsely-invoices/
├── main.py                 # CLI entry point
├── config.py               # Configuration dataclass
├── docker-compose.yml      # Service definitions
├── Dockerfile              # Multi-stage build
│
├── pipeline/               # Core processing modules
│   ├── processor.py        # Orchestrator
│   ├── extractor.py        # PDF extraction
│   ├── llm_parser.py       # LLM structured extraction
│   ├── supplier_matcher.py
│   ├── po_matcher.py
│   ├── validator.py
│   ├── database.py
│   └── custom_field_extractor.py
│
├── models/                 # Pydantic data models
│   ├── invoice.py
│   ├── purchase_order.py
│   ├── supplier.py
│   └── result.py
│
├── dashboard/              # Web UI
│   ├── app.py              # FastAPI backend
│   └── templates/index.html
│
├── config/                 # Operator-editable config (mounted)
│   ├── custom_fields.json  # Site-specific extraction fields
│   └── column_keys.json    # Table header synonyms
│
├── data/                   # Reference CSVs (gitignored)
│   ├── suppliers.csv
│   ├── purchase_orders.csv
│   └── purchase_order_lines.csv
│
├── invoices/               # Input PDFs (gitignored)
└── output/                 # Database + exports (gitignored)
    ├── pipeline.db
    └── export/
```

---

## Code Conventions

### Python Style

- **Python 3.11+** with type hints throughout
- **Ruff** for linting/formatting (follow existing patterns)
- Docstrings for all public classes and methods
- Use `pathlib.Path` for all file operations
- Prefer dataclasses or Pydantic models over plain dicts

### Import Style

```python
# Standard library first
import json
import logging
from pathlib import Path
from typing import Optional

# Third-party
from pydantic import BaseModel, Field

# Local project imports
from config import Config
from models.result import InvoiceProcessingResult
```

### Error Handling

```python
# Use try/except with specific exceptions
try:
    result = self.process(pdf)
except Exception as e:
    logger.error("Failed to process %s: %s", pdf.name, e, exc_info=True)
    self.db.record_failure(pdf.stem, str(pdf), pdf.stat().st_mtime, str(e))
```

### Logging

```python
logger = logging.getLogger(__name__)

# Info for high-level flow
logger.info("Processing: %s", pdf_path.name)

# Debug for details
logger.debug("Extracted %d line items", len(items))

# Error with exc_info for stack traces
logger.error("Processing failed: %s", e, exc_info=True)
```

---

## Configuration Patterns

### Environment Variables → Config

All settings flow through `config.py`:

```python
@dataclass
class Config:
    llm_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "llama3.2")
    )
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL", "30"))
    )
```

### Custom Fields

Site-specific fields are defined in `config/custom_fields.json`:

```json
{
  "section_title": "Additional Fields",
  "fields": [
    {
      "name": "strata_reference",
      "label": "Strata Reference",
      "regex": "(SP\\s*\\d+|\\d{4,6})",
      "table_keys": ["strata ref", "strata plan"],
      "llm_hint": "Strata plan reference number"
    }
  ]
}
```

Extraction priority: **regex > table > llm**

---

## Database Schema (SQLite)

The `Database` class in `pipeline/database.py` manages a single SQLite file:

**Table: `invoices`**
- `stem` (PK): filename without extension
- `source_file`: original PDF path
- `source_mtime`: file modification time
- `extracted_data`: JSON blob of full extraction result
- `status`: `needs_review` | `ready` | `exported`
- `corrections`: JSON blob of operator corrections
- `notes`: operator free-text notes
- `error_message`: set if processing failed
- `created_at`, `updated_at`, `exported_at`

---

## Testing Approach

Currently **no automated test suite** exists. Testing is manual:

```bash
# 1. Verify your setup
docker compose run --rm pipeline check

# 2. Process a single invoice
docker compose run --rm pipeline process /app/invoices/sample.pdf

# 3. Check database output
docker compose run --rm pipeline python -c "
from pipeline.database import Database
from config import Config
db = Database(Config().db_path)
print(db.get_stats())
"

# 4. Test dashboard at http://localhost:8080
```

When adding features, verify:
1. Single PDF processing works
2. Batch processing works
3. Watch mode picks up new files
4. Dashboard displays results correctly
5. Export writes files to `output/export/`

---

## Common Tasks

### Adding a New Discrepancy Check

1. Edit `pipeline/validator.py`
2. Add check in `validate()` method
3. Return `Discrepancy` with `severity` and `description`

### Adding a Custom Field Extraction Strategy

1. Edit `pipeline/custom_field_extractor.py`
2. Add method for new strategy type
3. Update `merge()` to include new priority level

### Modifying the Dashboard API

1. Edit `dashboard/app.py`
2. Add Pydantic model for request/response if needed
3. Follow existing pattern of DB operations via `get_db()`

### Adding a New CLI Command

1. Edit `main.py`
2. Add `@cli.command()` decorated function
3. Pass context with `@click.pass_context`

---

## LLM Integration Notes

The pipeline uses OpenAI-compatible APIs via `openai` library:

```python
from openai import OpenAI

client = OpenAI(
    base_url=config.llm_base_url,
    api_key=config.llm_api_key,
)
```

**Supported Backends:**
- Ollama (local, default): `http://host.docker.internal:11434/v1`
- OpenAI: `https://api.openai.com/v1`
- Groq: `https://api.groq.com/openai/v1`

**Recommended Models:**
- `qwen2.5:7b` — best JSON accuracy (Ollama)
- `llama3.2` — good general extraction (Ollama default)
- `gpt-4o-mini` — excellent accuracy, low cost

---

## File Permissions & Security

- `data/`, `invoices/`, `output/` are bind-mounted volumes
- Container runs as root inside (required for Docling)
- Never commit `.env` or real API keys
- PDFs in `invoices/` are read-only to pipeline, rw to dashboard
- Exported files in `output/export/` are the "system of record"

---

## Troubleshooting

**Docling model download fails:**
```bash
# Models are cached in named volume 'docling-models'
docker volume rm parsely-invoices_docling-models  # force re-download
```

**SQLite locked:**
- Dashboard and pipeline can run simultaneously
- Uses WAL mode for concurrent reads
- If locked, check no other process is holding the DB

**LLM connection fails:**
```bash
# Test LLM reachability
docker compose run --rm pipeline check

# Common issues:
# - Ollama not running on host
# - Wrong LLM_BASE_URL (host.docker.internal vs localhost)
# - Model not pulled in Ollama
```

---

## Dependencies

Key packages (see `requirements.txt` for full list):
- `docling` — layout-aware PDF extraction
- `pdfplumber` — supplementary table extraction
- `pydantic` — data validation
- `openai` — LLM client
- `rapidfuzz` — fuzzy string matching
- `fastapi` / `uvicorn` — dashboard
- `click` — CLI framework

---

*This file should be updated when significant architectural changes are made.*
