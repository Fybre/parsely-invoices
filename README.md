# Parsely — Invoice Processing Pipeline

An AI-powered invoice processing pipeline that extracts structured data from PDF invoices, matches them against Purchase Orders and a supplier master list, flags discrepancies, and surfaces everything through a web dashboard.

**Docker is the recommended way to run Parsely.** All dependencies — Python, Docling ML models, PDF rendering tools — are bundled in the image.

---

## Features

- **Layout-aware PDF extraction** — Docling preserves table structure and handles multi-column layouts; pdfplumber provides supplementary table extraction for edge cases
- **OCR support** — Docling's built-in OCR handles scanned/image PDFs automatically
- **Structured LLM extraction** — works with any OpenAI-compatible API: local Ollama, OpenAI, Groq, or any other hosted model
- **Direct table extraction** — line items are parsed directly from PDF tables where possible, reducing LLM load and improving accuracy
- **Supplier matching** — ABN exact → name exact → fuzzy name → email domain
- **PO matching** — matches by PO number; fuzzy line-item comparison by description and SKU
- **Discrepancy detection**:
  - Arithmetic: line item totals, tax calculation, grand total
  - Dates: future-dated, stale (>90 days), overdue, due before invoice date
  - PO: not found, supplier mismatch, total exceeded, quantity/price mismatches
  - Data quality: missing invoice number, supplier, total; negative or zero amounts
