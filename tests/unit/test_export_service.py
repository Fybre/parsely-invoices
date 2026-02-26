"""
Unit tests for export service functionality.
"""
import pytest

from dashboard.services.export import (
    apply_corrections,
    build_normalized_supplier,
    build_normalized_line_items,
    render_export_xml,
)


@pytest.mark.unit
class TestApplyCorrections:
    """Tests for apply_corrections function."""

    def test_no_corrections_returns_original(self):
        """Test that no corrections returns the original data."""
        extracted = {"invoice_number": "INV-001", "total": 100}
        result = apply_corrections(extracted, {})
        assert result == extracted

    def test_invoice_field_corrections(self):
        """Test correcting invoice-level fields."""
        extracted = {
            "extracted_invoice": {
                "invoice_number": "INV-001",
                "total": 100
            }
        }
        corrections = {
            "invoice_number": "INV-001-CORRECTED",
            "total": 200
        }
        
        result = apply_corrections(extracted, corrections)
        
        assert result["extracted_invoice"]["invoice_number"] == "INV-001-CORRECTED"
        assert result["extracted_invoice"]["total"] == 200

    def test_supplier_field_corrections(self):
        """Test correcting supplier fields."""
        extracted = {
            "extracted_invoice": {
                "supplier": {
                    "name": "Old Name",
                    "abn": "12 345 678 901"
                }
            }
        }
        corrections = {
            "supplier_name": "New Name",
            "supplier_abn": "98 765 432 109"
        }
        
        result = apply_corrections(extracted, corrections)
        
        assert result["extracted_invoice"]["supplier"]["name"] == "New Name"
        assert result["extracted_invoice"]["supplier"]["abn"] == "98 765 432 109"

    def test_supplier_id_override(self):
        """Test setting corrected supplier ID."""
        extracted = {
            "extracted_invoice": {
                "supplier": {"name": "Acme Inc"}
            },
            "matched_supplier": None
        }
        corrections = {
            "corrected_supplier_id": "SUP-NEW-001"
        }
        
        result = apply_corrections(extracted, corrections)
        
        assert result["matched_supplier"]["supplier_id"] == "SUP-NEW-001"
        assert result["matched_supplier"]["match_method"] == "operator_override"

    def test_line_items_replacement(self):
        """Test replacing line items."""
        extracted = {
            "extracted_invoice": {
                "line_items": [{"description": "Old Item"}]
            }
        }
        new_items = [{"description": "New Item"}]
        corrections = {"line_items": new_items}
        
        result = apply_corrections(extracted, corrections)
        
        assert result["extracted_invoice"]["line_items"] == new_items

    def test_handles_none_extracted_invoice(self):
        """Test handling when extracted_invoice is None."""
        extracted = {"extracted_invoice": None}
        corrections = {"invoice_number": "INV-001"}
        
        result = apply_corrections(extracted, corrections)
        
        assert result["extracted_invoice"]["invoice_number"] == "INV-001"

    def test_handles_none_supplier(self):
        """Test handling when supplier is None."""
        extracted = {
            "extracted_invoice": {"supplier": None}
        }
        corrections = {"supplier_name": "New Supplier"}
        
        result = apply_corrections(extracted, corrections)
        
        assert result["extracted_invoice"]["supplier"]["name"] == "New Supplier"


