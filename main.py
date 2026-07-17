#!/usr/bin/env python3
"""AI Test Case Generator
Jira Story -> AI-extracted Scenarios -> AI-generated Test Cases -> Excel/JSON.

Examples
--------
Live Jira + Claude:
    python main.py --jira-key PROJ-123

Offline demo (no Jira/Anthropic credentials needed):
    python main.py --mock --story-file sample_data/sample_story.json

Live Jira, but mock the AI (useful to sanity-check Jira parsing only):
    python main.py --jira-key PROJ-123 --mock-ai
"""
import argparse
import json
import logging
import sys

from src.ai_generator import ClaudeAIGenerator, MockAIGenerator
from src.config import config
from src.exporter import export_to_excel, export_to_json
from src.jira_client import JiraClient, JiraClientError
from src.models import Story

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")


def load_story(args) -> Story:
    if args.story_file:
        with open(args.story_file) as f:
            data = json.load(f)
        return Story(**data)

    if not args.jira_key:
        sys.exit("Provide --jira-key PROJ-123 (live Jira) or --story-file path.json (offline).")

    if not config.jira_configured():
        sys.exit(
            "Jira is not configured. Set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN "
            "in your .env (see .env.example), or use --story-file for an offline run."
        )
    client = JiraClient(config.jira_base_url, config.jira_email, config.jira_api_token)
    try:
        return client.get_issue(args.jira_key)
    except JiraClientError as e:
        sys.exit(f"Jira error: {e}")


def build_generator(use_mock: bool):
    if use_mock or not config.anthropic_configured():
        if not use_mock:
            logger.warning("ANTHROPIC_API_KEY not set -- falling back to MockAIGenerator.")
        return MockAIGenerator()
    return ClaudeAIGenerator(api_key=config.anthropic_api_key, model=config.claude_model)


def run(args) -> None:
    story = load_story(args)
    logger.info("Loaded story %s: %s", story.key, story.summary)

    generator = build_generator(args.mock or args.mock_ai)

    logger.info("Extracting scenarios...")
    scenarios = generator.extract_scenarios(story)
    logger.info("Extracted %d scenarios.", len(scenarios))

    all_test_cases = []
    for scenario in scenarios:
        logger.info("Generating test cases for %s: %s", scenario.id, scenario.title)
        test_cases = generator.generate_test_cases(story, scenario)
        all_test_cases.extend(test_cases)
    logger.info("Generated %d test cases total.", len(all_test_cases))

    xlsx_path = f"{args.output}.xlsx"
    json_path = f"{args.output}.json"
    export_to_excel(story, scenarios, all_test_cases, xlsx_path)
    export_to_json(story, scenarios, all_test_cases, json_path)

    print(f"\nDone. Wrote:\n  {xlsx_path}\n  {json_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate test cases from a Jira story using Claude.")
    parser.add_argument("--jira-key", help="Jira issue key, e.g. PROJ-123 (requires .env Jira credentials)")
    parser.add_argument("--story-file", help="Path to a local JSON file matching the Story schema (offline mode)")
    parser.add_argument("--mock", action="store_true", help="Use MockAIGenerator instead of the real Claude API")
    parser.add_argument("--mock-ai", action="store_true", help="Alias for --mock")
    parser.add_argument("--output", default="output/test_cases", help="Output file prefix (no extension)")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
