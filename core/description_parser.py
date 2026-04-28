"""
core/description_parser.py — Description parsing and formatting utilities.

Handles HTML-encoded job descriptions from various ATS platforms:
  - Unescape HTML entities
  - Convert HTML to plain text (remove tags, preserve readable content)
  - Truncate for display (console/telegram: 100 chars; webhook: full)
"""
import html
import logging
from typing import Optional

from bs4 import BeautifulSoup

logger = logging.getLogger("job_sniper.description_parser")


def parse_html_description(raw_html: Optional[str]) -> Optional[str]:
    """
    Convert HTML-encoded job description to plain text.
    
    Process:
      1. Handle JSON-escaped sequences (e.g., \\/ → /, \\" → ")
      2. Unescape HTML entities (e.g., &nbsp; → space, &lt; → <)
      3. Parse with BeautifulSoup and extract text
      4. Normalize whitespace (collapse multiple spaces, newlines)
    
    Args:
        raw_html: HTML string (may be HTML-entity-encoded or JSON-escaped)
    
    Returns:
        Plain text description, or None if input is empty/invalid
    """
    if not raw_html or not isinstance(raw_html, str):
        return None
    
    try:
        # Step 1: Handle JSON-escaped sequences (e.g., \\/ → /, \\" → ")
        # This can occur if HTML is returned as part of JSON payload
        raw_html = raw_html.replace(r'\"', '"').replace(r'\/', '/')
        
        # Step 2: Unescape HTML entities
        unescaped = html.unescape(raw_html)
        
        # Step 3: Parse HTML and extract text
        soup = BeautifulSoup(unescaped, 'html.parser')
        text = soup.get_text(separator=' ', strip=True)
        
        # Step 4: Normalize whitespace
        # Replace multiple spaces/newlines with single space
        text = ' '.join(text.split())
        
        return text if text else None
    except Exception as e:
        logger.warning(f"Failed to parse HTML description: {e}")
        return None


def truncate_description(description: Optional[str], max_length: int = 100) -> Optional[str]:
    """
    Truncate description to max_length characters, preserving word boundaries when possible.
    
    Args:
        description: Plain text description
        max_length: Maximum characters (default 100 for console/telegram)
    
    Returns:
        Truncated description with "..." suffix, or None if input is None
    """
    if not description:
        return None
    
    if len(description) <= max_length:
        return description
    
    # Truncate at max_length and try to preserve word boundary
    truncated = description[:max_length]
    
    # Find last space within truncated text
    last_space = truncated.rfind(' ')
    if last_space > max_length * 0.8:  # Only break at space if it's reasonably close
        truncated = truncated[:last_space]
    
    return truncated.rstrip() + "..."


def format_for_console(description: Optional[str]) -> Optional[str]:
    """
    Format description for console output (truncated to 100 chars).
    
    Args:
        description: Plain text description
    
    Returns:
        Truncated description suitable for console, or None
    """
    return truncate_description(description, max_length=100)


def format_for_telegram(description: Optional[str]) -> Optional[str]:
    """
    Format description for Telegram (truncated to 100 chars, escaped for Markdown).
    
    Args:
        description: Plain text description
    
    Returns:
        Truncated, Markdown-escaped description, or None
    """
    truncated = truncate_description(description, max_length=100)
    if not truncated:
        return None
    
    # Escape Telegram Markdown special characters
    for ch in r"_*[]()~`>#+-=|{}.!":
        truncated = truncated.replace(ch, f"\\{ch}")
    
    return truncated


def format_for_webhook(description: Optional[str]) -> Optional[str]:
    """
    Format description for webhook (full text, no truncation).
    
    Args:
        description: Plain text description
    
    Returns:
        Full description, or None
    """
    return description
