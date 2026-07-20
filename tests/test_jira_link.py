"""Unit tests for src/jira_link.parse_jira_reference.

Run with: python -m pytest tests/test_jira_link.py -v
   or:    python tests/test_jira_link.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.jira_link import parse_jira_reference


def test_bare_issue_key():
    key, base_url = parse_jira_reference("PROJ-123")
    assert key == "PROJ-123"
    assert base_url is None
    print("PASS: bare issue key ->", key, base_url)


def test_lowercase_issue_key_is_normalized():
    key, base_url = parse_jira_reference("proj-123")
    assert key == "PROJ-123"
    assert base_url is None
    print("PASS: lowercase issue key normalized ->", key)


def test_browse_url():
    key, base_url = parse_jira_reference("https://yourcompany.atlassian.net/browse/PROJ-123")
    assert key == "PROJ-123"
    assert base_url == "https://yourcompany.atlassian.net"
    print("PASS: /browse/ URL ->", key, base_url)


def test_url_with_query_params_and_fragment():
    key, base_url = parse_jira_reference(
        "https://yourcompany.atlassian.net/jira/software/projects/PROJ/boards/1?selectedIssue=PROJ-123#comment"
    )
    assert key == "PROJ-123"
    assert base_url == "https://yourcompany.atlassian.net"
    print("PASS: board URL with query param ->", key, base_url)


def test_url_with_port():
    key, base_url = parse_jira_reference("https://jira.internal.example.com:8443/browse/AB2-4501")
    assert key == "AB2-4501"
    assert base_url == "https://jira.internal.example.com:8443"
    print("PASS: URL with explicit port ->", key, base_url)


def test_garbage_raises():
    try:
        parse_jira_reference("not a jira reference")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        print("PASS: garbage input rejected:", e)


def test_empty_raises():
    try:
        parse_jira_reference("")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        print("PASS: empty input rejected:", e)


if __name__ == "__main__":
    test_bare_issue_key()
    test_lowercase_issue_key_is_normalized()
    test_browse_url()
    test_url_with_query_params_and_fragment()
    test_url_with_port()
    test_garbage_raises()
    test_empty_raises()
    print("\nAll jira_link tests passed.")