"""
OpenAI-compatible structured invoice extraction.

Accepts an ExtractionResult from DoclingExtractor (or PlainTextExtractor) and
asks an LLM to return a structured JSON invoice object.

Works with any OpenAI-compatible backend:
  - Ollama (local):  LLM_BASE_URL=http://host.docker.internal:11434/v1  LLM_API_KEY=ollama
  - OpenAI:          LLM_BASE_URL=https://api.openai.com/v1              LLM_API_KEY=sk-...
  - Groq:            LLM_BASE_URL=https://api.groq.com/openai/v1         LLM_API_KEY=gsk_...
  - Azure OpenAI:    LLM_BASE_URL=https://<resource>.openai.azure.com/   LLM_API_KEY=<key>

When Docling has already extracted line items directly from the table structure,
those are injected into the result and the LLM is told to skip that field --
reducing token usage and improving accuracy for the fields that actually need it
(dates, supplier details, totals, PO numbers, ABN, payment terms, etc.).
"""
import json
import logging
import re
from typing import Optional

from models.invoice import ExtractedInvoice, LineItem
from pipeline.extractor import ExtractionResult
from pipeline.custom_field_extractor import CustomField

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# Used when Docling could NOT parse line items directly
_PROMPT_FULL = """You are an invoice data extraction system. The invoice below has been converted to Markdown from a PDF, preserving table structure. Extract all structured data and return it as valid JSON.

IMPORTANT RULES:
- Return ONLY the JSON object -- no markdown, no explanation, no code fences
- All monetary amounts must be plain numbers (no currency symbols, no commas)
- All dates must be in YYYY-MM-DD format
- ABN format: preserve with spaces as found (e.g. "12 345 678 901")
- Use null for any field not found in the invoice
- tax_rate should be a decimal (e.g. 0.10 for 10% GST, not 10)
- If GST is mentioned without a rate, assume 0.10 (Australian standard)
- The invoice is rendered as Markdown -- pipe tables (| col | col |) contain the line items

Return a JSON object with exactly this structure:
{{
  "invoice_number": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "due_date": "YYYY-MM-DD or null",
  "supplier": {{
    "name": "string or null",
    "abn": "string or null",
    "acn": "string or null",
    "address": "string or null",
    "email": "string or null",
    "phone": "string or null",
    "website": "string or null"
  }},
  "bill_to": {{
    "name": "string or null",
    "address": "string or null",
    "email": "string or null",
    "contact": "string or null"
  }},
  "po_number": "string or null",
  "reference": "string or null",
  "line_items": [
    {{
      "line_number": integer or null,
      "sku": "string or null",
      "description": "string or null",
      "quantity": number or null,
      "unit": "string or null",
      "unit_price": number or null,
      "discount": number or null,
      "total": number or null
    }}
  ],
  "subtotal": number or null,
  "tax_rate": number or null,
  "tax_amount": number or null,
  "shipping": number or null,
  "other_charges": number or null,
  "total": number or null,
  "currency": "AUD",
  "payment_terms": "string or null",
  "bank_details": "string or null",
  "notes": "string or null"
}}

Invoice (Markdown):
---
{invoice_markdown}
---"""


