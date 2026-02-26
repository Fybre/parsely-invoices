# Task Summary: Configurable Callback & Therefore Integration

## Completed
- **Generic Callback System**: Implemented a template-driven webhook system that triggers upon invoice export.
- **Callback Service**: Created `pipeline/callback.py` which handles Jinja2 rendering and HTTP POST requests.
- **Configuration**: Added callback settings to `config.py` and `dashboard/app.py` (admin-editable).
- **Templates**: 
    - `config/callback_template.json.j2`: Default generic JSON payload.
    - `config/therefore_template.json.j2`: Specialized template for Therefore DMS `CreateDocument` API.
- **Audit Logging**: Integration with the database audit log to track `callback_triggered` events with response summaries.
- **Verification**: Successfully tested using a mock HTTP server and `test_callback.py`.

## Configuration Options (Admin Dashboard)
- **Callback URL**: Destination REST endpoint.
- **Callback Method**: HTTP method (POST/PUT).
- **Callback Headers**: Custom JSON headers (e.g., for `TenantName` or API keys).
- **Callback Template**: Choose between generic or system-specific templates.
- **Send PDF**: Toggle Base64-encoded PDF embedding in the payload.

## Therefore DMS Integration
To use with Therefore Online:
1. Set **Callback URL** to `https://{tenant}.thereforeonline.com/theservice/v0001/restun/CreateDocument`.
2. Set **Callback Headers** to `{"TenantName": "{tenant}", "Authorization": "Basic {base64_creds}"}`.
3. Set **Callback Template** to `therefore_template.json.j2`.
4. Enable **Send PDF**.
