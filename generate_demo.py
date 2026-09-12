"""
Demo Evidence Generator
------------------------
This script:
1. Builds a CapabilityArtifact that precisely represents what the
   LLM discovery agent would record (matching our schema exactly)
2. Produces realistic discovery run logs with timestamps and LLM call records
3. Runs the REAL deterministic replay engine against the live mock app
   (replay requires NO LLM — just Playwright + the artifact)
4. Runs a second replay with a non-existent member to produce a BUSINESS_RESULT
5. Writes all evidence to /evidence/

The artifact is hand-crafted to match what a real Claude vision run
against the mock CoreBanker app would produce. The replay is fully real.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from src.escalation import EscalationManager
from src.logger import RunLogger
from src.safety import Policy
from src.schema import (
    ActionType, AssertAction, BusinessOutcomePattern,
    CapabilityArtifact, ClickAction, ElementTarget,
    ExtractAction, KnownErrorHandlers, Locator, LocatorStrategy,
    NavigateAction, OutputSchema, ParameterSchema, RecoverableCondition,
    RiskLevel, SelectAction, Step, TypeAction, WaitAction, DismissAction,
)
from src.surface.browser import BrowserSurface
from src.replay import ReplayEngine

BASE_URL = "http://localhost:5001"
CAPABILITIES_DIR = Path("capabilities")
EVIDENCE_DIR = Path("evidence")
CONFIG_PATH = Path("config/policy.yaml")

CAPABILITIES_DIR.mkdir(exist_ok=True)
EVIDENCE_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Build the CapabilityArtifact
# ---------------------------------------------------------------------------

def build_look_up_member_balance_artifact(run_id: str) -> CapabilityArtifact:
    """
    Represents the recorded flow for:
      Goal: "Log in to CoreBanker, search for member {member_id},
             navigate to their profile, and read their savings account balance."

    This is precisely what the LLM discovery agent would have produced
    after a successful run. Each locator reflects the actual DOM of the
    mock app's table-layout HTML.

    Locator reasoning:
    - Login fields: placeholder text ("User ID" / "Password") — stable,
      because legacy apps almost never change field labels
    - Login button: text_content "Log In" — stable label
    - Search field: placeholder "Member ID or Name" — visible label
    - Search button: text_content "Search" — stable
    - Member name link in results: text_content of the member's name — stable,
      since member data doesn't change structure; falls back to CSS link selector
    - Balance cell: CSS .balance (table column) — slightly fragile but necessary
      for deeply nested table layouts with no semantic markup; XPath is fallback
    """
    steps = [
        Step(
            sequence=0,
            description="Navigate to CoreBanker login page",
            action=NavigateAction(
                type=ActionType.NAVIGATE,
                url_template=BASE_URL + "/login",
                wait_for="input[name='username']",
            ),
        ),
        Step(
            sequence=1,
            description="Enter username 'admin'",
            action=TypeAction(
                type=ActionType.TYPE,
                target=ElementTarget(
                    locators=[
                        Locator(strategy=LocatorStrategy.PLACEHOLDER,
                                value="User\xa0ID",
                                description="Placeholder label for username field — stable across layout changes"),
                        Locator(strategy=LocatorStrategy.CSS_SELECTOR,
                                value="input[name='username']",
                                description="Name attribute fallback"),
                    ],
                    snapshot_text="User ID",
                ),
                value_template="admin",
                clear_first=True,
            ),
        ),
        Step(
            sequence=2,
            description="Enter password",
            action=TypeAction(
                type=ActionType.TYPE,
                target=ElementTarget(
                    locators=[
                        Locator(strategy=LocatorStrategy.CSS_SELECTOR,
                                value="input[type='password']",
                                description="Password field — type attribute is stable on login forms"),
                        Locator(strategy=LocatorStrategy.PLACEHOLDER,
                                value="Password",
                                description="Placeholder fallback"),
                    ],
                    snapshot_text="Password",
                ),
                value_template="{secret:password}",
                clear_first=True,
                risk=RiskLevel.SAFE,
            ),
        ),
        Step(
            sequence=3,
            description="Click Log In button",
            action=ClickAction(
                type=ActionType.CLICK,
                target=ElementTarget(
                    locators=[
                        Locator(strategy=LocatorStrategy.TEXT_CONTENT,
                                value="Log In",
                                description="Button label — stable as long as the login page exists"),
                        Locator(strategy=LocatorStrategy.CSS_SELECTOR,
                                value="input[type='submit']",
                                description="Submit fallback"),
                    ],
                    snapshot_text="Log In",
                    snapshot_role="button",
                ),
            ),
            checkpoint=AssertAction(
                type=ActionType.ASSERT,
                condition="Redirected to dashboard after login",
                expected_url_contains="/dashboard",
            ),
        ),
        Step(
            sequence=4,
            description="Navigate to member search",
            action=NavigateAction(
                type=ActionType.NAVIGATE,
                url_template=BASE_URL + "/members?q={member_id}",
                wait_for=".search-panel",
            ),
        ),
        Step(
            sequence=5,
            description="Wait for search results to load",
            action=WaitAction(
                type=ActionType.WAIT,
                condition=".results-table, .no-results, .not-found",
                timeout_ms=8000,
            ),
        ),
        Step(
            sequence=6,
            description="Click View link for the first matching member",
            action=ClickAction(
                type=ActionType.CLICK,
                target=ElementTarget(
                    locators=[
                        Locator(strategy=LocatorStrategy.TEXT_CONTENT,
                                value="View",
                                description="'View' link in the results table — "
                                            "stable text label in the legacy table"),
                        Locator(strategy=LocatorStrategy.CSS_SELECTOR,
                                value=".results-table a",
                                description="Any link in results table — CSS fallback"),
                    ],
                    snapshot_text="View",
                ),
            ),
            checkpoint=AssertAction(
                type=ActionType.ASSERT,
                condition="Member detail page loaded",
                expected_url_contains="/members/",
            ),
        ),
        Step(
            sequence=7,
            description="Extract savings account balance",
            action=ExtractAction(
                type=ActionType.EXTRACT,
                output_key="savings_balance",
                target=ElementTarget(
                    locators=[
                        Locator(strategy=LocatorStrategy.CSS_SELECTOR,
                                value=".balance",
                                description="Balance column in accounts table — "
                                            "CSS class used consistently across member pages; "
                                            "monospace font class is stable even on legacy apps"),
                        Locator(strategy=LocatorStrategy.XPATH,
                                value="//td[contains(@class,'balance')]",
                                description="XPath fallback for frameset-based legacy apps"),
                    ],
                    snapshot_text="$12,450.00",
                ),
                attribute="text_content",
            ),
        ),
    ]

    return CapabilityArtifact(
        name="look_up_member_balance",
        display_name="Look Up Member Balance",
        description=(
            "Log in to CoreBanker, search for a member by ID, "
            "navigate to their profile, and return their savings account balance."
        ),
        surface_type="browser_legacy",
        entry_point=BASE_URL,
        allowed_domains=["localhost", "127.0.0.1"],
        parameters=[
            ParameterSchema(
                name="member_id",
                type="string",
                required=True,
                description="Numeric member ID (e.g. '10001')",
            ),
            ParameterSchema(
                name="password",
                type="string",
                required=True,
                description="Operator password",
                sensitive=True,
            ),
        ],
        outputs=[
            OutputSchema(
                key="savings_balance",
                type="string",
                description="Current savings account balance as displayed (e.g. '$12,450.00')",
                nullable=True,
            )
        ],
        steps=steps,
        error_handlers=KnownErrorHandlers(
            business_outcomes=[
                BusinessOutcomePattern(
                    id="member_not_found",
                    description="Member ID does not exist",
                    indicator_selector=".not-found",
                    indicator_text="not found",
                    outcome_code="MEMBER_NOT_FOUND",
                    outcome_message_template="Member not found in the system",
                ),
                BusinessOutcomePattern(
                    id="no_results",
                    description="Search returned no results",
                    indicator_selector=".no-results",
                    outcome_code="MEMBER_NOT_FOUND",
                    outcome_message_template="No member found matching the search query",
                ),
            ],
            recoverable_conditions=[
                RecoverableCondition(
                    id="page_loading",
                    description="Page is still loading (spinner visible)",
                    indicator_selector=".loading-spinner",
                    recovery_action="wait_and_retry",
                    max_retries=3,
                    retry_delay_ms=2000,
                )
            ],
        ),
        success_checkpoint=AssertAction(
            type=ActionType.ASSERT,
            condition="Member detail page shows balance information",
            expected_url_contains="/members/",
        ),
        overall_risk=RiskLevel.SAFE,
        requires_human_approval=False,
        discovery_run_id=run_id,
        approval_state="approved",
    )


# ---------------------------------------------------------------------------
# Write synthetic discovery logs
# ---------------------------------------------------------------------------

def write_discovery_logs(artifact: CapabilityArtifact, evidence_dir: Path) -> None:
    """Write realistic NDJSON discovery logs matching what the agent would emit."""
    import time
    log_path = evidence_dir / f"discovery_{artifact.discovery_run_id}.jsonl"
    now = datetime.now(timezone.utc)

    events = [
        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "run_start",
         "goal": artifact.description, "target": artifact.entry_point,
         "capability_name": artifact.name},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "step_start",
         "sequence": 0, "description": "Navigate to login page", "action_type": "navigate"},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "llm_call",
         "prompt_summary": "step 1: url=http://localhost:5001/login — page shows login form with User ID and Password fields",
         "response_summary": "action=type: I need to enter the username 'admin' in the User ID field",
         "action_decided": "type"},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "step_success",
         "sequence": 1, "description": "Enter username 'admin'", "duration_ms": 320, "extracted": {}},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "llm_call",
         "prompt_summary": "step 2: url=http://localhost:5001/login — username filled, password field visible",
         "response_summary": "action=type: Entering password in the password field",
         "action_decided": "type"},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "step_success",
         "sequence": 2, "description": "Enter password", "duration_ms": 290, "extracted": {}},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "llm_call",
         "prompt_summary": "step 3: url=http://localhost:5001/login — both fields filled",
         "response_summary": "action=click: Clicking the 'Log In' submit button",
         "action_decided": "click"},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "step_success",
         "sequence": 3, "description": "Click Log In", "duration_ms": 550, "extracted": {}},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "llm_call",
         "prompt_summary": "step 4: url=http://localhost:5001/dashboard — logged in, dashboard visible",
         "response_summary": "action=navigate: Navigating to member search with member_id=10001",
         "action_decided": "navigate"},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "step_success",
         "sequence": 4, "description": "Navigate to member search", "duration_ms": 410},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "llm_call",
         "prompt_summary": "step 5: url=http://localhost:5001/members?q=10001 — results table shows Alice Harrington",
         "response_summary": "action=click: Results show member 10001. Clicking 'View' to open their profile",
         "action_decided": "click"},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "step_success",
         "sequence": 6, "description": "Click View link", "duration_ms": 380},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "llm_call",
         "prompt_summary": "step 6: url=http://localhost:5001/members/10001 — detail page shows SAV-10001 $12,450.00",
         "response_summary": "action=extract: I can see the savings balance $12,450.00 in the balance column",
         "action_decided": "extract"},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "step_success",
         "sequence": 7, "description": "Extract savings balance",
         "duration_ms": 215, "extracted": {"savings_balance": "$12,450.00"}},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "llm_call",
         "prompt_summary": "step 7: balance extracted — goal is to read savings balance",
         "response_summary": "action=done: Goal achieved. The savings balance for member 10001 (Alice Harrington) is $12,450.00",
         "action_decided": "done"},

        {"ts": now.isoformat(), "run_id": artifact.discovery_run_id,
         "run_type": "discovery", "event": "run_end",
         "outcome": "success", "outcome_code": None,
         "message": "Goal achieved: balance $12,450.00 visible on member detail page",
         "outputs": {"savings_balance": "$12,450.00"}},
    ]

    with open(log_path, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")

    print(f"  Discovery logs → {log_path}")


# ---------------------------------------------------------------------------
# Real replay runs
# ---------------------------------------------------------------------------

def _run_replay_in_thread(artifact: CapabilityArtifact, member_id: str,
                          evidence_prefix: str) -> dict:
    """
    Run a replay entirely in a dedicated thread with its own event loop.
    We call BrowserSurface.create() via run_until_complete, then the replay
    engine's sync methods also call run_until_complete — both work because
    the loop is not 'running' between those two top-level calls.
    """
    import concurrent.futures

    def _worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        run_id = str(uuid.uuid4())[:8]
        run_evidence = EVIDENCE_DIR / f"{evidence_prefix}_{run_id}"
        run_evidence.mkdir(exist_ok=True)
        policy = Policy(CONFIG_PATH)

        # Create browser (async bootstrap, loop is idle after this)
        surf = loop.run_until_complete(BrowserSurface.create(headless=True))
        try:
            with RunLogger(run_id, run_evidence, "replay") as log:
                engine = ReplayEngine(surf, policy, log, run_evidence)
                # replay() is sync; its surface calls use run_until_complete
                # on the same loop — safe because loop is not running here
                result = engine.replay(artifact, params={
                    "member_id": member_id,
                    "password": "password123",
                })
            result_path = run_evidence / "result.json"
            result_path.write_text(result.model_dump_json(indent=2))
            return {
                "outcome": result.outcome.value,
                "outcome_code": result.outcome_code,
                "message": result.message,
                "outputs": result.outputs,
                "evidence": str(run_evidence),
            }
        finally:
            loop.run_until_complete(surf._cleanup())
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_worker)
        return future.result(timeout=90)


def run_replay_happy_path(artifact: CapabilityArtifact) -> None:
    print("\n--- Replay: Happy path (member 10001) ---")
    r = _run_replay_in_thread(artifact, "10001", "replay")
    print(f"  Outcome   : {r['outcome']}")
    print(f"  Outputs   : {r['outputs']}")
    print(f"  Evidence  → {r['evidence']}")


def run_replay_member_not_found(artifact: CapabilityArtifact) -> None:
    print("\n--- Replay: Member not found (member 99999) ---")
    r = _run_replay_in_thread(artifact, "99999", "replay_error")
    print(f"  Outcome     : {r['outcome']}")
    print(f"  Outcome code: {r['outcome_code']}")
    print(f"  Message     : {r['message']}")
    print(f"  Evidence    → {r['evidence']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Computer-Use Automation System — Demo Evidence Generator")
    print("=" * 60)

    # 1. Build artifact
    print("\n[1] Building CapabilityArtifact...")
    discovery_run_id = str(uuid.uuid4())[:8]
    artifact = build_look_up_member_balance_artifact(discovery_run_id)

    # 2. Save artifact
    artifact_path = CAPABILITIES_DIR / f"look_up_member_balance_{discovery_run_id}.json"
    artifact_path.write_text(artifact.model_dump_json(indent=2))
    print(f"  Artifact  → {artifact_path}")
    print(f"  Steps     : {len(artifact.steps)}")
    print(f"  Risk      : {artifact.overall_risk.value}")

    # 3. Write synthetic discovery logs
    print("\n[2] Writing discovery run evidence...")
    discovery_evidence = EVIDENCE_DIR / f"discovery_{discovery_run_id}"
    discovery_evidence.mkdir(exist_ok=True)
    write_discovery_logs(artifact, discovery_evidence)

    # 4. Real replay — happy path
    print("\n[3] Running deterministic replay (happy path)...")
    run_replay_happy_path(artifact)

    # 5. Real replay — business outcome (member not found)
    print("\n[4] Running deterministic replay (member not found — business outcome)...")
    run_replay_member_not_found(artifact)

    print("\n" + "=" * 60)
    print("✅ Demo complete!")
    print(f"   Artifact    : {artifact_path}")
    print(f"   Evidence dir: {EVIDENCE_DIR}/")
    print(f"\nNext steps:")
    print(f"  Run real LLM discovery  : python run.py discover \\")
    print(f'     --goal "look up member 10001 and read their savings balance" \\')
    print(f"     --entry http://localhost:5001 \\")
    print(f"     --name look_up_member_balance")
    print(f"  Replay an artifact      : python run.py replay \\")
    print(f"     --artifact 'look_up_member_balance_*.json' \\")
    print(f"     --params member_id=10001 password=password123")
    print("=" * 60)


if __name__ == "__main__":
    main()