@pytest.mark.unit
class TestBuildNormalizedSupplier:
    """Tests for build_normalized_supplier function."""

    def test_corrected_supplier(self):
        """Test building normalized supplier from corrections."""
        data = {
            "extracted_invoice": {
                "supplier": {"name": "Extracted Name"}
            },
            "matched_supplier": None
        }
        corrections = {
            "corrected_supplier_id": "SUP-001",
            "supplier_name": "Corrected Name"
        }
        
        result = build_normalized_supplier(data, corrections)
        
        assert result["id"] == "SUP-001"
        assert result["name"] == "Corrected Name"
        assert result["source"] == "corrected"

    def test_matched_supplier(self):
        """Test building normalized supplier from match."""
        data = {
            "extracted_invoice": {
                "supplier": {"name": "Acme Inc", "abn": "12 345 678 901"}
            },
            "matched_supplier": {
                "supplier_id": "SUP-001",
                "supplier_name": "Acme Supplies",
                "match_method": "abn_exact",
                "confidence": 1.0,
                "abn": "12 345 678 901"
            }
        }
        corrections = {}
        
        result = build_normalized_supplier(data, corrections)
        
        assert result["id"] == "SUP-001"
        assert result["source"] == "matched"
        assert result["match_method"] == "abn_exact"
        assert result["matched_on"]["field"] == "abn"

    def test_extracted_only_supplier(self):
        """Test building normalized supplier when only extracted data available."""
        data = {
            "extracted_invoice": {
                "supplier": {"name": "Unknown Supplier", "abn": "11 222 333 444"}
            },
            "matched_supplier": None
        }
        corrections = {}
        
        result = build_normalized_supplier(data, corrections)
        
        assert result["id"] is None
        assert result["name"] == "Unknown Supplier"
        assert result["source"] == "extracted"


@pytest.mark.unit
class TestBuildNormalizedLineItems:
    """Tests for build_normalized_line_items function."""

    def test_basic_line_items(self):
        """Test basic line item normalization."""
        data = {
            "extracted_invoice": {
                "line_items": [
                    {"description": "Item 1", "quantity": 5, "unit_price": 10}
                ]
            },
            "matched_po": None
        }
        
        result = build_normalized_line_items(data)
        
        assert len(result) == 1
        assert result[0]["description"] == "Item 1"
        assert result[0]["quantity"] == 5
        assert result[0]["po_match"] is None

    def test_line_items_with_po_match(self):
        """Test line items with PO matching info."""
        data = {
            "extracted_invoice": {
                "line_items": [
                    {"description": "Chairs", "quantity": 10}
                ]
            },
            "matched_po": {
                "line_matches": [
                    {
                        "invoice_line_index": 0,
                        "po_line_number": 1,
                        "po_description": "Office Chairs",
                        "matched": True,
                        "match_score": 0.95,
                        "quantity_matches": True,
                        "price_matches": False
                    }
                ]
            }
        }
        
        result = build_normalized_line_items(data)
        
        assert result[0]["po_match"] is not None
        assert result[0]["po_match"]["matched"] is True
        assert result[0]["po_match"]["po_line_number"] == 1


@pytest.mark.unit
class TestRenderExportXML:
    """Tests for render_export_xml function."""

    def test_basic_xml_render(self):
        """Test basic XML template rendering."""
        payload = {
            "stem": "test_invoice",
            "exported_at": "2024-01-15T10:00:00Z",
            "pdf_file": "test_invoice.pdf",
            "corrections_applied": False,
            "operator_notes": "Test note",
            "supplier": {"id": "SUP-001", "name": "Test Supplier"},
            "extracted_invoice": {
                "invoice_number": "INV-001",
                "total": 1100
            }
        }
        
        xml = render_export_xml(payload)
        
        assert "<?xml version=" in xml
        assert "<Stem>test_invoice</Stem>" in xml
        assert "<Total>1100</Total>" in xml

    def test_xml_escaping(self):
        """Test that XML special characters are escaped."""
        payload = {
            "stem": "test",
            "matched_supplier": {"supplier_id": "SUP-001", "supplier_name": "A & B Supplies <Special>"},
            "extracted_invoice": {
                "invoice_number": "INV-001",
                "supplier": {"name": "A & B Supplies <Special>"}
            }
        }
        
        xml = render_export_xml(payload)
        
        # Check that special characters are escaped in the output
        assert "&amp;" in xml or "&lt;" in xml or "&gt;" in xml
        # The supplier name should appear escaped
        assert "<Special>" not in xml  # Raw < should not appear
