"""
Deterministic Replay Engine
-----------------------------
Re-executes a CapabilityArtifact without any LLM in the decision loop.

Key properties:
- Input: CapabilityArtifact + typed parameter values
- Output: ReplayResult (success | business_result | hard_failure)
- No LLM calls — same surface, same steps, stable locators
- Explicit error taxonomy (see schema.py):
    * expected business outcomes → OutcomeType.BUSINESS_RESULT
    * recoverable conditions    → handled automatically, retried
    * hard failures             → OutcomeType.HARD_FAILURE + debug info
- Checkpoint verification after each step (when defined)
- Screenshots on failure

Design: the replay engine treats the artifact as a program and the
surface as an interpreter. It does not make decisions; it only executes
and detects. If it cannot proceed, it raises or escalates.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .escalation import EscalationManager
from .logger import RunLogger
from .safety import Policy, PolicyViolation, SafetyGuard
from .schema import (
    ActionType, CapabilityArtifact, ExtractAction, NavigateAction,
    OutcomeType, ReplayResult, RiskLevel, Step, StepResult,
    TenantOverride, TypeAction
)
from .surface.base import ElementNotFoundError, Surface, SurfaceError


class ReplayEngine:
    """
    Executes a CapabilityArtifact deterministically.

    Usage:
        engine = ReplayEngine(surface, policy, logger, evidence_dir)
        result = engine.replay(artifact, params={"member_id": "10001"})
    """

    def __init__(self, surface: Surface, policy: Policy,
                 run_logger: RunLogger, evidence_dir: Path,
                 escalation_manager: Optional[EscalationManager] = None,
                 tenant_id: Optional[str] = None):
        self.surface = surface
        self.policy  = policy
        self.guard   = SafetyGuard(policy)
        self.log     = run_logger
        self.evidence_dir = evidence_dir
        self.escalation   = escalation_manager
        self.tenant_id    = tenant_id

    def replay(self, artifact: CapabilityArtifact,
               params: Optional[Dict[str, Any]] = None) -> ReplayResult:
        """
        Main entry point. Returns a structured ReplayResult.
        Never raises — all failures are encoded in the result.
        """
        params = params or {}
        result = ReplayResult(
            capability_id=artifact.capability_id,
            capability_name=artifact.name,
            capability_version=artifact.version,
            outcome=OutcomeType.HARD_FAILURE,  # pessimistic default
        )
        self.log.run_start(
            goal=artifact.description,
            target=artifact.entry_point,
            capability_name=artifact.name,
        )

        # Resolve tenant overrides
        override = self._get_tenant_override(artifact)

        steps = artifact.steps

        for step in steps:
            # Apply tenant locator overrides if present
            resolved_step = self._apply_override(step, override)

            step_result = StepResult(
                step_id=resolved_step.step_id,
                sequence=resolved_step.sequence,
                description=resolved_step.description,
                status="success",
            )
            result.steps.append(step_result)

            # ---- Pre-step: check recoverable conditions ----
            for cond_id in resolved_step.precondition_checks:
                self._handle_recoverable(cond_id, artifact, resolved_step.sequence)

            # ---- Pre-step: check all known recoverable conditions ----
            for rc in artifact.error_handlers.recoverable_conditions:
                self._handle_recoverable_condition(rc, resolved_step.sequence)

            # ---- Execute the step ----
            t0 = time.monotonic()
            try:
                outcome_code, extracted = self._execute_step(
                    resolved_step, params, artifact
                )
            except PolicyViolation as e:
                self.log.safety_violation(str(e))
                step_result.status = "failed"
                step_result.outcome_type = OutcomeType.HARD_FAILURE
                step_result.detail = str(e)
                result.outcome = OutcomeType.HARD_FAILURE
                result.message = f"Safety policy violation: {e}"
                result.failed_step = resolved_step.description
                self._capture_failure_screenshot(result, resolved_step.sequence)
                break

            except (ElementNotFoundError, SurfaceError) as e:
                # Check if this is a known business outcome
                bo = self._check_business_outcomes(artifact, resolved_step.sequence)
                if bo:
                    step_result.status = "skipped"
                    step_result.outcome_type = OutcomeType.BUSINESS_RESULT
                    step_result.detail = bo["message"]
                    result.outcome = OutcomeType.BUSINESS_RESULT
                    result.outcome_code = bo["code"]
                    result.message = bo["message"]
                    self.log.business_outcome(bo["code"], bo["message"])
                    break

                # Try escalation if available
                if self.escalation:
                    ss_path = self.evidence_dir / f"escalation_step_{resolved_step.sequence:03d}.png"
                    self.surface.screenshot(ss_path)
                    intervened = self.escalation.request_intervention(
                        run_id=self.log.run_id,
                        artifact=artifact,
                        step=resolved_step,
                        reason=str(e),
                        reason_code="replay_hard_failure",
                        screenshot_path=str(ss_path),
                        page_url=self.surface.current_url(),
                    )
                    if intervened:
                        # Human handled it; try to continue
                        self.log.step_success(
                            resolved_step.sequence,
                            f"[HUMAN] {resolved_step.description}"
                        )
                        step_result.status = "success"
                        step_result.detail = "Completed by human operator"
                        continue

                # Hard failure
                step_result.status = "failed"
                step_result.outcome_type = OutcomeType.HARD_FAILURE
                step_result.detail = str(e)
                result.outcome = OutcomeType.HARD_FAILURE
                result.message = str(e)
                result.failed_step = resolved_step.description
                result.expected  = resolved_step.description
                result.observed  = str(e)
                self._capture_failure_screenshot(result, resolved_step.sequence)
                self.log.step_failure(
                    resolved_step.sequence, resolved_step.description,
                    str(e), screenshot_path=result.failure_screenshot
                )
                break

            except Exception as e:
                step_result.status = "failed"
                step_result.outcome_type = OutcomeType.HARD_FAILURE
                step_result.detail = str(e)
                result.outcome = OutcomeType.HARD_FAILURE
                result.message = f"Unexpected error: {e}"
                result.failed_step = resolved_step.description
                self._capture_failure_screenshot(result, resolved_step.sequence)
                self.log.step_failure(
                    resolved_step.sequence, resolved_step.description,
                    str(e), screenshot_path=result.failure_screenshot
                )
                break

            else:
                duration_ms = int((time.monotonic() - t0) * 1000)
                step_result.duration_ms = duration_ms

                if outcome_code:
                    # Business outcome detected mid-step
                    step_result.outcome_type = OutcomeType.BUSINESS_RESULT
                    result.outcome = OutcomeType.BUSINESS_RESULT
                    result.outcome_code = outcome_code
                    result.message = extracted.get("message", outcome_code)
                    break

                if extracted:
                    result.outputs.update(extracted)

                self.log.step_success(
                    resolved_step.sequence, resolved_step.description,
                    extracted=extracted or None
                )

                # ---- Post-step: checkpoint ----
                if resolved_step.checkpoint:
                    cp = resolved_step.checkpoint
                    passed = self.surface.assert_condition(
                        cp.target, cp.expected_text, cp.expected_url_contains
                    )
                    if not passed:
                        step_result.status = "failed"
                        step_result.outcome_type = OutcomeType.HARD_FAILURE
                        step_result.detail = f"Checkpoint failed: {cp.condition}"
                        result.outcome = OutcomeType.HARD_FAILURE
                        result.message  = f"Checkpoint failed: {cp.condition}"
                        result.failed_step = resolved_step.description
                        result.expected  = cp.condition
                        result.observed  = "Checkpoint condition not met"
                        self._capture_failure_screenshot(result, resolved_step.sequence)
                        break
        else:
            # Loop completed without break — verify final checkpoint
            cp = artifact.success_checkpoint
            passed = self.surface.assert_condition(
                cp.target, cp.expected_text, cp.expected_url_contains
            )
            if passed:
                result.outcome = OutcomeType.SUCCESS
                result.message = "Capability executed successfully"
            else:
                result.outcome = OutcomeType.HARD_FAILURE
                result.message = f"Final checkpoint failed: {cp.condition}"
                self._capture_failure_screenshot(result, 999)

        result.finished_at = datetime.now(timezone.utc)
        self.log.run_end(
            outcome=result.outcome.value,
            outcome_code=result.outcome_code,
            message=result.message,
            outputs=result.outputs,
        )
        return result

    # ---- step execution ----

    def _execute_step(self, step: Step, params: Dict[str, Any],
                      artifact: CapabilityArtifact):
        """
        Execute a single step. Returns (outcome_code_or_None, extracted_dict_or_None).
        Raises on surface errors.
        """
        action = step.action
        at = action.type

        # Safety pre-flight
        risk = self.guard.preflight(action, step.description)

        if at == ActionType.NAVIGATE:
            url = self._substitute(action.url_template, params)
            self.guard.check_url(url)
            self.surface.navigate(url, wait_for=getattr(action, "wait_for", None))
            return None, None

        elif at == ActionType.CLICK:
            self.surface.click(action.target)
            return None, None

        elif at == ActionType.TYPE:
            value = self._substitute(action.value_template, params)
            self.surface.type_text(action.target, value, clear_first=action.clear_first)
            return None, None

        elif at == ActionType.SELECT:
            option = self._substitute(action.option_template, params)
            self.surface.select_option(action.target, option)
            return None, None

        elif at == ActionType.WAIT:
            self.surface.wait_for(action.condition, action.timeout_ms)
            return None, None

        elif at == ActionType.EXTRACT:
            value = self.surface.extract_text(action.target, action.attribute)
            return None, {action.output_key: value}

        elif at == ActionType.ASSERT:
            passed = self.surface.assert_condition(
                action.target, action.expected_text, action.expected_url_contains
            )
            if not passed:
                raise SurfaceError(f"Assert failed: {action.condition}")
            return None, None

        elif at == ActionType.DISMISS:
            self.surface.dismiss_if_present(
                action.trigger_selector, action.dismiss_target
            )
            return None, None

        else:
            raise ValueError(f"Unknown action type in artifact: {at}")

    # ---- recoverable condition handling ----

    def _handle_recoverable(self, cond_id: str, artifact: CapabilityArtifact,
                             step_seq: int) -> None:
        for rc in artifact.error_handlers.recoverable_conditions:
            if rc.id == cond_id:
                self._handle_recoverable_condition(rc, step_seq)
                return

    def _handle_recoverable_condition(self, rc, step_seq: int) -> None:
        """Check if a recoverable condition is present; if so, apply recovery."""
        try:
            # Quick non-blocking check
            import asyncio
            from playwright.async_api import TimeoutError as PWTimeout

            # Use wait_for with short timeout
            self.surface.wait_for(rc.indicator_selector, timeout_ms=500)
        except Exception:
            return  # condition not present

        # Condition is present — recover
        for attempt in range(1, rc.max_retries + 1):
            self.log.recoverable_condition(rc.id, rc.recovery_action, attempt)
            if rc.recovery_action == "wait_and_retry":
                time.sleep(rc.retry_delay_ms / 1000)
            elif rc.recovery_action == "reload":
                self.surface.navigate(self.surface.current_url())
            # dismiss and re_login handled separately
            time.sleep(0.5)
            # Re-check
            try:
                self.surface.wait_for(rc.indicator_selector, timeout_ms=300)
            except Exception:
                return  # recovered

    # ---- business outcome detection ----

    def _check_business_outcomes(self, artifact: CapabilityArtifact,
                                  step_seq: int) -> Optional[Dict]:
        """Check if any known business outcome pattern is currently visible."""
        for bo in artifact.error_handlers.business_outcomes:
            try:
                self.surface.wait_for(bo.indicator_selector, timeout_ms=500)
                return {
                    "code": bo.outcome_code,
                    "message": bo.outcome_message_template,
                }
            except Exception:
                continue
        return None

    # ---- tenant override ----

    def _get_tenant_override(self, artifact: CapabilityArtifact) -> Optional[TenantOverride]:
        if not self.tenant_id:
            return None
        for override in artifact.tenant_overrides:
            if override.tenant_id == self.tenant_id:
                return override
        return None

    def _apply_override(self, step: Step,
                         override: Optional[TenantOverride]) -> Step:
        if not override or step.step_id not in override.locator_overrides:
            return step
        # Return a shallow copy with overridden target
        import copy
        new_step = copy.deepcopy(step)
        if hasattr(new_step.action, "target"):
            new_step.action.target = override.locator_overrides[step.step_id]
        return new_step

    # ---- helpers ----

    @staticmethod
    def _substitute(template: str, params: Dict[str, Any]) -> str:
        """Substitute {param_name} placeholders. {secret:name} values are not logged."""
        for k, v in params.items():
            template = template.replace(f"{{{k}}}", str(v))
            template = template.replace(f"{{secret:{k}}}", str(v))
        return template

    def _capture_failure_screenshot(self, result: ReplayResult,
                                     step_seq: int) -> None:
        try:
            ss_path = self.evidence_dir / f"failure_step_{step_seq:03d}.png"
            self.surface.screenshot(ss_path)
            result.failure_screenshot = str(ss_path)
        except Exception:
            pass
