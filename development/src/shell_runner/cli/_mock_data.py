"""Mock data source for UX prototype of shell-runner-review CLI.

This module exposes plausible T3 (template telemetry) candidates for testing
the operator review flow. Data is hardcoded for rapid UX iteration.
"""


def fake_t3_candidates() -> list[dict]:
    """Return ~5 plausible T3 candidates for UX iteration.

    Each candidate dict has keys matching real T3 schema fields:
    - template: command pattern string
    - observed_count: number of times template was executed
    - distinct_agent_count: how many distinct agents used this template
    - denial_count: how many times operator denied promotion
    - days_observed: calendar days template has been in use
    - observed_agents: list of agent names that used it
    - sample_invocations: actual command strings matching this template
    - suggested_category: one of vcs|test|build|search|lint|package
    - first_seen: ISO date string when template was first observed

    Candidates are varied: at least one passes all rules, at least one fails
    each individual rule (count<10, agents<2, denials>0, days<7) so the UI
    can show ready vs blocked states.
    """
    return [
        {
            "template": "git status --short",
            "observed_count": 47,
            "distinct_agent_count": 4,
            "denial_count": 0,
            "days_observed": 12,
            "observed_agents": [
                "focused-quality-resolver",
                "focused-unit-resolver",
                "micro-quality-resolver",
                "focused-ci-analyzer",
            ],
            "sample_invocations": [
                "git status --short",
                "git status --short 2>/dev/null",
                "cd /repo && git status --short",
            ],
            "suggested_category": "vcs",
            "first_seen": "2026-04-21",
        },
        {
            "template": "pixi run -- pytest tests/unit",
            "observed_count": 8,  # BELOW THRESHOLD: count < 10
            "distinct_agent_count": 2,
            "denial_count": 0,
            "days_observed": 9,
            "observed_agents": ["focused-unit-resolver", "comprehensive-test-resolver"],
            "sample_invocations": [
                "pixi run -- pytest tests/unit -v",
                "pixi run -- pytest tests/unit --cov",
            ],
            "suggested_category": "test",
            "first_seen": "2026-04-26",
        },
        {
            "template": "rg --files -t py",
            "observed_count": 23,
            "distinct_agent_count": 1,  # BELOW THRESHOLD: agents < 2
            "denial_count": 0,
            "days_observed": 6,  # BELOW THRESHOLD: days < 7
            "observed_agents": ["focused-code-modifier"],
            "sample_invocations": [
                "rg --files -t py src/",
                "rg --files -t py | grep test",
            ],
            "suggested_category": "search",
            "first_seen": "2026-04-29",
        },
        {
            "template": "ruff check src/ --fix",
            "observed_count": 31,
            "distinct_agent_count": 3,
            "denial_count": 1,  # EXCEEDS THRESHOLD: denials > 0
            "days_observed": 10,
            "observed_agents": [
                "focused-quality-resolver",
                "micro-quality-resolver",
                "focused-unit-resolver",
            ],
            "sample_invocations": [
                "ruff check src/ --fix",
                "ruff check src/ tests/ --fix",
            ],
            "suggested_category": "lint",
            "first_seen": "2026-04-20",
        },
        {
            "template": "mypy src/",
            "observed_count": 42,
            "distinct_agent_count": 5,
            "denial_count": 0,
            "days_observed": 14,
            "observed_agents": [
                "focused-quality-resolver",
                "focused-unit-resolver",
                "comprehensive-test-resolver",
                "micro-quality-resolver",
                "focused-ci-analyzer",
            ],
            "sample_invocations": [
                "mypy src/",
                "mypy src/ || echo 'mypy: warnings found (non-blocking)'",
            ],
            "suggested_category": "lint",
            "first_seen": "2026-04-15",
        },
    ]
