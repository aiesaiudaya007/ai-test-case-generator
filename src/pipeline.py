"""The core generation step, shared by every entry point (CLI, the
status-change webhook, the generic /generate endpoint).

Deliberately tiny and has no idea where the Story came from (Jira, a local
JSON file, a URL) or where the output goes (Excel file, Jira comment) --
that's each caller's job. This is what keeps `main.py` and `webhook_server.py`
from re-implementing (and slowly drifting apart on) the same two-call AI flow.
"""

from typing import List, Tuple

from .ai_generator import AIGenerator
from .models import Scenario, Story, TestCase

def generate_scenarios_and_test_cases(
        story: Story,
        generator: AIGenerator
) -> Tuple[List[Scenario], List[TestCase]]:
    scenarios = generator.extract_scenarios(story)
    test_cases: List[TestCase] = []
    for scenario in scenarios:
        test_cases.extend(generator.generate_test_cases(story, scenario))
    return scenarios, test_cases

