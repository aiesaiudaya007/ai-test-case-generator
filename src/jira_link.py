"""Parse a Jira *reference* -- either a bare issue key ("PROJ-123") or a full
Jira issue URL ("https://yourcompany.atlassian.net/browse/PROJ-123") -- into
an issue key and (when derivable) a Jira base URL.

Used so every entry point (CLI, the status-change webhook, the generic
/generate endpoint) accepts the same two input shapes instead of each one
re-implementing its own parsing.
"""
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

# Standard Jira issue key shape: one or more uppercase letters/digits
# (starting with a letter), a hyphen, then digits. e.g. PROJ-123, AB2-4501.
ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")

def parse_jira_reference(value: str) -> Tuple[str, Optional[str]]:
    """Returns (issue_key, base_url).

    `base_url` is None when `value` was a bare issue key rather than a URL --
    callers should fall back to their own configured JIRA_BASE_URL in that case.

    Raises ValueError if no issue key can be found in `value`.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("Jira reference is empty.")
    if "://" not in value:
        match = ISSUE_KEY_RE.search(value.upper())
        if not match:
            raise ValueError(f"'{value}' doesn't look like a Jira issue key (expected e.g. PROJ-123).")
        return match.group(0), None

    parsed = urlparse(value)

    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"'{value}' doesn't look like a valid url.")

    match = ISSUE_KEY_RE.search(parsed.path.upper()) or ISSUE_KEY_RE.search(value.upper())
    if not match:
        raise ValueError(f"Could not find a Jira issue key (e.g. PROJ-123) in '{value}'.")

    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return match.group(0), base_url


