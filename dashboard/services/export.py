"""
Export service for invoice data normalization and formatting.
"""
import copy
from pathlib import Path
from jinja2 import BaseLoader, Environment, FileSystemLoader

# Default XML export template
DEFAULT_EXPORT_XML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!--
  Invoice export template — edit config/export_template.xml.j2 to customise.
  Template engine : Jinja2  (https://jinja.palletsprojects.com/)
  Values are XML-escaped automatically.  Use | safe only for trusted markup.

  Top-level variables available in every export:
    stem                 — invoice filename stem (no extension)
    exported_at          — ISO-8601 UTC timestamp
    pdf_file             — relative filename of the accompanying PDF
    corrections_applied  — true | false
    operator_notes       — free-text note, may be empty
    matched_supplier     — dict: supplier_id, supplier_name, match_method, confidence
    extracted_invoice    — dict: invoice fields, supplier sub-dict, line_items list
    matched_pos          — list of matched purchase orders
-->
<Invoice>
  <Meta>
    <Stem>{{ stem }}</Stem>
    <ExportedAt>{{ exported_at }}</ExportedAt>
    <PdfFile>{{ pdf_file }}</PdfFile>
    <CorrectionsApplied>{{ corrections_applied | string | lower }}</CorrectionsApplied>
    {% if operator_notes %}<OperatorNotes>{{ operator_notes }}</OperatorNotes>
    {% endif %}
  </Meta>

  {% set ms  = matched_supplier or {} %}
  {% set inv = extracted_invoice or {} %}
  {% set s   = inv.supplier or {} %}
  <Supplier>
    {% if ms.supplier_id %}<Id>{{ ms.supplier_id }}</Id>
    {% endif %}
    <Name>{{ ms.supplier_name or s.name or '' }}</Name>
    {% if s.abn %}<ABN>{{ s.abn }}</ABN>
    {% endif %}
    {% if s.acn %}<ACN>{{ s.acn }}</ACN>
    {% endif %}
    {% if s.email %}<Email>{{ s.email }}</Email>
    {% endif %}
    {% if s.phone %}<Phone>{{ s.phone }}</Phone>
    {% endif %}
    {% if s.address %}<Address>{{ s.address }}</Address>
    {% endif %}
    {% if ms.match_method %}<MatchMethod>{{ ms.match_method }}</MatchMethod>
    {% endif %}
  </Supplier>

  <InvoiceDetails>
    {% if inv.invoice_number %}<InvoiceNumber>{{ inv.invoice_number }}</InvoiceNumber>
    {% endif %}
    {% if inv.invoice_date %}<InvoiceDate>{{ inv.invoice_date }}</InvoiceDate>
    {% endif %}
    {% if inv.due_date %}<DueDate>{{ inv.due_date }}</DueDate>
    {% endif %}
    {% if inv.po_number %}<PONumber>{{ inv.po_number }}</PONumber>
    {% endif %}
    {% if inv.currency %}<Currency>{{ inv.currency }}</Currency>
    {% endif %}
    {% if inv.subtotal is not none %}<Subtotal>{{ inv.subtotal }}</Subtotal>
    {% endif %}
    {% if inv.tax_amount is not none %}<TaxAmount>{{ inv.tax_amount }}</TaxAmount>
    {% endif %}
    {% if inv.total is not none %}<Total>{{ inv.total }}</Total>
    {% endif %}
  </InvoiceDetails>

  {% if inv.line_items %}
  <LineItems>
    {% for item in inv.line_items %}
    <LineItem number="{{ item.line_number | default(loop.index) }}">
      {% if item.description %}<Description>{{ item.description }}</Description>
      {% endif %}
      {% if item.sku %}<SKU>{{ item.sku }}</SKU>
      {% endif %}
      {% if item.quantity is not none %}<Quantity>{{ item.quantity }}</Quantity>
      {% endif %}
      {% if item.unit_price is not none %}<UnitPrice>{{ item.unit_price }}</UnitPrice>
      {% endif %}
      {% if item.line_total is not none %}<LineTotal>{{ item.line_total }}</LineTotal>
      {% endif %}
    </LineItem>
    {% endfor %}
  </LineItems>
  {% endif %}

  {% if inv.custom_fields %}
  <CustomFields>
    {% for key, value in inv.custom_fields.items() %}
    <Field name="{{ key }}">{{ value }}</Field>
    {% endfor %}
  </CustomFields>
  {% endif %}

</Invoice>
"""


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


def render_export_xml(payload: dict, template_file: Path | None = None) -> str:
    """
    Render *payload* as XML using the operator template (or built-in default).
    
    Args:
        payload: The export data dictionary
        template_file: Optional path to custom Jinja2 template file
    """
    if template_file and template_file.exists():
        env = Environment(
            loader=FileSystemLoader(str(template_file.parent)),
            autoescape=True,
            keep_trailing_newline=True,
        )
        tmpl = env.get_template(template_file.name)
    else:
        env = Environment(loader=BaseLoader(), autoescape=True, keep_trailing_newline=True)
        tmpl = env.from_string(DEFAULT_EXPORT_XML_TEMPLATE)
    return tmpl.render(**payload)
