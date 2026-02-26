"""
Pytest configuration and shared fixtures for Parsely test suite.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Generator

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
os.chdir(PROJECT_ROOT)


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Provide a temporary directory for test files."""
    tmp_path = tempfile.mkdtemp(prefix="parsely_test_")
    yield Path(tmp_path)
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def test_config(temp_dir: Path) -> "Config":
    """Provide a test configuration with isolated directories."""
    from config import Config
    
    config = Config()
    # Override paths to use temp directory
    config.output_dir = temp_dir / "output"
    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.export_dir = temp_dir / "output" / "export"
    config.export_dir.mkdir(parents=True, exist_ok=True)
    config.db_path = temp_dir / "output" / "pipeline.db"
    config.suppliers_csv = temp_dir / "data" / "suppliers.csv"
    config.po_csv = temp_dir / "data" / "purchase_orders.csv"
    config.po_lines_csv = temp_dir / "data" / "purchase_order_lines.csv"
    
    # Ensure data directory exists
    config.suppliers_csv.parent.mkdir(parents=True, exist_ok=True)
    
    return config


@pytest.fixture
def sample_suppliers_csv(temp_dir: Path) -> Path:
    """Create a sample suppliers CSV file."""
    csv_path = temp_dir / "suppliers.csv"
    content = """id,name,abn,acn,email,phone,address,aliases
SUP-001,Acme Supplies Pty Ltd,12 345 678 901,123 456 789,accounts@acme.com,02 9876 5432,"123 Main St, Sydney",
SUP-002,Global Logistics Ltd,98 765 432 109,,logistics@global.com,03 5555 1212,"456 Queen St, Melbourne",Global Logistics|Global Log
SUP-003,Tech Solutions Inc,,,,,,"""
    csv_path.write_text(content)
    return csv_path


@pytest.fixture
def sample_po_csv(temp_dir: Path) -> Path:
    """Create a sample purchase orders CSV file."""
    csv_path = temp_dir / "purchase_orders.csv"
    content = """po_number,supplier_id,supplier_name,issue_date,expected_delivery,subtotal,tax_amount,total,currency,status,notes
PO-2024-001,SUP-001,Acme Supplies Pty Ltd,2024-01-15,2024-02-15,1000.00,100.00,1100.00,AUD,approved,Standard order
PO-2024-002,SUP-002,Global Logistics Ltd,2024-01-20,2024-03-01,5000.00,500.00,5500.00,AUD,approved,Urgent delivery
PO-2024-003,SUP-001,Acme Supplies Pty Ltd,2024-02-01,2024-03-01,250.00,25.00,275.00,AUD,approved,Small items"""
    csv_path.write_text(content)
    return csv_path


@pytest.fixture
def sample_po_lines_csv(temp_dir: Path) -> Path:
    """Create a sample PO lines CSV file."""
    csv_path = temp_dir / "purchase_order_lines.csv"
    content = """po_number,line_number,sku,description,quantity,unit,unit_price,total
PO-2024-001,1,ITEM-001,Office Chairs (Ergonomic),10,ea,50.00,500.00
PO-2024-001,2,ITEM-002,Desks (Standing),5,ea,100.00,500.00
PO-2024-002,1,LOG-001,Freight Services,1,lot,5000.00,5000.00
PO-2024-003,1,ITEM-003,Stationery Pack,25,ea,10.00,250.00"""
    csv_path.write_text(content)
    return csv_path


@pytest.fixture
def sample_extracted_invoice() -> dict:
    """Return a sample extracted invoice structure."""
    return {
        "invoice_number": "INV-2024-001",
        "invoice_date": "2024-01-25",
        "due_date": "2024-02-25",
        "supplier": {
            "name": "Acme Supplies Pty Ltd",
            "abn": "12 345 678 901",
            "email": "accounts@acme.com",
            "address": "123 Main St, Sydney"
        },
        "po_number": "PO-2024-001",
        "line_items": [
            {
                "line_number": 1,
                "sku": "ITEM-001",
                "description": "Office Chairs (Ergonomic)",
                "quantity": 10,
                "unit": "ea",
                "unit_price": 50.00,
                "total": 500.00
            },
            {
                "line_number": 2,
                "sku": "ITEM-002",
                "description": "Desks (Standing)",
                "quantity": 5,
                "unit": "ea",
                "unit_price": 100.00,
                "total": 500.00
            }
        ],
        "subtotal": 1000.00,
        "tax_rate": 0.10,
        "tax_amount": 100.00,
        "total": 1100.00,
        "currency": "AUD"
    }


