"""
Unit tests for invoice validation functionality.
"""
from datetime import date, timedelta

import pytest

from models.invoice import ExtractedInvoice, LineItem, SupplierInfo
from models.result import MatchedPO, POLineMatch, MatchedSupplier
from pipeline.validator import InvoiceValidator


@pytest.mark.unit
class TestInvoiceValidator:
    """Tests for InvoiceValidator class."""

    @pytest.fixture
    def validator(self):
        """Provide a default validator instance."""
        return InvoiceValidator()

    def test_no_discrepancies_for_valid_invoice(self, validator):
        """Test a valid invoice has no discrepancies."""
        invoice = ExtractedInvoice(
            invoice_number="INV-001",
            invoice_date=date.today().isoformat(),
            supplier=SupplierInfo(name="Test Supplier"),
            line_items=[
                LineItem(description="Item 1", quantity=1, unit_price=100, total=100)
            ],
            subtotal=100,
            tax_rate=0.10,
            tax_amount=10,
            total=110
        )
        
        discrepancies = validator.validate(invoice, None, None, None)
        
        # Should have no errors, maybe some info/warnings
        errors = [d for d in discrepancies if d.severity == "error"]
        assert len(errors) == 0

    def test_arithmetic_error_subtotal_mismatch(self, validator):
        """Test detection of subtotal vs line items mismatch."""
        invoice = ExtractedInvoice(
            line_items=[
                LineItem(description="Item 1", quantity=2, unit_price=50, total=100),
                LineItem(description="Item 2", quantity=1, unit_price=50, total=50)
            ],
            subtotal=200  # Wrong - should be 150
        )
        
        discrepancies = validator.validate(invoice, None, None, None)
        
        errors = [d for d in discrepancies if d.type == "line_items_subtotal_mismatch"]
        assert len(errors) == 1
        assert errors[0].severity == "error"

    def test_tax_calculation_mismatch(self, validator):
        """Test detection of tax calculation errors."""
        invoice = ExtractedInvoice(
            subtotal=1000,
            tax_rate=0.10,
            tax_amount=150,  # Wrong - should be 100
            total=1100
        )
        
        discrepancies = validator.validate(invoice, None, None, None)
        
        warnings = [d for d in discrepancies if d.type == "tax_calculation_mismatch"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"

    def test_grand_total_mismatch(self, validator):
        """Test detection of grand total errors."""
        invoice = ExtractedInvoice(
            subtotal=1000,
            tax_amount=100,
            total=1000  # Wrong - should be 1100
        )
        
        discrepancies = validator.validate(invoice, None, None, None)
        
        errors = [d for d in discrepancies if d.type == "grand_total_mismatch"]
        assert len(errors) == 1
        assert errors[0].severity == "error"

    def test_future_invoice_date(self, validator):
        """Test detection of future invoice dates."""
        future_date = (date.today() + timedelta(days=30)).isoformat()
        invoice = ExtractedInvoice(
            invoice_date=future_date
        )
        
        discrepancies = validator.validate(invoice, None, None, None)
        
        errors = [d for d in discrepancies if d.type == "invoice_date_future"]
        assert len(errors) == 1

    def test_old_invoice_date(self, validator):
        """Test detection of very old invoices."""
        old_date = (date.today() - timedelta(days=200)).isoformat()
        invoice = ExtractedInvoice(
            invoice_date=old_date
        )
        
        discrepancies = validator.validate(invoice, None, None, None)
        
        warnings = [d for d in discrepancies if d.type == "invoice_date_too_old"]
        assert len(warnings) == 1

    def test_missing_required_fields(self, validator):
        """Test detection of missing required fields."""
        invoice = ExtractedInvoice(
            invoice_number=None,
            invoice_date=None,
            supplier=SupplierInfo(name=None),
            total=None
        )
        
        discrepancies = validator.validate(invoice, None, None, None)
        
        error_types = [d.type for d in discrepancies if d.severity == "error"]
        assert "missing_total" in error_types

    def test_negative_amount(self, validator):
        """Test detection of negative amounts."""
        invoice = ExtractedInvoice(
            total=-100
        )
        
        discrepancies = validator.validate(invoice, None, None, None)
        
        errors = [d for d in discrepancies if d.type == "negative_amount"]
        assert len(errors) == 1

    def test_po_not_found(self, validator):
        """Test detection of PO not found."""
        invoice = ExtractedInvoice(
            po_number="PO-NONEXISTENT"
        )
        matched_po = MatchedPO(
            po_number="PO-NONEXISTENT",
            match_method="invoice_reference"
        )
        
        discrepancies = validator.validate(invoice, matched_po, None, None)
        
        errors = [d for d in discrepancies if d.type == "po_not_found"]
        assert len(errors) == 1

    def test_po_total_exceeded(self, validator):
        """Test detection when invoice total exceeds PO total."""
        invoice = ExtractedInvoice(
            po_number="PO-001",
            total=1500
        )
        matched_po = MatchedPO(
            po_number="PO-001",
            match_method="invoice_reference",
            po_total=1000
        )
        
        discrepancies = validator.validate(invoice, matched_po, None, None)
        
        errors = [d for d in discrepancies if d.type == "po_total_exceeded"]
        assert len(errors) == 1

    def test_po_line_quantity_mismatch(self, validator, sample_extracted_invoice):
        """Test detection of PO line quantity mismatch."""
        from models.purchase_order import PurchaseOrder, POLineItem
        
        invoice = ExtractedInvoice(
            line_items=[
                LineItem(description="Office Chairs", quantity=20, unit_price=50, total=1000)
            ],
            po_number="PO-001"
        )
        
        matched_po = MatchedPO(
            po_number="PO-001",
            match_method="invoice_reference",
            line_matches=[
                POLineMatch(
                    invoice_line_index=0,
                    invoice_description="Office Chairs",
                    po_line_number=1,
                    matched=True,
                    quantity_matches=False
                )
            ]
        )
        
        po_record = PurchaseOrder(
            po_number="PO-001",
            line_items=[
                POLineItem(description="Office Chairs", quantity=10, unit_price=50, total=500)
            ]
        )
        
        discrepancies = validator.validate(invoice, matched_po, None, po_record)
        
        warnings = [d for d in discrepancies if d.type == "po_line_quantity_mismatch"]
        assert len(warnings) == 1

    def test_supplier_not_found(self, validator):
        """Test warning when supplier not matched."""
        invoice = ExtractedInvoice(
            supplier=SupplierInfo(name="Unknown Supplier Inc")
        )
        
        discrepancies = validator.validate(invoice, None, None, None)
        
        warnings = [d for d in discrepancies if d.type == "supplier_not_found"]
        assert len(warnings) == 1
