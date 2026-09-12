"""
Goal-Driven Discovery Agent
---------------------------
Implements the observe → decide → act loop.

The LLM sees:
  - Current screenshot (vision)
  - Current accessibility tree (text)
  - Current URL
  - Goal
  - Action history (last N steps)

And responds with a structured JSON action to take next, plus reasoning.

After a successful run the agent emits a CapabilityArtifact capturing
the full recorded flow.

Design decisions:
- Claude claude-3-5-sonnet with vision: screenshot + accessibility tree
  together give the best signal on legacy surfaces (screenshot for visual
  layout, AX tree for semantic labels and form fields)
- Structured output via JSON mode: action is always a typed dict, never free text
- Max steps hard limit: prevents infinite loops
- "Done" signal: LLM returns action_type = "done" when goal is achieved
- Recording: every successful action is appended to a Step list; the
  final artifact is emitted from that list
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic

from .logger import RunLogger
from .safety import Policy, PolicyViolation, SafetyGuard
from .schema import (
    ActionType, AssertAction, CapabilityArtifact, ClickAction,
    ElementTarget, KnownErrorHandlers, Locator, LocatorStrategy,
    NavigateAction, OutputSchema, ParameterSchema, RiskLevel,
    Step, StepAction, SurfaceType, TypeAction, ExtractAction,
    WaitAction, SelectAction
)
from .surface.base import Surface


# ---------------------------------------------------------------------------
# System prompt for the discovery agent
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a computer-use automation agent for bank back-office software.
Your job is to accomplish a stated goal by operating a web application.

At each step you receive:
- The current page URL
- A screenshot of the current page (vision)
- The accessibility tree of the page (text)
- Your action history so far

You must respond with a JSON object describing the NEXT single action to take.

Action types and their JSON schemas:

navigate:   {"action_type": "navigate", "url": "...", "reasoning": "..."}
click:      {"action_type": "click", "target_description": "...", "aria_label": "...",
             "text_content": "...", "css_hint": "...", "reasoning": "..."}
type:       {"action_type": "type", "target_description": "...", "placeholder": "...",
             "aria_label": "...", "text_content": "...", "value": "...",
             "clear_first": true, "reasoning": "..."}
select:     {"action_type": "select", "target_description": "...", "css_hint": "...",
             "option": "...", "reasoning": "..."}
wait:       {"action_type": "wait", "condition": "css_selector_or_network_idle",
             "timeout_ms": 5000, "reasoning": "..."}
extract:    {"action_type": "extract", "output_key": "...", "target_description": "...",
             "attribute": "text_content", "css_hint": "...", "reasoning": "..."}
done:       {"action_type": "done", "success": true,
             "success_indicator": "what text/element confirms success",
             "reasoning": "..."}
stuck:      {"action_type": "stuck", "reason": "...", "reasoning": "..."}

Rules:
- Always return exactly ONE action per response, as a JSON object.
- Prefer aria_label and text_content over CSS selectors — they are more stable.
- If you must use CSS, prefer role-based selectors (input[type=text]) over class/id.
- If you see a confirmation dialog, you must click the confirm button to proceed.
- "done" means the GOAL is achieved. Include what you see that confirms it.
- "stuck" means you genuinely cannot proceed. Be specific about why.
- Never invent data. Only use values from the goal or the page.
- Never navigate outside the allowed domain.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class DiscoveryAgent:
    """
    Runs an LLM-driven discovery session and records a CapabilityArtifact.
    """

    def __init__(self, surface: Surface, policy: Policy,
                 run_logger: RunLogger, evidence_dir: Path):
        self.surface = surface
        self.policy = policy
        self.guard = SafetyGuard(policy)
        self.log = run_logger
        self.evidence_dir = evidence_dir
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._recorded_steps: List[Step] = []
        self._extracted_outputs: Dict[str, str] = {}
        self._action_history: List[Dict] = []

    def run(self, goal: str, entry_point: str,
            capability_name: str, capability_display_name: str,
            parameters: Optional[List[ParameterSchema]] = None,
            outputs: Optional[List[OutputSchema]] = None) -> CapabilityArtifact:
        """
        Execute the discovery loop. Returns a CapabilityArtifact on success.
        Raises on failure or if stuck.
        """
        run_id = self.log.run_id
        self.log.run_start(goal=goal, target=entry_point,
                           capability_name=capability_name)

        # Navigate to entry point
        self.guard.check_url(entry_point)
        self.surface.navigate(entry_point)
        self._record_navigate(entry_point, sequence=0)

        step_seq = 1
        max_steps = self.policy.max_steps

        while step_seq <= max_steps:
            # ---- observe ----
            url     = self.surface.current_url()
            ax_tree = self.surface.page_accessibility_tree()
            ss_b64  = self.surface.page_screenshot_b64()

            # Save screenshot to evidence
            ss_path = self.evidence_dir / f"step_{step_seq:03d}.png"
            import base64
            ss_path.write_bytes(base64.b64decode(ss_b64))

            # ---- decide ----
            action_json = self._llm_decide(goal, url, ax_tree, ss_b64)
            action_type = action_json.get("action_type", "")
            reasoning   = action_json.get("reasoning", "")

            self.log.llm_call(
                prompt_summary=f"step {step_seq}: url={url[:80]}",
                response_summary=f"action={action_type}: {reasoning[:200]}",
                action_decided=action_type,
            )

            # ---- terminal states ----
            if action_type == "done":
                indicator = action_json.get("success_indicator", "")
                self.log.run_end(
                    outcome="success", message=f"Goal achieved: {indicator}",
                    outputs=self._extracted_outputs
                )
                return self._build_artifact(
                    capability_name=capability_name,
                    capability_display_name=capability_display_name,
                    description=goal,
                    entry_point=entry_point,
                    parameters=parameters or [],
                    outputs=outputs or [],
                    run_id=run_id,
                    success_indicator=indicator,
                )

            if action_type == "stuck":
                reason = action_json.get("reason", "Agent reported stuck")
                self.log.run_end(outcome="hard_failure", message=reason)
                raise RuntimeError(f"Discovery agent stuck: {reason}")

            # ---- act ----
            try:
                self._execute_action(action_json, step_seq)
            except PolicyViolation as e:
                self.log.safety_violation(str(e))
                raise
            except Exception as e:
                ss_fail = self.evidence_dir / f"failure_step_{step_seq:03d}.png"
                self.surface.screenshot(ss_fail)
                self.log.step_failure(step_seq, action_type, str(e),
                                      screenshot_path=str(ss_fail))
                raise

            self._action_history.append(action_json)
            step_seq += 1

        raise RuntimeError(f"Discovery exceeded max_steps={max_steps}")

    # ---- LLM call ----

    def _llm_decide(self, goal: str, url: str, ax_tree: str,
                    ss_b64: str) -> Dict[str, Any]:
        history_text = json.dumps(self._action_history[-8:], indent=2)

        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": ss_b64,
                },
            },
            {
                "type": "text",
                "text": (
                    f"GOAL: {goal}\n\n"
                    f"CURRENT URL: {url}\n\n"
                    f"ACCESSIBILITY TREE (truncated to 3000 chars):\n"
                    f"{ax_tree[:3000]}\n\n"
                    f"ACTION HISTORY (last 8 steps):\n{history_text}\n\n"
                    "What is the next single action to take? Respond with JSON only."
                ),
            },
        ]

        response = self.client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )

        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        return json.loads(raw)

    # ---- action execution ----

    def _execute_action(self, action: Dict[str, Any], seq: int) -> None:
        action_type = action["action_type"]
        desc = action.get("reasoning", action_type)

        self.log.step_start(seq, desc, action_type)

        if action_type == "navigate":
            url = action["url"]
            self.guard.check_url(url)
            self.surface.navigate(url)
            self._record_navigate(url, seq)

        elif action_type == "click":
            target = self._build_target(action)
            step_action = ClickAction(type=ActionType.CLICK, target=target)
            self.guard.preflight(step_action, desc)
            self.surface.click(target)
            self._recorded_steps.append(Step(
                sequence=seq, description=desc, action=step_action
            ))

        elif action_type == "type":
            target = self._build_target(action)
            value  = action.get("value", "")
            clear  = action.get("clear_first", True)
            step_action = TypeAction(
                type=ActionType.TYPE, target=target,
                value_template=value, clear_first=clear
            )
            self.guard.preflight(step_action, desc)
            self.surface.type_text(target, value, clear_first=clear)
            self._recorded_steps.append(Step(
                sequence=seq, description=desc, action=step_action
            ))

        elif action_type == "select":
            target = self._build_target(action)
            option = action.get("option", "")
            step_action = SelectAction(
                type=ActionType.SELECT, target=target, option_template=option
            )
            self.guard.preflight(step_action, desc)
            self.surface.select_option(target, option)
            self._recorded_steps.append(Step(
                sequence=seq, description=desc, action=step_action
            ))

        elif action_type == "wait":
            condition  = action.get("condition", "network_idle")
            timeout_ms = action.get("timeout_ms", 5000)
            step_action = WaitAction(
                type=ActionType.WAIT, condition=condition, timeout_ms=timeout_ms
            )
            self.surface.wait_for(condition, timeout_ms)
            self._recorded_steps.append(Step(
                sequence=seq, description=desc, action=step_action
            ))

        elif action_type == "extract":
            target    = self._build_target(action)
            out_key   = action.get("output_key", f"output_{seq}")
            attribute = action.get("attribute", "text_content")
            step_action = ExtractAction(
                type=ActionType.EXTRACT, output_key=out_key,
                target=target, attribute=attribute
            )
            value = self.surface.extract_text(target, attribute)
            self._extracted_outputs[out_key] = value
            self._recorded_steps.append(Step(
                sequence=seq, description=desc, action=step_action
            ))
            self.log.step_success(seq, desc, extracted={out_key: value})
            return  # early return, no double-log

        else:
            raise ValueError(f"Unknown action type from LLM: {action_type}")

        self.log.step_success(seq, desc)

    # ---- target builder from LLM output ----

    def _build_target(self, action: Dict[str, Any]) -> ElementTarget:
        """
        Construct an ElementTarget from the LLM's natural-language description.
        Priority: aria_label → text_content → placeholder → css_hint
        """
        locators: List[Locator] = []

        if action.get("aria_label"):
            locators.append(Locator(
                strategy=LocatorStrategy.ARIA_LABEL,
                value=action["aria_label"],
                description="ARIA label — stable across layout changes"
            ))
        if action.get("text_content"):
            locators.append(Locator(
                strategy=LocatorStrategy.TEXT_CONTENT,
                value=action["text_content"],
                description="Visible text content"
            ))
        if action.get("placeholder"):
            locators.append(Locator(
                strategy=LocatorStrategy.PLACEHOLDER,
                value=action["placeholder"],
                description="Input placeholder text"
            ))
        if action.get("css_hint"):
            locators.append(Locator(
                strategy=LocatorStrategy.CSS_SELECTOR,
                value=action["css_hint"],
                description="CSS selector — fragile fallback for legacy surfaces"
            ))

        if not locators:
            raise ValueError(f"No locator info in action: {action}")

        return ElementTarget(
            locators=locators,
            snapshot_text=action.get("text_content"),
        )

    # ---- step recording helpers ----

    def _record_navigate(self, url: str, sequence: int) -> None:
        self._recorded_steps.append(Step(
            sequence=sequence,
            description=f"Navigate to {url}",
            action=NavigateAction(
                type=ActionType.NAVIGATE,
                url_template=url,
            )
        ))

    # ---- artifact builder ----

    def _build_artifact(
        self,
        capability_name: str,
        capability_display_name: str,
        description: str,
        entry_point: str,
        parameters: List[ParameterSchema],
        outputs: List[OutputSchema],
        run_id: str,
        success_indicator: str,
    ) -> CapabilityArtifact:
        from urllib.parse import urlparse
        domain = urlparse(entry_point).hostname or "localhost"

        # Auto-extract output schemas from what was actually extracted
        if not outputs and self._extracted_outputs:
            outputs = [
                OutputSchema(key=k, type="string",
                             description=f"Extracted: {k}")
                for k in self._extracted_outputs
            ]

        success_checkpoint = AssertAction(
            type=ActionType.ASSERT,
            condition=f"Page confirms: {success_indicator}",
            expected_text=success_indicator if len(success_indicator) < 100 else None,
        )

        return CapabilityArtifact(
            name=capability_name,
            display_name=capability_display_name,
            description=description,
            entry_point=entry_point,
            allowed_domains=[domain, "localhost"],
            parameters=parameters,
            outputs=outputs,
            steps=self._recorded_steps,
            success_checkpoint=success_checkpoint,
            discovery_run_id=run_id,
            recorded_by="discovery_agent",
        )
