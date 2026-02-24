# Invoice Processing Pipeline

A fully local invoice processing pipeline that extracts structured data from PDF invoices using a local LLM (via Ollama), matches invoices against Purchase Orders and a supplier master list, and flags discrepancies.

## Features

- **PDF text extraction** — digital (text-layer) PDFs via `pdfplumber`; OCR-ready hook for scanned PDFs
- **LLM-powered extraction** — structured JSON via a local Ollama model (no cloud API calls)
- **Supplier matching** — ABN exact → name exact → fuzzy name → email domain
- **PO matching** — matches by PO number reference; fuzzy line-item matching by description + SKU
- **Discrepancy detection**:
  - Arithmetic: line item sum vs subtotal, tax calculation, grand total
  - Dates: future-dated invoices, invoices older than 90 days, overdue, due date before invoice date
  - PO: PO not found, supplier mismatch, total exceeded, line item quantity/price mismatches
  - Data quality: missing invoice number, supplier, total; negative or zero amounts
- **Machine-readable JSON output** — one JSON file per invoice + a batch summary

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) installed and running locally

### Recommended Ollama models (in order of extraction accuracy)

| Model | Pull command |
|---|---|
| `qwen2.5:7b` *(best JSON accuracy)* | `ollama pull qwen2.5:7b` |
| `llama3.2` *(default)* | `ollama pull llama3.2` |
| `mistral` | `ollama pull mistral` |

---

## Installation

```bash
cd invoice_pipeline
pip install -r requirements.txt
```

---

## Quick Start

### 1. Verify your setup
```bash
python main.py check
```

This confirms Ollama is running, your chosen model is available, and the data CSV files are found.

### 2. Process a single invoice
```bash
python main.py process invoices/my_invoice.pdf
```

### 3. Batch process a folder
```bash
python main.py process invoices/
```

Results are written to the `output/` directory.

---

## Data Files

Place your data files in the `data/` directory (or pass `--suppliers`, `--po-csv`, `--po-lines-csv` flags).

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
| `po_number` | PO number *(primary key — must match what appears on invoices)* |
| `supplier_id` | References `id` in suppliers.csv |
| `supplier_name` | Supplier name on the PO |
| `issue_date` | PO issue date (YYYY-MM-DD) |
| `expected_delivery` | Expected delivery date (YYYY-MM-DD) |
| `subtotal` | Pre-tax total |
| `tax_amount` | Tax amount |
| `total` | Grand total |
| `currency` | e.g. AUD |
| `status` | open / closed / partially_received |
| `notes` | Optional notes |

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

## Output Format

Each invoice produces a JSON file in `output/` with this top-level structure:

```json
{
  "source_file": "invoices/INV-001.pdf",
  "processed_at": "2026-02-24T08:00:00+00:00",
  "processing_time_seconds": 4.2,
  "extracted_invoice": { ... },
  "matched_supplier": { ... },
  "matched_po": { ... },
  "discrepancies": [
    {
      "type": "grand_total_mismatch",
      "severity": "error",
      "description": "Grand total (1105.00) does not match sum of components (1100.00)",
      "field": "total",
      "invoice_value": "1105.00",
      "expected_value": "1100.00"
    }
  ],
  "requires_review": true,
  "review_reasons": ["Grand total (1105.00) does not match sum of components (1100.00)"],
  "error_count": 1,
  "warning_count": 0
}
```

A `batch_summary.json` is also written after batch processing, giving a one-row-per-invoice summary.

---

## Advanced Usage

### Use a different model
```bash
python main.py process invoices/ --model qwen2.5:7b
```

### Override data file locations
```bash
python main.py process invoices/ \
  --suppliers /path/to/suppliers.csv \
  --po-csv /path/to/pos.csv \
  --po-lines-csv /path/to/po_lines.csv \
  --output /path/to/results/
```

### Compact (non-indented) JSON output
```bash
python main.py process invoices/ --no-pretty
```

### Debug logging
```bash
python main.py -v process invoices/
```

---

## OCR Support (Scanned PDFs)

The pipeline detects scanned PDFs and logs a warning. To enable OCR:

1. Install Tesseract: https://github.com/tesseract-ocr/tesseract
2. Install Python deps: `pip install pdf2image pytesseract Pillow`
3. In your code, call `extractor.extract_with_ocr(pdf_path)` instead of `extract()`

---

## Project Structure

```
invoice_pipeline/
├── main.py                      CLI entry point
├── config.py                    Configuration and thresholds
├── requirements.txt
├── pipeline/
│   ├── extractor.py             PDF text extraction
│   ├── llm_parser.py            Ollama LLM structured extraction
│   ├── supplier_matcher.py      Supplier identification
│   ├── po_matcher.py            PO matching and line comparison
│   ├── validator.py             Discrepancy detection
│   └── processor.py             Pipeline orchestrator
├── models/
│   ├── invoice.py               ExtractedInvoice Pydantic model
│   ├── purchase_order.py        PurchaseOrder Pydantic model
│   ├── supplier.py              Supplier Pydantic model
│   └── result.py                InvoiceProcessingResult + Discrepancy models
├── data/
│   ├── suppliers.csv            Supplier master list
│   ├── purchase_orders.csv      PO headers
│   └── purchase_order_lines.csv PO line items
├── invoices/                    Drop invoice PDFs here
└── output/                      JSON results written here
```

---

## Customising Thresholds

Edit `config.py` or pass a `Config` instance to `InvoiceProcessor`:

```python
from config import Config
from pipeline.processor import InvoiceProcessor

config = Config(
    ollama_model="qwen2.5:7b",
    max_invoice_age_days=60,     # Warn on invoices > 60 days old
    max_future_days=0,            # Never allow future dates
    arithmetic_tolerance=0.10,   # Allow $0.10 rounding difference
    supplier_fuzzy_threshold=80, # Stricter supplier name matching
)
processor = InvoiceProcessor(config)
result = processor.process("invoices/my_invoice.pdf")
```
