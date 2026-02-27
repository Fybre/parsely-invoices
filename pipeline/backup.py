"""
Backup service for the invoice pipeline.
Handles periodic creation and rotation of ZIP archives containing the database, config, and data.
"""
import logging
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from config import PROJECT_ROOT

logger = logging.getLogger(__name__)

class BackupService:
    """
    Manages automated backups and rotation.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.backup_dir = Path(os.getenv("BACKUP_DIR", "backups"))
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self) -> str:
        """
        Create a new timestamped ZIP backup. Returns the filename.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_name = f"parsely_backup_{timestamp}.zip"
        zip_path = self.backup_dir / zip_name
        
        logger.info("Starting automated backup: %s", zip_name)
        
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # 1. Backup Database (safe copy)
                if self.config.db_path.exists():
                    temp_db = self.backup_dir / f"temp_{timestamp}.db"
                    try:
                        src_conn = None
                        dst_conn = None
                        try:
                            src_conn = sqlite3.connect(self.config.db_path)
                            dst_conn = sqlite3.connect(temp_db)
                            src_conn.backup(dst_conn)
                            zipf.write(temp_db, arcname="output/pipeline.db")
                        finally:
                            if src_conn:
                                src_conn.close()
                            if dst_conn:
                                dst_conn.close()
                    finally:
                        if temp_db.exists():
                            temp_db.unlink()
                
                # 2. Backup Config
                config_dir = Path(os.getenv("CONFIG_DIR", str(PROJECT_ROOT / "config")))
                if config_dir.exists():
                    for f in config_dir.glob("*"):
                        if f.is_file() and f.suffix != ".bak" and f.name != "users.json":
                            zipf.write(f, arcname=f"config/{f.name}")
                
                # 3. Backup Data (CSVs)
                data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))
                if data_dir.exists():
                    for f in data_dir.glob("*.csv"):
                        zipf.write(f, arcname=f"data/{f.name}")

            logger.info("Backup completed successfully: %s", zip_name)
            self.rotate_backups()
            return zip_name
            
        except Exception as e:
            logger.error("Backup failed: %s", e)
            if zip_path.exists():
                zip_path.unlink()
            raise

    def rotate_backups(self) -> None:
        """
        Remove old backups, keeping only the last N files.
        """
        retention = self.config.backup_retention_count
        if retention <= 0:
            return

        backups = sorted(
            self.backup_dir.glob("parsely_backup_*.zip"),
            key=os.path.getmtime,
            reverse=True
        )
        
        if len(backups) > retention:
            to_delete = backups[retention:]
            for old_zip in to_delete:
                logger.info("Rotating out old backup: %s", old_zip.name)
                try:
                    old_zip.unlink()
                except Exception as e:
                    logger.warning("Failed to delete old backup %s: %s", old_zip, e)

    def get_last_backup_time(self) -> Optional[datetime]:
        """Return the timestamp of the newest backup file."""
        backups = sorted(self.backup_dir.glob("parsely_backup_*.zip"), key=os.path.getmtime)
        if not backups:
            return None
        return datetime.fromtimestamp(backups[-1].stat().st_mtime)
