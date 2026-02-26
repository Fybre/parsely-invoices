"""
CSV management utilities for loading and saving reference data.

Provides caching and metadata tracking for suppliers, purchase orders,
and other CSV-based reference data used by the pipeline and dashboard.
"""
import csv
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CSVManager:
    """
    Manages CSV file operations with metadata caching.
    
    Caches file metadata (mtime, size, row count) to avoid repeated
    disk operations when checking if files have changed.
    """
    
    def __init__(self):
        self._meta_cache: dict[str, dict] = {}
    
    def clear_cache(self) -> None:
        """Clear the metadata cache."""
        self._meta_cache.clear()
    
    def get_metadata(self, path: Path) -> dict:
        """
        Get metadata for a CSV file (with caching).
        
        Returns:
            dict with keys: exists, mtime, mtime_iso, size, rows
        """
        if not path.exists():
            return {"exists": False, "mtime": None, "size": 0, "rows": 0}
        
        stat = path.stat()
        mtime = stat.st_mtime
        
        # Check cache
        cached = self._meta_cache.get(str(path))
        if cached and cached.get("mtime") == mtime:
            return cached
        
        # Count rows
        rows = 0
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader)  # Skip header
                rows = sum(1 for _ in reader)
        except Exception:
            pass
        
        from datetime import datetime, timezone
        meta = {
            "exists": True,
            "mtime": mtime,
            "mtime_iso": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "size": stat.st_size,
            "rows": rows,
        }
        self._meta_cache[str(path)] = meta
        return meta
    
    def load_dicts(self, path: Path) -> list[dict]:
        """
        Load a CSV file as a list of dictionaries.
        
        Args:
            path: Path to the CSV file
            
        Returns:
            List of row dictionaries (empty if file doesn't exist)
        """
        if not path.exists():
            logger.warning("CSV file not found: %s", path)
            return []
        
        try:
            with open(path, newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except Exception as e:
            logger.error("Failed to load CSV %s: %s", path, e)
            return []
    
    def save_dicts(self, path: Path, rows: list[dict], fieldnames: list[str]) -> None:
        """
        Save a list of dictionaries to a CSV file.
        
        Args:
            path: Path to write the CSV file
            rows: List of row dictionaries
            fieldnames: List of column headers
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        
        # Clear cache for this file since we modified it
        self._meta_cache.pop(str(path), None)
        logger.info("Saved CSV: %s (%d rows)", path, len(rows))
    
    def append_dict(self, path: Path, row: dict, fieldnames: list[str]) -> None:
        """
        Append a single row to a CSV file.
        
        If the file doesn't exist, creates it with the given headers.
        
        Args:
            path: Path to the CSV file
            row: Row dictionary to append
            fieldnames: List of column headers
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = path.exists()
        
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        
        # Clear cache for this file
        self._meta_cache.pop(str(path), None)


# Global instance for shared use
csv_manager = CSVManager()