# Used when Docling already extracted line items -- LLM skips that field
_PROMPT_METADATA_ONLY = """You are an invoice data extraction system. The invoice below has been converted to Markdown. The line items have already been extracted directly from the document tables, so you do NOT need to extract them -- set "line_items" to [].

Focus on extracting the header/summary fields: invoice number, dates, supplier details, bill-to, PO number, subtotals, tax, grand total, payment terms, bank details, and notes.

IMPORTANT RULES:
- Return ONLY the JSON object -- no markdown, no explanation, no code fences
- All monetary amounts must be plain numbers (no currency symbols, no commas)
- All dates must be in YYYY-MM-DD format
- ABN format: preserve with spaces as found (e.g. "12 345 678 901")
- Use null for any field not found in the invoice
- tax_rate should be a decimal (e.g. 0.10 for 10% GST, not 10)
- If GST is mentioned without a rate, assume 0.10 (Australian standard)
- Set "line_items" to [] -- do not extract line items

Return a JSON object with exactly this structure:
{{
  "invoice_number": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "due_date": "YYYY-MM-DD or null",
  "supplier": {{
    "name": "string or null",
    "abn": "string or null",
    "acn": "string or null",
    "address": "string or null",
    "email": "string or null",
    "phone": "string or null",
    "website": "string or null"
  }},
  "bill_to": {{
    "name": "string or null",
    "address": "string or null",
    "email": "string or null",
    "contact": "string or null"
  }},
  "po_number": "string or null",
  "reference": "string or null",
  "line_items": [],
  "subtotal": number or null,
  "tax_rate": number or null,
  "tax_amount": number or null,
  "shipping": number or null,
  "other_charges": number or null,
  "total": number or null,
  "currency": "AUD",
  "payment_terms": "string or null",
  "bank_details": "string or null",
  "notes": "string or null"
}}

Invoice (Markdown):
---
{invoice_markdown}
---"""


# ---------------------------------------------------------------------------
# Custom field prompt injection
# ---------------------------------------------------------------------------

def _custom_fields_prompt_addition(fields: list[CustomField]) -> tuple[str, str]:
    """
    Build the two prompt snippets needed to ask the LLM for custom fields.

    Returns (instructions_block, schema_block):
      - instructions_block: bullet list of field hints to insert into the prompt
      - schema_block: JSON schema lines to add inside the return object
    """
    hints = [f for f in fields if f.llm_hint]
    if not hints:
        return "", ""

    bullet_lines = "\n".join(
        f'  - {f.name}: {f.llm_hint}' for f in hints
    )
    instructions = (
        f'\nAlso extract these additional custom fields into the "custom_fields" object:\n'
        f'{bullet_lines}\n'
        f'Use null for any custom field not found in the invoice.\n'
    )

    schema_fields = ",\n    ".join(
        f'"{f.name}": "string or null"' for f in hints
    )
    schema = f',\n  "custom_fields": {{\n    {schema_fields}\n  }}'

    return instructions, schema


# ---------------------------------------------------------------------------
# LLMParser
# ---------------------------------------------------------------------------

