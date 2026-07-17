"""Plain dataclasses shared across the pipeline (Jira story -> scenarios -> test cases)."""
from dataclasses import dataclass, field, asdict
from typing import List


@dataclass
class Story:
    key: str
    summary: str
    description: str
    acceptance_criteria: List[str]
    issue_type: str = ""
    status: str = ""
    labels: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Scenario:
    id: str
    title: str
    category: str  # Functional | Negative | Edge Case | Boundary | Security | Performance | Usability
    description: str
    related_ac: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TestCase:
    test_case_id: str
    title: str
    steps: List[str]
    expected_result: str
    priority: str  # High | Medium | Low
    type: str  # Positive | Negative | Edge
    preconditions: str = ""
    test_data: str = ""
    scenario_id: str = ""
    scenario_title: str = ""

    def as_dict(self) -> dict:
        return asdict(self)