- **Web dashboard** — review, correct, and approve invoices; upload PDFs directly from the browser; dark mode support
- **Admin page** — edit suppliers, POs, and PO lines in-browser; reload data without restarting
- **PO line lookup** — in edit mode, view PO lines and copy SKU/description to invoice line items
- **Custom fields** — define site-specific fields (e.g. strata reference, job number) extracted alongside standard fields
- **SQLite-backed state** — all results, corrections, and approvals stored in a single database; no per-invoice JSON files cluttering the filesystem
- **Export on approval** — approved invoices written to `output/export/` as JSON + PDF for downstream system pickup

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) (v2)
- An LLM backend — one of:
  - **[Ollama](https://ollama.com)** running locally (free, fully offline)
  - **OpenAI** API key
  - **[Groq](https://console.groq.com)** API key (free tier available, fast)

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/Fybre/parsely-invoices.git
cd parsely-invoices
cp .env.example .env
```

Open `.env` and set your LLM connection details (see [Configuration](#configuration) below).

### 2. Build the image

```bash
docker compose build
```

> On first run, Docling downloads ~1 GB of ML models into a named Docker volume (`docling-models`). This only happens once.

### 3. Verify your setup

```bash
docker compose run --rm pipeline check
```

This confirms the LLM backend is reachable, the selected model is available, and the data CSV files are found.

### 4. Start the dashboard

```bash
docker compose up -d dashboard
```

Open **http://localhost:8080** in your browser. You can upload invoices directly from the dashboard, or drop PDFs into the `invoices/` folder.

### 5. Process invoices

**One-shot batch** (process everything in `invoices/` then exit):
```bash
docker compose run --rm pipeline
```

**Continuous watch mode** (poll for new PDFs every N seconds):
```bash
WATCH_MODE=true docker compose up -d pipeline
```

Or set `WATCH_MODE=true` in your `.env` and run:
```bash
docker compose up -d
```

---

## Configuration

All settings live in `.env`. Copy `.env.example` to get started — it contains full comments for every option.

### LLM backend

| Setting | Description |
|---|---|
| `LLM_BASE_URL` | API endpoint (OpenAI-compatible) |
| `LLM_MODEL` | Model name |
| `LLM_API_KEY` | API key (`ollama` works for local Ollama) |

**Common configurations:**

```bash
# Ollama running on this machine (default)
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=qwen2.5:7b
LLM_API_KEY=ollama

# Ollama on another machine on your network
LLM_BASE_URL=http://192.168.1.50:11434/v1
LLM_MODEL=qwen2.5:7b
LLM_API_KEY=ollama

# OpenAI
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...

# Groq (free tier, fast)
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.3-70b-versatile
LLM_API_KEY=gsk_...
```

### Recommended models

| Model | Notes |
|---|---|
| `qwen2.5:7b` | Best JSON extraction accuracy (Ollama) |
| `llama3.2` | Good general extraction (Ollama default) |
| `gpt-4o-mini` | Excellent accuracy, low cost (OpenAI) |
| `llama-3.3-70b-versatile` | Fast and accurate (Groq free tier) |

### Other settings

| Setting | Default | Description |
|---|---|---|
| `WATCH_MODE` | `false` | `true` = continuous polling; `false` = single batch pass |
| `POLL_INTERVAL` | `30` | Seconds between directory scans in watch mode |
| `USE_DOCLING` | `true` | `false` = use plain pdfplumber only (faster, less accurate) |
| `DASHBOARD_PORT` | `8080` | Port the dashboard listens on |

---

## Data Files

Place your reference data in the `data/` directory. These are mounted into the container at runtime — no rebuild needed when you update them.

### `data/suppliers.csv`

| Column | Description |
|---|---|
| `id` | Unique supplier ID |
| `name` | Canonical supplier name |
| `abn` | ABN (with or without spaces) |
| `acn` | ACN (optional) |
| `email` | Billing email |
| `phone` | Phone number |
| `address` | Physical address |
| `aliases` | Pipe-separated (`\|`) alternative trading names |

### `data/purchase_orders.csv`

| Column | Description |
|---|---|
| `po_number` | PO number *(must match what appears on invoices)* |
| `supplier_id` | References `id` in suppliers.csv |
| `supplier_name` | Supplier name on the PO |
| `issue_date` | PO issue date (YYYY-MM-DD) |
| `expected_delivery` | Expected delivery date (YYYY-MM-DD) |
| `subtotal` | Pre-tax total |
| `tax_amount` | Tax amount |
| `total` | Grand total |
| `currency` | e.g. `AUD` |
| `status` | `open` / `closed` / `partially_received` |

### `data/purchase_order_lines.csv`

| Column | Description |
|---|---|
| `po_number` | References `po_number` in purchase_orders.csv |
| `line_number` | Line sequence number |
| `sku` | Product SKU (optional but improves matching) |
| `description` | Line item description |
| `quantity` | Ordered quantity |
| `unit` | Unit (ea, box, hr, kg, etc.) |
| `unit_price` | Unit price |
| `total` | Line total |

---

## Custom Fields

Define site-specific fields to extract alongside the standard invoice fields. Edit `config/custom_fields.json`:

```json
{
  "section_title": "Additional Fields",
  "fields": [
    {
      "name": "strata_reference",
      "label": "Strata Reference",
      "description": "Strata plan or lot reference number",
      "patterns": ["strata\\s*ref[^:]*:\\s*([A-Z0-9/-]+)"]
    },
    {
      "name": "job_number",
      "label": "Job Number",
      "description": "Internal job or work order number",
      "patterns": ["job\\s*(?:no\\.?|number|#)\\s*:?\\s*([A-Z0-9-]+)"]
    }
  ]
}
```

Each field is extracted using both regex patterns (from `patterns`) and the LLM. Extracted values appear in a dedicated card on the dashboard.

---

## Dashboard

Access the dashboard at **http://localhost:8080** after starting it with `docker compose up -d dashboard`.

Key features:
- **Upload** — drag-and-drop or click to upload a PDF directly from the browser; the pipeline picks it up on the next poll cycle
- **Review** — side-by-side PDF viewer and extracted data panel; flag invoices for review or mark them ready
- **Correct** — edit any extracted field inline; corrections are stored separately and applied on export
- **PO Lookup** — in edit mode, view PO lines and copy data to invoice line items for quick correction
- **Export** — approve individual invoices or bulk-export all ready invoices; exports land in `output/export/` as `<stem>.json` + `<stem>.pdf`
- **Reprocess** — clear the record and let the pipeline re-extract from scratch
- **Dark mode** — toggle between light and dark themes

The dashboard reads from the same `output/pipeline.db` database that the pipeline writes to. Both services can run simultaneously.

### Admin Page

Access the admin page at **http://localhost:8080/admin** to manage reference data:

- **Edit CSV files** — suppliers, purchase orders, and PO lines can be edited directly in the browser
- **Upload CSV** — replace entire data files with a CSV upload
- **Reload into Pipeline** — after editing data files, click to reload them into the running pipeline without restarting
- **Database** — view pipeline stats and clear the database if needed

> **Note:** CSV files are loaded into memory at startup. After making changes via the admin page, you must click **"Reload into Pipeline"** for the changes to take effect for new invoices.

---

## Project Structure

```
parsely-invoices/
├── docker-compose.yml           Service definitions (pipeline + dashboard)
├── Dockerfile                   Multi-stage build
├── docker-entrypoint.sh         Batch vs watch mode selection
├── .env.example                 Template for environment configuration
├── main.py                      CLI entry point (check / process / watch)
├── config.py                    Thresholds and pipeline settings
├── requirements.txt
│
├── config/                      Operator-editable config (mounted into container)
│   ├── custom_fields.json       Site-specific extraction fields
│   └── column_keys.json         Column header synonyms for table extraction
│
├── pipeline/
│   ├── extractor.py             Docling + pdfplumber PDF extraction
│   ├── llm_parser.py            LLM structured extraction
│   ├── custom_field_extractor.py  Site-specific field extraction
│   ├── supplier_matcher.py      Supplier identification
│   ├── po_matcher.py            PO matching and line comparison
│   ├── validator.py             Discrepancy detection
│   ├── processor.py             Pipeline orchestrator
│   └── database.py              SQLite persistence
│
├── models/
│   ├── invoice.py               ExtractedInvoice Pydantic model
│   ├── purchase_order.py        PurchaseOrder model
│   ├── supplier.py              Supplier model
│   └── result.py                InvoiceProcessingResult + Discrepancy models
│
├── dashboard/
│   ├── app.py                   FastAPI backend
│   ├── templates/index.html     Single-page dashboard UI
│   └── templates/admin.html     Admin page for CSV/data management
│
├── data/                        Reference CSVs (gitignored — add your own)
│   ├── suppliers.csv
│   ├── purchase_orders.csv
│   └── purchase_order_lines.csv
│
├── invoices/                    Drop PDFs here (gitignored)
└── output/                      Database + exports (gitignored)
    ├── pipeline.db
    └── export/
```

---

## Optional: Ollama as a Docker sidecar

If you don't have Ollama installed on the host, you can run it as a container alongside the pipeline:

```bash
docker compose --profile with-ollama up -d
```

Then pull your model once:
```bash
docker compose exec ollama ollama pull qwen2.5:7b
```

And point the pipeline at it in `.env`:
```bash
LLM_BASE_URL=http://ollama:11434/v1
LLM_MODEL=qwen2.5:7b
LLM_API_KEY=ollama
```

---

## Running Without Docker

For local development without Docker:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Verify setup
python main.py check

# Process invoices
python main.py process invoices/

# Start dashboard
uvicorn dashboard.app:app --host 0.0.0.0 --port 8080 --reload
```

Requires Python 3.11+ and a running LLM backend. Docling will download its models (~1 GB) on first use.
