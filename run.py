#!/usr/bin/env python3
"""
CLI entry point for the Computer-Use Automation System.

Usage:
  python run.py discover  --goal "look up member 10001 and read their savings balance" \\
                          --entry http://localhost:5001 \\
                          --name look_up_member_balance

  python run.py replay    --artifact capabilities/look_up_member_balance_*.json \\
                          --params member_id=10001

  python run.py replay    --artifact capabilities/look_up_member_balance_*.json \\
                          --params member_id=99999          # triggers MEMBER_NOT_FOUND

  python run.py serve-mock                                  # start the bank app
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure src is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

from src.escalation import EscalationManager
from src.logger import RunLogger
from src.safety import Policy
from src.schema import (
    BusinessOutcomePattern, CapabilityArtifact,
    KnownErrorHandlers, ParameterSchema, OutputSchema,
    RecoverableCondition,
)


CAPABILITIES_DIR = Path(__file__).parent / "capabilities"
EVIDENCE_DIR     = Path(__file__).parent / "evidence"
CONFIG_PATH      = Path(__file__).parent / "config" / "policy.yaml"


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

def cmd_discover(args):
    import asyncio
    from src.surface.browser import BrowserSurface
    from src.agent import DiscoveryAgent

    run_id = str(uuid.uuid4())[:8]
    EVIDENCE_DIR.mkdir(exist_ok=True)
    CAPABILITIES_DIR.mkdir(exist_ok=True)
    run_evidence = EVIDENCE_DIR / f"discovery_{run_id}"
    run_evidence.mkdir(exist_ok=True)

    policy = Policy(CONFIG_PATH)

    async def _run():
        surf = await BrowserSurface.create(headless=args.headless)
        try:
            with RunLogger(run_id, run_evidence, "discovery") as log:
                agent = DiscoveryAgent(surf, policy, log, run_evidence)

                parameters = [
                    ParameterSchema(
                        name="member_id",
                        type="string",
                        required=True,
                        description="The numeric member ID to look up",
                    )
                ] if not args.no_params else []

                outputs = [
                    OutputSchema(
                        key="savings_balance",
                        type="string",
                        description="Current savings account balance",
                        nullable=True,
                    )
                ] if not args.no_outputs else []

                artifact = agent.run(
                    goal=args.goal,
                    entry_point=args.entry,
                    capability_name=args.name,
                    capability_display_name=args.name.replace("_", " ").title(),
                    parameters=parameters,
                    outputs=outputs,
                )

                # Add known error handlers for replay robustness
                artifact.error_handlers = KnownErrorHandlers(
                    business_outcomes=[
                        BusinessOutcomePattern(
                            id="member_not_found",
                            description="Member ID does not exist in the system",
                            indicator_selector=".not-found",
                            indicator_text="not found",
                            outcome_code="MEMBER_NOT_FOUND",
                            outcome_message_template="Member not found in the system",
                        )
                    ],
                    recoverable_conditions=[
                        RecoverableCondition(
                            id="page_loading",
                            description="Page is still loading",
                            indicator_selector=".loading-spinner",
                            recovery_action="wait_and_retry",
                            max_retries=3,
                            retry_delay_ms=2000,
                        )
                    ]
                )

                # Save artifact
                artifact_path = CAPABILITIES_DIR / f"{args.name}_{run_id}.json"
                artifact_path.write_text(artifact.model_dump_json(indent=2))

                print(f"\n✅ Discovery complete!")
                print(f"   Artifact: {artifact_path}")
                print(f"   Steps recorded: {len(artifact.steps)}")
                print(f"   Outputs captured: {list(agent._extracted_outputs.keys())}")
                print(f"   Logs: {run_evidence}")
        finally:
            surf.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def cmd_replay(args):
    import asyncio
    from src.surface.browser import BrowserSurface
    from src.replay import ReplayEngine

    # Resolve artifact path (supports glob)
    pattern = args.artifact
    matches = sorted(glob.glob(str(CAPABILITIES_DIR / pattern)))
    if not matches:
        matches = sorted(glob.glob(pattern))
    if not matches:
        print(f"❌ No artifact found matching: {pattern}")
        sys.exit(1)
    artifact_path = Path(matches[-1])  # most recent
    print(f"Using artifact: {artifact_path}")

    artifact = CapabilityArtifact.model_validate_json(artifact_path.read_text())

    # Parse params
    params: dict = {}
    for p in (args.params or []):
        k, _, v = p.partition("=")
        params[k.strip()] = v.strip()

    run_id = str(uuid.uuid4())[:8]
    EVIDENCE_DIR.mkdir(exist_ok=True)
    run_evidence = EVIDENCE_DIR / f"replay_{run_id}"
    run_evidence.mkdir(exist_ok=True)

    policy = Policy(CONFIG_PATH)
    escalation = EscalationManager(run_evidence) if not args.no_escalation else None

    async def _run():
        surf = await BrowserSurface.create(headless=args.headless)
        try:
            with RunLogger(run_id, run_evidence, "replay") as log:
                engine = ReplayEngine(surf, policy, log, run_evidence,
                                      escalation_manager=escalation,
                                      tenant_id=args.tenant_id)
                result = engine.replay(artifact, params=params)

                print(f"\n{'='*50}")
                print(f"Replay Result: {result.outcome.value.upper()}")
                if result.outcome_code:
                    print(f"Outcome Code : {result.outcome_code}")
                print(f"Message      : {result.message}")
                if result.outputs:
                    print(f"Outputs      : {json.dumps(result.outputs, indent=2)}")
                if result.failed_step:
                    print(f"Failed step  : {result.failed_step}")
                    print(f"Expected     : {result.expected}")
                    print(f"Observed     : {result.observed}")
                    if result.failure_screenshot:
                        print(f"Screenshot   : {result.failure_screenshot}")
                print(f"Logs         : {run_evidence}")
                print(f"{'='*50}\n")

                # Save result
                result_path = run_evidence / "result.json"
                result_path.write_text(result.model_dump_json(indent=2))
        finally:
            surf.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# serve-mock
# ---------------------------------------------------------------------------

def cmd_serve_mock(args):
    from mock_app.server import app
    print("Starting mock CoreBanker bank app on http://localhost:5001")
    print("  Credentials: admin / password123")
    print("  Members: 10001 (Alice), 10002 (Bob), 10003 (Carol/suspended)")
    app.run(port=5001, debug=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Computer-Use Automation System — interface.ai assignment"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # discover
    p_disc = sub.add_parser("discover", help="Run LLM-driven discovery")
    p_disc.add_argument("--goal",  required=True,
                        help='Natural language goal, e.g. "look up member 10001 and read their savings balance"')
    p_disc.add_argument("--entry", default="http://localhost:5001",
                        help="Entry point URL")
    p_disc.add_argument("--name",  default="unnamed_capability",
                        help="Capability name slug")
    p_disc.add_argument("--headless", action="store_true", default=True)
    p_disc.add_argument("--no-headless", dest="headless", action="store_false")
    p_disc.add_argument("--no-params",  action="store_true")
    p_disc.add_argument("--no-outputs", action="store_true")

    # replay
    p_rep = sub.add_parser("replay", help="Deterministic replay of a capability artifact")
    p_rep.add_argument("--artifact", required=True,
                       help="Artifact JSON filename or glob (relative to capabilities/)")
    p_rep.add_argument("--params", nargs="*", metavar="KEY=VALUE",
                       help="Input parameters, e.g. member_id=10001")
    p_rep.add_argument("--tenant-id", default=None)
    p_rep.add_argument("--no-escalation", action="store_true")
    p_rep.add_argument("--headless", action="store_true", default=True)
    p_rep.add_argument("--no-headless", dest="headless", action="store_false")

    # serve-mock
    sub.add_parser("serve-mock", help="Start the mock bank app")

    args = parser.parse_args()

    if args.command == "discover":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("❌ ANTHROPIC_API_KEY not set.")
            sys.exit(1)
        cmd_discover(args)
    elif args.command == "replay":
        cmd_replay(args)
    elif args.command == "serve-mock":
        cmd_serve_mock(args)


if __name__ == "__main__":
    main()
