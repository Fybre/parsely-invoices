"""
Integration tests for database operations.
"""
import json
from datetime import datetime, timezone

import pytest

from pipeline.database import Database, STATUS_NEEDS_REVIEW, STATUS_READY, STATUS_EXPORTED


@pytest.mark.integration
class TestDatabase:
    """Integration tests for Database class."""

    def test_upsert_and_get_invoice(self, test_db):
        """Test inserting and retrieving an invoice."""
        result_dict = {
            "extracted_invoice": {
                "invoice_number": "INV-001",
                "total": 1000
            },
            "requires_review": False,
            "error_count": 0,
            "warning_count": 0
        }
        
        status = test_db.upsert_invoice(
            stem="test_invoice",
            result_dict=result_dict,
            source_file="/path/to/test_invoice.pdf",
            source_mtime=1234567890.0
        )
        
        assert status == STATUS_READY
        
        # Retrieve the invoice
        invoice = test_db.get_invoice("test_invoice")
        assert invoice is not None
        assert invoice["stem"] == "test_invoice"
        assert invoice["status"] == STATUS_READY

    def test_is_processed_detection(self, test_db):
        """Test detection of already processed files."""
        result_dict = {"extracted_invoice": {"invoice_number": "INV-001"}}
        
        # First processing
        test_db.upsert_invoice(
            stem="test_invoice",
            result_dict=result_dict,
            source_file="/path/to/test.pdf",
            source_mtime=1000.0
        )
        
        # Same mtime - should be processed
        assert test_db.is_processed("test_invoice", 1000.0) is True
        
        # Different mtime - should need reprocessing
        assert test_db.is_processed("test_invoice", 2000.0) is False
        
        # New invoice - not processed
        assert test_db.is_processed("new_invoice", 1000.0) is False

    def test_update_status(self, test_db):
        """Test updating invoice status."""
        result_dict = {"extracted_invoice": {}}
        test_db.upsert_invoice(
            stem="test_invoice",
            result_dict=result_dict,
            source_file="/path/to/test.pdf",
            source_mtime=1000.0
        )
        
        # Update to ready
        test_db.update_status("test_invoice", STATUS_READY)
        invoice = test_db.get_invoice("test_invoice")
        assert invoice["status"] == STATUS_READY
        assert invoice["exported_at"] is None
        
        # Update to exported
        test_db.update_status("test_invoice", STATUS_EXPORTED)
        invoice = test_db.get_invoice("test_invoice")
        assert invoice["status"] == STATUS_EXPORTED
        assert invoice["exported_at"] is not None

    def test_corrections_and_notes(self, test_db):
        """Test saving corrections and notes."""
        result_dict = {"extracted_invoice": {}}
        test_db.upsert_invoice(
            stem="test_invoice",
            result_dict=result_dict,
            source_file="/path/to/test.pdf",
            source_mtime=1000.0
        )
        
        # Save corrections
        corrections = {"invoice_number": "INV-CORRECTED", "total": 2000}
        test_db.update_corrections("test_invoice", corrections)
        
        # Save notes
        test_db.update_notes("test_invoice", "Test note content")
        
        # Retrieve and verify
        invoice = test_db.get_invoice("test_invoice")
        assert json.loads(invoice["corrections"]) == corrections
        assert invoice["notes"] == "Test note content"

    def test_list_invoices_with_filters(self, test_db):
        """Test listing invoices with status filters."""
        # Insert invoices with different statuses
        for i, status in enumerate([STATUS_NEEDS_REVIEW, STATUS_READY, STATUS_EXPORTED]):
            result_dict = {
                "extracted_invoice": {"invoice_number": f"INV-{i}"},
                "requires_review": status == STATUS_NEEDS_REVIEW,
                "error_count": 1 if status == STATUS_NEEDS_REVIEW else 0,
                "warning_count": 0
            }
            test_db.upsert_invoice(
                stem=f"invoice_{i}",
                result_dict=result_dict,
                source_file=f"/path/to/invoice_{i}.pdf",
                source_mtime=1000.0 + i
            )
            test_db.update_status(f"invoice_{i}", status)
        
        # List all
        all_invoices = test_db.list_invoices()
        assert len(all_invoices) == 3
        
        # Filter by status
        ready_invoices = test_db.list_invoices(status=STATUS_READY)
        assert len(ready_invoices) == 1
        assert ready_invoices[0]["status"] == STATUS_READY

    def test_search_invoices(self, test_db):
        """Test searching invoices."""
        # Insert test invoices
        test_db.upsert_invoice(
            stem="acme_invoice",
            result_dict={
                "extracted_invoice": {"invoice_number": "INV-ACME-001"},
                "supplier": {"name": "Acme Corp"}
            },
            source_file="/path/to/acme.pdf",
            source_mtime=1000.0
        )
        test_db.upsert_invoice(
            stem="globex_invoice",
            result_dict={
                "extracted_invoice": {"invoice_number": "INV-GLOBEX-001"},
                "supplier": {"name": "Globex Inc"}
            },
            source_file="/path/to/globex.pdf",
            source_mtime=1001.0
        )
        
        # Search by invoice number
        results = test_db.list_invoices(search="ACME")
        assert len(results) == 1
        assert results[0]["stem"] == "acme_invoice"

    def test_delete_invoice(self, test_db):
        """Test deleting an invoice."""
        result_dict = {"extracted_invoice": {"invoice_number": "INV-001"}}
        test_db.upsert_invoice(
            stem="test_invoice",
            result_dict=result_dict,
            source_file="/path/to/test.pdf",
            source_mtime=1000.0
        )
        
        # Delete
        deleted = test_db.delete_invoice("test_invoice")
        assert deleted is True
        
        # Verify deleted
        invoice = test_db.get_invoice("test_invoice")
        assert invoice is None

    def test_reset_for_reprocess(self, test_db):
        """Test resetting invoice for reprocessing."""
        result_dict = {"extracted_invoice": {"invoice_number": "INV-001"}}
        test_db.upsert_invoice(
            stem="test_invoice",
            result_dict=result_dict,
            source_file="/path/to/test.pdf",
            source_mtime=1000.0
        )
        test_db.update_status("test_invoice", STATUS_EXPORTED)
        test_db.update_corrections("test_invoice", {"total": 100})
        
        # Reset
        reset = test_db.reset_for_reprocess("test_invoice")
        assert reset is True
        
        # Verify reset
        invoice = test_db.get_invoice("test_invoice")
        assert invoice["status"] == STATUS_NEEDS_REVIEW
        assert invoice["source_mtime"] == 0
        assert invoice["corrections"] is None
        assert invoice["exported_at"] is None

    def test_get_stats(self, test_db):
        """Test getting database statistics."""
        # Insert test data
        for i in range(3):
            result_dict = {
                "extracted_invoice": {},
                "error_count": i,
                "warning_count": i
            }
            test_db.upsert_invoice(
                stem=f"invoice_{i}",
                result_dict=result_dict,
                source_file=f"/path/to/invoice_{i}.pdf",
                source_mtime=1000.0 + i
            )
        
        stats = test_db.get_stats()
        
        assert stats["total"] == 3
        assert stats["total_errors"] == 3  # 0 + 1 + 2
        assert stats["total_warnings"] == 3
