"""
Email ingestion service for the invoice pipeline.
Connects via IMAP to poll a mailbox and extract PDF attachments into the invoices/ folder.
"""
import email
import imaplib
import logging
import os
import re
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

class EmailIngestService:
    """
    Polls an IMAP mailbox for unread emails with PDF attachments.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.invoices_dir = Path(os.getenv("INVOICES_DIR", "invoices"))
        self.invoices_dir.mkdir(parents=True, exist_ok=True)

    def poll_mailbox(self) -> int:
        """
        Connect to the mailbox and download any new PDF attachments.
        Returns the count of PDFs successfully downloaded.
        """
        if not self.config.email_ingest_enabled:
            return 0
        
        if not self.config.email_imap_host or not self.config.email_imap_user:
            logger.warning("Email ingestion enabled but host/user not configured.")
            return 0

        logger.info("Polling mailbox %s for new invoices...", self.config.email_imap_user)
        
        downloaded_count = 0
        mail = None
        
        try:
            # 1. Connect and Login
            if self.config.email_use_ssl:
                mail = imaplib.IMAP4_SSL(self.config.email_imap_host, self.config.email_imap_port)
            else:
                mail = imaplib.IMAP4(self.config.email_imap_host, self.config.email_imap_port)
            
            mail.login(self.config.email_imap_user, self.config.email_imap_password)
            
            # 2. Select Mailbox
            # Ensure mailbox name is stripped and quoted. Fallback to INBOX if empty.
            mailbox_name = (self.config.email_mailbox or "INBOX").strip()
            if not mailbox_name:
                mailbox_name = "INBOX"
                
            mailbox = f'"{mailbox_name}"'
            status, _ = mail.select(mailbox)
            if status != 'OK':
                raise ValueError(f"Failed to select mailbox {mailbox}")
            
            # 3. Search for messages using configured criteria
            criteria = self.config.email_search_criteria or "UNSEEN"
            
            # Safety check: 'ALL' without a move-to folder will cause infinite loops
            if criteria == "ALL" and not self.config.email_processed_mailbox:
                logger.warning("Email search criteria set to 'ALL' but no Processed Folder configured. Falling back to 'UNSEEN' to avoid loops.")
                criteria = "UNSEEN"

            status, data = mail.search(None, criteria)
            if status != 'OK':
                raise ValueError(f"IMAP search command failed with criteria: {criteria}")
            
            mail_ids = data[0].split()
            if not mail_ids:
                logger.debug("No emails matching criteria '%s' found.", criteria)
                return 0
            
            logger.info("Found %d email(s) matching '%s'. Processing attachments...", len(mail_ids), criteria)
            
            for m_id in mail_ids:
                try:
                    # Fetch the email body
                    status, msg_data = mail.fetch(m_id, '(RFC822)')
                    if status != 'OK':
                        continue
                    
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    # 4. Extract Attachments
                    msg_downloaded = self._extract_attachments(msg)
                    downloaded_count += msg_downloaded
                    
                    # 5. Post-processing action: Move or Mark-as-read
                    dest_folder = self.config.email_processed_mailbox
                    if dest_folder:
                        if msg_downloaded == 0:
                            logger.debug("No PDF found in email ID %s, moving to %s to clear inbox", m_id, dest_folder)
                        
                        # Move to processed folder
                        dest_quoted = f'"{dest_folder.strip()}"'
                        res, _ = mail.copy(m_id, dest_quoted)
                        if res == 'OK':
                            mail.store(m_id, '+FLAGS', r'\Deleted')
                            logger.debug("Moved email ID %s to %s", m_id, dest_quoted)
                        else:
                            logger.error("Failed to move email ID %s to %s", m_id, dest_quoted)
                    else:
                        # Fallback: just mark as seen
                        mail.store(m_id, '+FLAGS', r'\Seen')
                    
                except Exception as e:
                    logger.error("Error processing email ID %s: %s", m_id, e)

            # Expunge deleted messages if we moved any
            if self.config.email_processed_mailbox:
                mail.expunge()
            
            if downloaded_count > 0:
                logger.info("Email poll complete: %d new PDF(s) saved to %s", downloaded_count, self.invoices_dir)
            
            return downloaded_count

        except Exception as e:
            logger.error("IMAP connection/polling failed: %s", e)
            raise
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

    def _extract_attachments(self, msg: Message) -> int:
        """
        Walk through email parts and save any PDF attachments.
        Returns count of PDFs saved from this specific email.
        """
        saved = 0
        for part in msg.walk():
            # Multipart containers don't have payloads
            if part.get_content_maintype() == 'multipart':
                continue
            if part.get('Content-Disposition') is None:
                continue
            
            filename = part.get_filename()
            if not filename:
                continue
            
            # Clean filename and check extension
            if filename.lower().endswith('.pdf'):
                # Sanitize filename: remove special characters, keep it safe
                clean_name = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
                
                # Add timestamp to avoid collisions if multiple emails have same filename
                ts = datetime.now().strftime("%H%M%S")
                save_path = self.invoices_dir / f"email_{ts}_{clean_name}"
                
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        save_path.write_bytes(payload)
                        logger.info("Saved attachment from email: %s", clean_name)
                        saved += 1
                except Exception as e:
                    logger.error("Failed to save attachment %s: %s", filename, e)
        
        return saved
