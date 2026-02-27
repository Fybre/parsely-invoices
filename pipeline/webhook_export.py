"""
Configurable webhook export service for the invoice pipeline.

Triggered upon invoice approval/export to push data to external systems
(e.g., Therefore DMS, ERP, or custom APIs) via a templated JSON payload.
"""
import base64
import json
import logging
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional

from jinja2 import FileSystemLoader, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

logger = logging.getLogger(__name__)

class WebhookExportService:
    """
    Sends a templated JSON payload to a configured URL when an invoice is exported.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.config_dir = Path(os.getenv("CONFIG_DIR", str(Path(__file__).parent.parent / "config")))
        
        # Initialize Jinja2 environment for template rendering
        self.jinja_env = SandboxedEnvironment(
            loader=FileSystemLoader(str(self.config_dir)),
            autoescape=select_autoescape(['json', 'xml']),
            keep_trailing_newline=True
        )

    def render_payload(self, export_payload: Dict[str, Any], pdf_path: Optional[Path] = None) -> str:
        """
        Render the webhook export payload using the configured Jinja2 template.
        """
        template_name = self.config.webhook_export_template
        try:
            template = self.jinja_env.get_template(template_name)
        except Exception as e:
            logger.error("Webhook export template not found: %s (%s)", template_name, e)
            raise ValueError(f"Webhook export template '{template_name}' not found in {self.config_dir}")

        # Prepare context for the template
        context = {**export_payload}
        
        # Add Base64-encoded PDF if enabled and file exists
        if self.config.webhook_export_enable_pdf and pdf_path and pdf_path.exists():
            try:
                pdf_bytes = pdf_path.read_bytes()
                context['pdf_base64'] = base64.b64encode(pdf_bytes).decode('utf-8')
                context['pdf_size_bytes'] = len(pdf_bytes)
            except Exception as e:
                logger.warning("Failed to read PDF for webhook export: %s", e)
                context['pdf_base64'] = None
        else:
            context['pdf_base64'] = None

        return template.render(**context)

    def send_webhook_export(self, stem: str, export_payload: Dict[str, Any], pdf_path: Optional[Path] = None) -> Dict[str, Any]:
        """
        Execute the webhook export for a specific invoice.
        
        Returns a dict describing the result (success, status_code, response, etc.)
        suitable for storing in the audit log.
        """
        url = self.config.webhook_export_url
        if not url:
            return {"status": "skipped", "reason": "WEBHOOK_EXPORT_URL not configured"}

        try:
            payload_str = self.render_payload(export_payload, pdf_path)
            # Ensure the rendered payload is valid JSON (if it's supposed to be)
            # This also handles escaping/formatting from the template
            payload_data = payload_str.encode('utf-8')
        except Exception as e:
            logger.error("Failed to render webhook export payload for %s: %s", stem, e)
            return {"status": "failed", "error": f"Template rendering failed: {str(e)}"}

        # Build request
        req = urllib.request.Request(
            url,
            data=payload_data,
            method=self.config.webhook_export_method.upper()
        )
        
        # Add default headers
        req.add_header('Content-Type', 'application/json; charset=utf-8')
        req.add_header('User-Agent', 'Parsely-Invoices-Webhook-Export/1.0')

        # Add custom headers from config
        if self.config.webhook_export_headers_json:
            try:
                custom_headers_dict = json.loads(self.config.webhook_export_headers_json)
                for k, v in custom_headers_dict.items():
                    req.add_header(k, str(v))
            except Exception as e:
                logger.warning("Failed to parse WEBHOOK_EXPORT_HEADERS: %s", e)

        # Execute request
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                status_code = response.getcode()
                resp_body = response.read().decode('utf-8', errors='replace')
                
                logger.info("Webhook export sent for %s: HTTP %d", stem, status_code)
                return {
                    "status": "success",
                    "status_code": status_code,
                    "response_summary": resp_body[:200]  # Store first 200 chars for audit
                }
        except urllib.error.HTTPError as e:
            resp_body = e.read().decode('utf-8', errors='replace') if e.fp else str(e)
            logger.error("Webhook export failed for %s: HTTP %d - %s", stem, e.code, resp_body)
            return {
                "status": "failed",
                "status_code": e.code,
                "error": resp_body[:500]
            }
        except Exception as e:
            logger.error("Webhook export error for %s: %s", stem, e)
            return {
                "status": "failed",
                "error": str(e)
            }