@pytest.fixture
def sample_matched_supplier() -> dict:
    """Return a sample matched supplier structure."""
    return {
        "supplier_id": "SUP-001",
        "supplier_name": "Acme Supplies Pty Ltd",
        "match_method": "abn_exact",
        "confidence": 1.0,
        "abn": "12 345 678 901",
        "matched_on": {"field": "abn", "value": "12 345 678 901"}
    }


@pytest.fixture
def mock_llm_response() -> dict:
    """Return a mock LLM extraction response."""
    return {
        "invoice_number": "INV-TEST-001",
        "invoice_date": "2024-01-15",
        "due_date": "2024-02-15",
        "supplier": {
            "name": "Test Supplier Ltd",
            "abn": "11 222 333 444",
            "email": "test@example.com"
        },
        "po_number": "PO-001",
        "line_items": [
            {
                "line_number": 1,
                "description": "Consulting Services",
                "quantity": 5,
                "unit_price": 100.00,
                "total": 500.00
            }
        ],
        "subtotal": 500.00,
        "tax_rate": 0.10,
        "tax_amount": 50.00,
        "total": 550.00,
        "currency": "AUD"
    }


@pytest.fixture
def test_db(test_config) -> "Database":
    """Provide a test database instance."""
    from pipeline.database import Database
    return Database(test_config.db_path)


@pytest.fixture
def sample_pdf_path(temp_dir: Path) -> Path:
    """Create a minimal test PDF file."""
    pdf_path = temp_dir / "test_invoice.pdf"
    
    # Create a minimal valid PDF
    # This is a very basic PDF structure for testing
    pdf_content = b"""%PDF-1.4
1 0 obj
<<
/Type /Catalog
/Pages 2 0 R
>>
endobj

2 0 obj
<<
/Type /Pages
/Kids [3 0 R]
/Count 1
>>
endobj

3 0 obj
<<
/Type /Page
/Parent 2 0 R
/MediaBox [0 0 612 792]
/Contents 4 0 R
>>
endobj

4 0 obj
<<
/Length 44
>>
stream
BT
/F1 12 Tf
100 700 Td
(Test Invoice) Tj
ET
endstream
endobj

xref
0 5
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000214 00000 n 

trailer
<<
/Size 5
/Root 1 0 R
>>
startxref
308
%%EOF"""
    
    pdf_path.write_bytes(pdf_content)
    return pdf_path


@pytest.fixture
def sample_invoice_in_db(test_db, temp_dir):
    """Create a sample invoice in the database for API tests."""
    result_dict = {
        "extracted_invoice": {
            "invoice_number": "INV-TEST-001",
            "invoice_date": "2024-01-15",
            "supplier": {"name": "Test Supplier", "abn": "12 345 678 901"},
            "total": 1100.00,
            "currency": "AUD"
        },
        "requires_review": False,
        "error_count": 0,
        "warning_count": 0,
        "processed_at": "2024-01-15T10:00:00Z"
    }
    
    # Create invoices directory if not exists
    invoices_dir = temp_dir / "invoices"
    invoices_dir.mkdir(parents=True, exist_ok=True)
    
    # Create a dummy PDF file
    pdf_path = invoices_dir / "test_invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test content")
    
    test_db.upsert_invoice(
        stem="test_invoice",
        result_dict=result_dict,
        source_file=str(pdf_path),
        source_mtime=pdf_path.stat().st_mtime
    )
    
    return "test_invoice"


# Configure pytest markers
def pytest_configure(config):
    """Configure custom pytest markers."""
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "api: API tests")
    config.addinivalue_line("markers", "slow: Slow tests")
