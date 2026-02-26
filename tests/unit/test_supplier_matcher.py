"""
Unit tests for supplier matching functionality.
"""
import pytest

from models.invoice import ExtractedInvoice, SupplierInfo
from pipeline.supplier_matcher import SupplierMatcher, _normalise_abn, _email_domain


@pytest.mark.unit
class TestSupplierMatcher:
    """Tests for SupplierMatcher class."""

    def test_normalise_abn(self):
        """Test ABN normalization removes non-digits."""
        assert _normalise_abn("12 345 678 901") == "12345678901"
        assert _normalise_abn("12345678901") == "12345678901"
        assert _normalise_abn("") is None
        assert _normalise_abn(None) is None

    def test_email_domain_extraction(self):
        """Test email domain extraction."""
        assert _email_domain("test@example.com") == "example.com"
        assert _email_domain("Test.User@Company.COM") == "company.com"
        assert _email_domain("invalid-email") is None
        assert _email_domain(None) is None

    def test_load_suppliers_from_csv(self, sample_suppliers_csv):
        """Test loading suppliers from CSV."""
        matcher = SupplierMatcher(sample_suppliers_csv)
        
        assert len(matcher.suppliers) == 3
        assert matcher.suppliers[0].id == "SUP-001"
        assert matcher.suppliers[0].name == "Acme Supplies Pty Ltd"
        assert matcher.suppliers[0].abn == "12345678901"

    def test_match_by_abn_exact(self, sample_suppliers_csv):
        """Test exact ABN matching."""
        matcher = SupplierMatcher(sample_suppliers_csv)
        
        invoice = ExtractedInvoice(
            supplier=SupplierInfo(
                name="Some Supplier",
                abn="12 345 678 901"
            )
        )
        
        result = matcher.match(invoice)
        
        assert result is not None
        assert result.supplier_id == "SUP-001"
        assert result.match_method == "abn_exact"
        assert result.confidence == 1.0
        assert result.matched_on["field"] == "abn"

    def test_match_by_name_exact(self, sample_suppliers_csv):
        """Test exact name matching."""
        matcher = SupplierMatcher(sample_suppliers_csv)
        
        invoice = ExtractedInvoice(
            supplier=SupplierInfo(
                name="Global Logistics Ltd"
            )
        )
        
        result = matcher.match(invoice)
        
        assert result is not None
        assert result.supplier_id == "SUP-002"
        assert result.match_method == "name_exact"

    def test_match_by_name_case_insensitive(self, sample_suppliers_csv):
        """Test case-insensitive name matching."""
        matcher = SupplierMatcher(sample_suppliers_csv)
        
        invoice = ExtractedInvoice(
            supplier=SupplierInfo(
                name="ACME SUPPLIES PTY LTD"
            )
        )
        
        result = matcher.match(invoice)
        
        assert result is not None
        assert result.supplier_id == "SUP-001"

    def test_match_by_alias(self, sample_suppliers_csv):
        """Test matching via supplier alias."""
        matcher = SupplierMatcher(sample_suppliers_csv)
        
        invoice = ExtractedInvoice(
            supplier=SupplierInfo(
                name="Global Logistics"  # Alias from CSV
            )
        )
        
        result = matcher.match(invoice)
        
        assert result is not None
        assert result.supplier_id == "SUP-002"

    def test_match_by_email_domain(self, sample_suppliers_csv):
        """Test email domain matching."""
        matcher = SupplierMatcher(sample_suppliers_csv)
        
        invoice = ExtractedInvoice(
            supplier=SupplierInfo(
                name="Unknown Supplier",
                email="billing@acme.com"
            )
        )
        
        result = matcher.match(invoice)
        
        assert result is not None
        assert result.supplier_id == "SUP-001"
        assert result.match_method == "email_domain"

    def test_no_match_found(self, sample_suppliers_csv):
        """Test when no supplier matches."""
        matcher = SupplierMatcher(sample_suppliers_csv)
        
        invoice = ExtractedInvoice(
            supplier=SupplierInfo(
                name="Completely Unknown Supplier",
                abn="99 999 999 999"
            )
        )
        
        result = matcher.match(invoice)
        
        assert result is None

    def test_no_supplier_in_invoice(self, sample_suppliers_csv):
        """Test handling invoice with no supplier info."""
        matcher = SupplierMatcher(sample_suppliers_csv)
        
        invoice = ExtractedInvoice(
            supplier=None
        )
        
        result = matcher.match(invoice)
        
        assert result is None

    def test_empty_suppliers_csv(self, temp_dir):
        """Test with missing/empty suppliers CSV."""
        empty_csv = temp_dir / "empty.csv"
        empty_csv.write_text("id,name,abn,acn,email,phone,address,aliases\n")
        
        matcher = SupplierMatcher(empty_csv)
        
        assert len(matcher.suppliers) == 0
        
        invoice = ExtractedInvoice(
            supplier=SupplierInfo(name="Any Supplier")
        )
        
        result = matcher.match(invoice)
        assert result is None
