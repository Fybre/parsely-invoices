#!/usr/bin/env python3
"""
Invoice Processing Pipeline — CLI entry point.

Usage examples:
  python main.py check                              # Verify setup (Ollama, data files)
  python main.py process invoice.pdf                # Process a single invoice
  python main.py process invoices/                  # Batch-process a folder of PDFs
  python main.py process invoice.pdf --model qwen2.5:7b
  python main.py process invoices/ --output results/

  python main.py watch invoices/                    # Watch folder, process new PDFs
  python main.py watch invoices/ --interval 60      # Poll every 60 seconds
  python main.py watch invoices/ --model qwen2.5:7b # Use a specific model
"""
import json
import logging
import sys
from pathlib import Path

import click

from config import Config, PROJECT_ROOT
from pipeline.processor import InvoiceProcessor


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quieten noisy third-party loggers
    logging.getLogger("pdfminer").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """Invoice Processing Pipeline — extract, match, and validate invoices."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


# --------------------------------------------------------------------
# check command
# --------------------------------------------------------------------

@cli.command()
@click.option("--model", default=None, help="LLM model name to check")
@click.pass_context
def check(ctx: click.Context, model: str | None) -> None:
    """Verify that the LLM backend and all data files are ready."""
    config = Config()
    if model:
        config.llm_model = model

    processor = InvoiceProcessor(config)
    status = processor.check_setup()

    click.echo("\n=== Pipeline Setup Check ===\n")

    # LLM backend
    llm = status["llm"]
    click.echo(f"  LLM endpoint:  {config.llm_base_url}")
    if llm["ok"]:
        model_status = "✓ available" if llm.get("model_available") else "✗ NOT found"
        click.echo(f"  Model '{config.llm_model}':  {model_status}")
        if not llm.get("model_available"):
            available = llm.get("available_models", [])
            if available:
                click.echo(f"  Available models: {', '.join(available[:10])}")
            click.echo(f"  → Check LLM_MODEL matches a model at {config.llm_base_url}")
    else:
        click.echo(f"  LLM backend:   ✗ NOT reachable ({llm.get('error')})")
        click.echo("  → Check LLM_BASE_URL, LLM_API_KEY in your .env")

    click.echo()

    # Data files
    for key, label in [("suppliers_csv", "suppliers.csv"), ("po_csv", "purchase_orders.csv")]:
        info = status[key]
        tick = "✓" if info["exists"] else "✗"
        count_str = f" ({info['count']} loaded)" if info["exists"] else " (file not found)"
        click.echo(f"  {label:<28} {tick}{count_str}")
        if not info["exists"]:
            click.echo(f"     → Expected at: {info['path']}")

    click.echo()
    out = status["output_dir"]
    tick = "✓" if out["exists"] else "✗"
    click.echo(f"  Output directory:             {tick}  {out['path']}")
    click.echo()


# --------------------------------------------------------------------
# process command
# --------------------------------------------------------------------

@cli.command()
@click.argument("target", type=click.Path(exists=True))
@click.option("--model", "-m", default=None, help="Ollama model (default: llama3.2)")
@click.option("--suppliers", default=None, type=click.Path(), help="Path to suppliers CSV")
@click.option("--po-csv", default=None, type=click.Path(), help="Path to purchase_orders CSV")
@click.option("--po-lines-csv", default=None, type=click.Path(), help="Path to purchase_order_lines CSV")
@click.option("--output", "-o", default=None, type=click.Path(), help="Output directory")
@click.option("--no-pretty", is_flag=True, help="Output compact (non-indented) JSON")
@click.pass_context
def process(
    ctx: click.Context,
    target: str,
    model: str | None,
    suppliers: str | None,
    po_csv: str | None,
    po_lines_csv: str | None,
    output: str | None,
    no_pretty: bool,
) -> None:
    """Process a single invoice PDF or a directory of PDFs."""
    config = Config()
    if model:
        config.llm_model = model
    if suppliers:
        config.suppliers_csv = Path(suppliers)
    if po_csv:
        config.po_csv = Path(po_csv)
    if po_lines_csv:
        config.po_lines_csv = Path(po_lines_csv)
    if output:
        config.output_dir = Path(output)
    if no_pretty:
        config.pretty_json = False

    processor = InvoiceProcessor(config)
    target_path = Path(target)

    if target_path.is_dir():
        # Batch mode
        results = processor.process_batch(target_path)
        click.echo(f"\nProcessed {len(results)} invoices.")
        needs_review = [r for r in results if r.requires_review]
        if needs_review:
            click.echo(f"⚠  {len(needs_review)} require review:")
            for r in needs_review:
                fname = Path(r.source_file).name
                click.echo(
                    f"   {fname}  ({r.error_count} errors, {r.warning_count} warnings)"
                )
        click.echo(f"\nResults written to: {config.output_dir}/")
        click.echo(f"Batch summary:      {config.output_dir}/batch_summary.json")

    else:
        # Single file
        if not target_path.suffix.lower() == ".pdf":
            click.echo(f"Error: '{target}' is not a PDF file.", err=True)
            sys.exit(1)

        result = processor.process(target_path)
        out_file = config.output_dir / f"{target_path.stem}.json"

        click.echo()
        click.echo(f"  Invoice:     {result.extracted_invoice.invoice_number or '(unknown)'}")
        click.echo(f"  Date:        {result.extracted_invoice.invoice_date or '(unknown)'}")
        click.echo(f"  Supplier:    {result.extracted_invoice.supplier.name if result.extracted_invoice.supplier else '(unknown)'}")
        click.echo(f"  Total:       {result.extracted_invoice.currency} {result.extracted_invoice.total:.2f}" if result.extracted_invoice.total else "  Total:       (unknown)")
        click.echo(f"  PO:          {result.extracted_invoice.po_number or '(none)'}")
        click.echo(f"  Matched to:  {result.matched_supplier.supplier_name if result.matched_supplier else '(unmatched)'}")
        click.echo()

        if result.discrepancies:
            click.echo(f"  Discrepancies ({len(result.discrepancies)}):")
            for d in result.discrepancies:
                icon = "✗" if d.severity == "error" else ("⚠" if d.severity == "warning" else "ℹ")
                click.echo(f"    {icon} [{d.severity.upper()}] {d.description}")
        else:
            click.echo("  ✓ No discrepancies found")

        click.echo()
        click.echo(f"  Result saved to: {out_file}")


# --------------------------------------------------------------------
# watch command
# --------------------------------------------------------------------

@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option("--model", "-m", default=None, help="Ollama model (default: llama3.2)")
@click.option(
    "--interval", "-i", default=None, type=int,
    help="Seconds between directory scans (default: POLL_INTERVAL env var or 30)",
)
@click.option("--suppliers", default=None, type=click.Path(), help="Path to suppliers CSV")
@click.option("--po-csv", default=None, type=click.Path(), help="Path to purchase_orders CSV")
@click.option("--po-lines-csv", default=None, type=click.Path(), help="Path to purchase_order_lines CSV")
@click.option("--output", "-o", default=None, type=click.Path(), help="Output directory")
@click.option("--no-pretty", is_flag=True, help="Output compact (non-indented) JSON")
@click.pass_context
def watch(
    ctx: click.Context,
    directory: str,
    model: str | None,
    interval: int | None,
    suppliers: str | None,
    po_csv: str | None,
    po_lines_csv: str | None,
    output: str | None,
    no_pretty: bool,
) -> None:
    """
    Watch DIRECTORY and automatically process new PDF invoices as they arrive.

    \b
    State is saved to output/.pipeline_state.json so restarting is safe —
    no invoice is processed twice unless the file itself changes.

    \b
    Examples:
      python main.py watch invoices/
      python main.py watch invoices/ --interval 60 --model qwen2.5:7b
    """
    config = Config()
    if model:
        config.llm_model = model
    if interval is not None:
        config.poll_interval_seconds = interval
    if suppliers:
        config.suppliers_csv = Path(suppliers)
    if po_csv:
        config.po_csv = Path(po_csv)
    if po_lines_csv:
        config.po_lines_csv = Path(po_lines_csv)
    if output:
        config.output_dir = Path(output)
    if no_pretty:
        config.pretty_json = False

    click.echo(
        f"\n  Watching:  {directory}\n"
        f"  Interval:  every {config.poll_interval_seconds}s\n"
        f"  Model:     {config.llm_model}\n"
        f"  Endpoint:  {config.llm_base_url}\n"
        f"  Output:    {config.output_dir}/\n"
        f"  Database:  {config.db_path}\n"
    )
    click.echo("  Press Ctrl-C to stop.\n")

    processor = InvoiceProcessor(config)
    processor.watch_directory(directory, interval=config.poll_interval_seconds)


# --------------------------------------------------------------------
# backup command
# --------------------------------------------------------------------

@cli.command()
@click.argument("destination", type=click.Path(), default="backups")
@click.pass_context
def backup(ctx: click.Context, destination: str) -> None:
    """
    Create a timestamped backup of the database, config, and data files.
    """
    import zipfile
    import sqlite3
    from datetime import datetime
    
    config = Config()
    dest_dir = Path(destination)
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"parsely_backup_{timestamp}.zip"
    zip_path = dest_dir / zip_name
    
    click.echo(f"Creating backup: {zip_path}")
    
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 1. Backup Database (safely)
            if config.db_path.exists():
                click.echo("  + Database (safe copy)...")
                # Create a temporary in-memory backup to avoid locking issues
                temp_db = dest_dir / f"temp_{timestamp}.db"
                try:
                    src_conn = sqlite3.connect(config.db_path)
                    dst_conn = sqlite3.connect(temp_db)
                    with dst_conn:
                        src_conn.backup(dst_conn)
                    src_conn.close()
                    dst_conn.close()
                    zipf.write(temp_db, arcname="output/pipeline.db")
                finally:
                    if temp_db.exists():
                        temp_db.unlink()
            
            # 2. Backup Config
            config_dir = Path(os.getenv("CONFIG_DIR", str(PROJECT_ROOT / "config")))
            if config_dir.exists():
                click.echo("  + Configuration files...")
                for f in config_dir.glob("*"):
                    if f.is_file() and f.suffix != ".bak":
                        zipf.write(f, arcname=f"config/{f.name}")
            
            # 3. Backup Data (CSVs)
            data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))
            if data_dir.exists():
                click.echo("  + Data files (CSVs)...")
                for f in data_dir.glob("*.csv"):
                    zipf.write(f, arcname=f"data/{f.name}")

        click.echo(f"\n✓ Backup successful: {zip_name}")
        
    except Exception as e:
        click.echo(f"\n✗ Backup failed: {e}", err=True)
        if zip_path.exists():
            zip_path.unlink()
        sys.exit(1)


if __name__ == "__main__":
    cli()