class LLMParser:
    """
    Uses any OpenAI-compatible LLM API to extract structured invoice data.

    Defaults to Ollama's OpenAI-compatible endpoint so existing deployments
    continue to work without any changes.  Switch to OpenAI or another backend
    by setting LLM_BASE_URL, LLM_MODEL, and LLM_API_KEY in your environment.

    When pre_extracted_line_items are provided (from TableLineItemExtractor),
    the LLM is given a shorter metadata-only prompt and the line items are
    merged in afterwards -- this improves reliability and cuts token usage.

    Recommended models (in order of preference for this task):
      - gpt-4o-mini    (OpenAI — excellent JSON accuracy, cost-effective)
      - gpt-4o         (OpenAI — best accuracy)
      - qwen2.5:7b     (Ollama — best local JSON accuracy, recommended)
      - llama3.2       (Ollama — good general extraction)
      - mistral        (Ollama — solid alternative)
    """

    def __init__(
        self,
        model: str = "llama3.2",
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "ollama",
    ):
        self.model    = model
        self.base_url = base_url
        self.api_key  = api_key
        self._client  = None

    def _get_client(self):
        """Lazily initialise the OpenAI client."""
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                )
            except ImportError:
                raise RuntimeError(
                    "openai package not installed. Run: pip install openai"
                )
        return self._client

    def parse(
        self,
        extraction: ExtractionResult,
        pre_extracted_line_items: Optional[list[dict]] = None,
        custom_fields: Optional[list[CustomField]] = None,
    ) -> ExtractedInvoice:
        """
        Parse an ExtractionResult into a validated ExtractedInvoice.

        If pre_extracted_line_items is provided, the LLM uses the
        metadata-only prompt and line items are merged in afterwards.

        If custom_fields is provided, hints for fields with llm_hint are
        appended to the prompt so the LLM can attempt to extract them.

        Raises ValueError if the model returns unparseable JSON after retries.
        """
        has_pre_extracted = bool(pre_extracted_line_items)

        # Build custom field prompt additions (empty strings if no hints defined)
        cf_instructions, cf_schema = _custom_fields_prompt_addition(custom_fields or [])

        if has_pre_extracted:
            logger.info(
                "Using metadata-only prompt (%d line items pre-extracted from table)",
                len(pre_extracted_line_items),
            )
            base_prompt = _PROMPT_METADATA_ONLY
        else:
            base_prompt = _PROMPT_FULL

        # Inject custom field snippets BEFORE resolving the format placeholders.
        # The snippets contain literal { } characters (JSON) which must be escaped
        # as {{ }} so that str.format() treats them as literals, not placeholders.
        if cf_instructions:
            escaped_instructions = cf_instructions.replace("{", "{{").replace("}", "}}")
            escaped_schema       = cf_schema.replace("{", "{{").replace("}", "}}")
            # Insert instructions just before "Return a JSON object"
            base_prompt = base_prompt.replace(
                "Return a JSON object with exactly this structure:",
                escaped_instructions + "Return a JSON object with exactly this structure:"
            )
            # Insert schema fields just before the final closing }}
            base_prompt = base_prompt.replace(
                '"notes": "string or null"\n}}',
                f'"notes": "string or null"{escaped_schema}\n}}'
            )

        prompt = base_prompt.format(invoice_markdown=extraction.markdown)

        client = self._get_client()
        invoice: Optional[ExtractedInvoice] = None

        for attempt in range(1, 4):
            logger.debug("LLM extraction attempt %d (model=%s)", attempt, self.model)
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,   # deterministic output
                )
                raw = response.choices[0].message.content.strip()
                invoice = self._parse_json_response(raw)
                if invoice:
                    logger.info("LLM extraction succeeded on attempt %d", attempt)
                    break
            except Exception as e:
                logger.warning("LLM attempt %d failed: %s", attempt, e)
                if attempt == 3:
                    raise

        if invoice is None:
            raise ValueError("LLM failed to return valid invoice JSON after 3 attempts")

        # Merge pre-extracted line items (more accurate than LLM extraction)
        if has_pre_extracted:
            invoice.line_items = [
                LineItem.model_validate(item) for item in pre_extracted_line_items
            ]
            logger.info(
                "Merged %d pre-extracted line items into invoice",
                len(invoice.line_items),
            )

        return invoice

    def _parse_json_response(self, raw: str) -> Optional[ExtractedInvoice]:
        """
        Extract and validate JSON from the model's response.
        Handles markdown code fences and attempts basic JSON repair.
        """
        # Strip code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()

        # Find outermost JSON object
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            logger.warning("No JSON object found in LLM response")
            return None

        json_str = raw[start:end]
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning("JSON decode error: %s", e)
            # Attempt repair: remove trailing commas before } or ]
            json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                logger.error("Could not repair JSON from LLM response")
                return None

        try:
            return ExtractedInvoice.model_validate(data)
        except Exception as e:
            logger.warning("Pydantic validation failed: %s", e)
            # Best-effort: keep only valid top-level fields
            valid_data = {
                k: v for k, v in data.items()
                if k in ExtractedInvoice.model_fields
            }
            try:
                return ExtractedInvoice.model_validate(valid_data)
            except Exception:
                return None

    def check_connection(self) -> dict:
        """
        Verify the LLM endpoint is reachable and the configured model is available.
        """
        try:
            client = self._get_client()
            models_response = client.models.list()
            available = [m.id for m in models_response.data]
            model_available = any(self.model in m for m in available)
            return {
                "ok": True,
                "base_url": self.base_url,
                "model_available": model_available,
                "available_models": available,
            }
        except Exception as e:
            return {
                "ok": False,
                "base_url": self.base_url,
                "error": str(e),
                "model_available": False,
            }
