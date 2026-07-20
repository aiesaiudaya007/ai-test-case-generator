"""Jira Cloud REST API client.

Fetches a story/issue and normalizes it into a `Story` dataclass, including
parsing the Atlassian Document Format (ADF) description into plain text and
pulling out an "Acceptance Criteria" section if present.
"""
import logging
import re
from typing import List, Optional

import requests
from requests.auth import HTTPBasicAuth
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import Story

logger = logging.getLogger(__name__)

# Some Jira instances expose Acceptance Criteria as a dedicated custom field.
# The field id varies per instance/plan; override via JiraClient(ac_field_id=...)
# if you know yours. Otherwise we fall back to parsing the description text.
DEFAULT_AC_FIELD_ID: Optional[str] = None

# Matches a line that is *only* an "Acceptance Criteria" heading, optionally
# prefixed with markdown-style '#'s (added by _walk_adf for ADF heading nodes)
# and an optional trailing colon. Deliberately anchored to the whole line
# (^...$) so it doesn't match the phrase appearing mid-sentence elsewhere.
_AC_HEADING_RE = re.compile(r"^\s*#{0,3}\s*acceptance criteria\s*:?\s*$", re.IGNORECASE)

class JiraClientError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, ac_field_id: Optional[str] = None):
        if not (base_url and email and api_token):
            raise JiraClientError("Jira base_url, email and api_token are all required.")
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(email, api_token)
        self.headers = {"Accept": "application/json"}
        self.ac_field_id = ac_field_id or DEFAULT_AC_FIELD_ID

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get_issue(self, issue_key: str) -> Story:
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        resp = requests.get(url, headers=self.headers, auth=self.auth, timeout=15)
        if resp.status_code == 404:
            raise JiraClientError(f"Issue '{issue_key}' not found (check the key and your permissions).")
        if resp.status_code in (401, 403):
            raise JiraClientError("Jira auth failed. Check JIRA_EMAIL / JIRA_API_TOKEN.")
        resp.raise_for_status()

        fields = resp.json()["fields"]
        description_text = self._adf_to_text(fields.get("description"))

        acceptance_criteria = []
        if self.ac_field_id and fields.get(self.ac_field_id):
            ac_raw = fields[self.ac_field_id]
            ac_text = ac_raw if isinstance(ac_raw, str) else self._adf_to_text(ac_raw)
            acceptance_criteria = self._split_lines(ac_text)
        if not acceptance_criteria:
            acceptance_criteria = self._extract_ac_from_description(description_text)

        return Story(
            key=resp.json()["key"],
            summary=fields.get("summary", ""),
            description=description_text,
            acceptance_criteria=acceptance_criteria,
            issue_type=(fields.get("issuetype") or {}).get("name", ""),
            status=(fields.get("status") or {}).get("name", ""),
            labels=fields.get("labels", []) or [],
        )

    # ---------- ADF (Atlassian Document Format) parsing ----------

    def _adf_to_text(self, node) -> str:
        if not node:
            return ""
        if isinstance(node, str):
            return node
        lines: List[str] = []
        self._walk_adf(node, lines)
        return "\n".join(l for l in lines if l is not None)

    def _walk_adf(self, node: dict, lines: List[str], list_prefix: str = "") -> None:
        node_type = node.get("type")
        content = node.get("content", [])

        if node_type == "text":
            lines.append(node.get("text", ""))
            return

        if node_type == "heading":
            text = self._inline_text(content)
            lines.append(f"\n## {text}")
            return

        if node_type == "paragraph":
            text = self._inline_text(content)
            lines.append(text)
            return

        if node_type in ("bulletList", "orderedList"):
            for item in content:
                item_text = self._inline_text(item.get("content", []))
                lines.append(f"- {item_text}")
            return

        if node_type == "listItem":
            text = self._inline_text(content)
            lines.append(f"{list_prefix}- {text}")
            return

        # Fallback: recurse into children (doc, table, blockquote, etc.)
        for child in content:
            self._walk_adf(child, lines, list_prefix)

    def _inline_text(self, content: list) -> str:
        parts = []
        for node in content:
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            elif node.get("content"):
                parts.append(self._inline_text(node["content"]))
        return "".join(parts).strip()

    # ---------- Acceptance criteria heuristics ----------

    def _extract_ac_from_description(self, description: str) -> List[str]:
        """Look for a line that is exactly an 'Acceptance Criteria' heading and
        pull only the contiguous bullet items directly beneath it.

        Deliberately conservative in two ways that earlier versions weren't:
          1. If no such heading is found at all, this returns an empty list
             rather than falling back to treating the *entire* description as
             a list of acceptance criteria -- narrative prose lines aren't AC.
          2. Once collecting bullets, it stops at the first blank line, the
             next heading, or the first non-bullet paragraph line -- so a
             later, unrelated section (e.g. "Notes", "Out of scope") never
             gets swept into the AC list just because it lacked a heading
             marker.
        """
        if not description:
            return []

        lines = description.splitlines()
        start = None
        for i, raw_line in enumerate(lines):
            if _AC_HEADING_RE.match(raw_line.strip()):
                start = i + 1
                break
        if start is None:
            return []
        items: List[str] = []
        for raw_line in lines[start]:
            line = raw_line.strip()
            if not line:
                if items:
                    break # blank line after we've already collected bullets -> section ended
                continue  # skip blank lines between the heading and the first bullet
            if line.startswith("#"):
                break  # the next heading -> acceptance criteria section ended
            if not line.startswith("_"):
                break  # a non-bullet paragraph right after the heading -- stop rather than guess
            item = line.lstrip("_").strip()
            if item:
                items.append(item)
        return items

    @staticmethod
    def _split_lines(text: str) -> List[str]:
        items = []
        for raw_line in text.splitlines():
            line = raw_line.strip(" -\t")
            if line:
                items.append(line)
        return items

    # ---------- Writing results back to Jira ----------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def add_comment(self, issue_key: str, adf_body: dict) -> None:
        """Post a comment (Atlassian Document Format) to the issue."""
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"
        resp = requests.post(
            url,
            headers={**self.headers, "Content-Type": "application/json"},
            auth=self.auth,
            json={"body": adf_body},
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("Comment added to %s", issue_key)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def add_attachment(self, issue_key: str, file_path: str) -> None:
        """Upload a file (e.g. the generated Excel report) as an attachment."""
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/attachments"
        headers = {"X-Atlassian-Token": "no-check"}  # required by Jira for attachment uploads
        with open(file_path, "rb") as f:
            resp = requests.post(
                url, headers=headers, auth=self.auth,
                files={"file": (file_path.split("/")[-1], f)}, timeout=30,
            )
        resp.raise_for_status()
        logger.info("Attachment '%s' added to %s", file_path, issue_key)


def build_scenarios_adf(scenarios) -> dict:
    """Render a list of Scenario objects as an ADF document for a Jira comment."""
    bullet_items = [
        {
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [{"type": "text", "text": f"[{sc.category}] {sc.title} (related AC: {sc.related_ac or 'n/a'})"}],
            }],
        }
        for sc in scenarios
    ]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "heading", "attrs": {"level": 3},
             "content": [{"type": "text", "text": "AI-Generated Test Scenarios"}]},
            {"type": "bulletList", "content": bullet_items},
            {"type": "paragraph",
             "content": [{"type": "text", "text": "Full test cases attached as an Excel report on this issue."}]},
        ],
    }
