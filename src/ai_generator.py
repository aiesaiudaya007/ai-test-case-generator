"""AI pipeline: Jira Story -> Scenarios -> Test Cases.

Two-stage design (industry-standard for AI test generation, mirrors how a
senior QA engineer works):
  1. Read the story + acceptance criteria and break it into distinct test
     SCENARIOS (functional, negative, edge, boundary, security...).
  2. For each scenario, expand it into concrete, executable TEST CASES with
     steps, test data and an expected result.

Splitting into two calls (rather than one big prompt) keeps each request
focused, makes failures easier to retry/debug, and lets you swap models or
add human review between stages.

Structured output is enforced via Claude's tool-use (forced tool_choice),
not by asking the model to "return JSON" in free text -- this avoids brittle
parsing of prose/markdown fences.
"""
import json
import logging
from typing import List, Protocol

from tenacity import retry, stop_after_attempt, wait_exponential

from .models import Scenario, Story, TestCase

logger = logging.getLogger(__name__)

SCENARIO_TOOL = {
    "name": "record_scenarios",
    "description": "Record the test scenarios extracted from a user story.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Short id, e.g. SC-01"},
                        "title": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": [
                                "Functional", "Negative", "Edge Case",
                                "Boundary", "Security", "Performance", "Usability",
                            ],
                        },
                        "description": {"type": "string"},
                        "related_ac": {
                            "type": "string",
                            "description": "The acceptance criterion this scenario validates, verbatim or paraphrased.",
                        },
                    },
                    "required": ["id", "title", "category", "description"],
                },
            }
        },
        "required": ["scenarios"],
    },
}

TEST_CASE_TOOL = {
    "name": "record_test_cases",
    "description": "Record the detailed, executable test cases for one scenario.",
    "input_schema": {
        "type": "object",
        "properties": {
            "test_cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "test_case_id": {"type": "string", "description": "e.g. TC-01-1"},
                        "title": {"type": "string"},
                        "preconditions": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "test_data": {"type": "string"},
                        "expected_result": {"type": "string"},
                        "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                        "type": {"type": "string", "enum": ["Positive", "Negative", "Edge"]},
                    },
                    "required": ["test_case_id", "title", "steps", "expected_result", "priority", "type"],
                },
            }
        },
        "required": ["test_cases"],
    },
}


class AIGenerator(Protocol):
    def extract_scenarios(self, story: Story) -> List[Scenario]: ...
    def generate_test_cases(self, story: Story, scenario: Scenario) -> List[TestCase]: ...


class ClaudeAIGenerator:
    """Real implementation backed by the Anthropic Messages API."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5-20250929"):
        import anthropic  # imported lazily so --mock runs don't need the package configured

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def extract_scenarios(self, story: Story) -> List[Scenario]:
        prompt = f"""You are a senior QA engineer. Read this Jira story and break it down
into a comprehensive set of test SCENARIOS -- not detailed test cases yet, just the
distinct situations that need coverage. Include functional, negative, edge/boundary,
and (where relevant) security scenarios. Base them on the acceptance criteria first,
then fill gaps the AC doesn't explicitly cover but that a careful QA engineer would test.

Story key: {story.key}
Summary: {story.summary}

Description:
{story.description or "(none)"}

Acceptance Criteria:
{self._format_ac(story.acceptance_criteria)}

Call record_scenarios with the full list."""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            tools=[SCENARIO_TOOL],
            tool_choice={"type": "tool", "name": "record_scenarios"},
            messages=[{"role": "user", "content": prompt}],
        )
        payload = self._extract_tool_input(response, "record_scenarios")
        return [Scenario(**s) for s in payload["scenarios"]]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def generate_test_cases(self, story: Story, scenario: Scenario) -> List[TestCase]:
        prompt = f"""You are a senior QA engineer writing executable test cases for one
                     test scenario derived from a Jira story. Be concrete: steps must be actions a tester
                     can literally follow, with real sample test data, and an unambiguous expected result.
                     Write 1-4 test cases depending on how much the scenario needs (e.g. a "Negative" or
                     "Boundary" scenario often needs a couple of variants with different data).

Story: {story.key} - {story.summary}

Scenario: {scenario.id} - {scenario.title}
Category: {scenario.category}
Details: {scenario.description}
Related AC: {scenario.related_ac or "(n/a)"}

Call record_test_cases with the full list. Prefix test_case_id with '{scenario.id}-'."""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            tools=[TEST_CASE_TOOL],
            tool_choice={"type": "tool", "name": "record_test_cases"},
            messages=[{"role": "user", "content": prompt}],
        )
        payload = self._extract_tool_input(response, "record_test_cases")
        test_cases = []
        for tc in payload["test_cases"]:
            test_cases.append(
                TestCase(
                    **tc,
                    scenario_id=scenario.id,
                    scenario_title=scenario.title,
                )
            )
        return test_cases

    @staticmethod
    def _format_ac(items: List[str]) -> str:
        if not items:
            return "(none provided)"
        return "\n".join(f"- {item}" for item in items)

    @staticmethod
    def _extract_tool_input(response, tool_name: str) -> dict:
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        raise RuntimeError(f"Claude did not call the expected tool '{tool_name}'. Raw response: {response}")


class MockAIGenerator:
    """Deterministic, offline stand-in for ClaudeAIGenerator.

    Useful for: running the pipeline / exporter end-to-end without API
    credentials, unit tests, and CI smoke tests. Swap for ClaudeAIGenerator
    once ANTHROPIC_API_KEY is set -- both implement the same interface.
    """

    def extract_scenarios(self, story: Story) -> List[Scenario]:
        scenarios = []
        criteria = story.acceptance_criteria or [story.summary]
        for i, ac in enumerate(criteria, start=1):
            base_id = f"SC-{i:02d}"
            scenarios.append(
                Scenario(id=base_id, title=f"Verify: {ac}", category="Functional",
                          description=f"Confirm the system behaves as described: {ac}", related_ac=ac)
            )
            scenarios.append(
                Scenario(id=f"{base_id}b", title=f"Negative check for: {ac}", category="Negative",
                          description=f"Confirm the system rejects/handles invalid input related to: {ac}",
                          related_ac=ac)
            )
        return scenarios

    def generate_test_cases(self, story: Story, scenario: Scenario) -> List[TestCase]:
        is_negative = scenario.category == "Negative"
        return [
            TestCase(
                test_case_id=f"{scenario.id}-1",
                title=f"{scenario.title} - primary path",
                preconditions=f"User is logged in and story {story.key} feature is enabled.",
                steps=[
                    "Navigate to the relevant screen/endpoint.",
                    f"Perform the action implied by: {scenario.description}",
                    "Observe the system response.",
                ],
                test_data="sample_input=<valid_value>" if not is_negative else "sample_input=<invalid_value>",
                expected_result=("System rejects the input with a clear error message."
                                  if is_negative else "System completes the action successfully and reflects the change."),
                priority="High" if not is_negative else "Medium",
                type="Negative" if is_negative else "Positive",
                scenario_id=scenario.id,
                scenario_title=scenario.title,
            )
        ]
