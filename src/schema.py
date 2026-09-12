"""
Capability Artifact Schema
--------------------------
A typed, versioned, serializable description of a recorded automation flow.
This is the contract between the discovery agent (which records it) and the
replay engine (which executes it deterministically without an LLM).

Design goals:
- Human-readable and reviewable
- Machine-invocable with typed parameters and outputs
- Surface-agnostic (browser today, desktop tomorrow via same schema)
- Supports multi-locator targeting with explicit fallback ordering
- Versioned so drift can be detected and managed across tenants
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE    = "navigate"
    CLICK       = "click"
    TYPE        = "type"
    SELECT      = "select"
    WAIT        = "wait"
    EXTRACT     = "extract"
    ASSERT      = "assert"
    SCREENSHOT  = "screenshot"
    SCROLL      = "scroll"
    DISMISS     = "dismiss"   # dismiss a dialog/modal

class RiskLevel(str, Enum):
    SAFE        = "safe"        # fully reversible (reads, navigates)
    MODERATE    = "moderate"    # submits data but reversible (e.g. edit a field)
    HIGH        = "high"        # irreversible (transfer, delete, confirm)

class LocatorStrategy(str, Enum):
    """Ordered from most stable to most fragile."""
    ARIA_LABEL      = "aria_label"
    ARIA_ROLE       = "aria_role"
    TEXT_CONTENT    = "text_content"
    PLACEHOLDER     = "placeholder"
    CSS_SELECTOR    = "css_selector"
    XPATH           = "xpath"
    SCREENSHOT_COORD = "screenshot_coord"   # fallback: visual coordinate

class SurfaceType(str, Enum):
    BROWSER_MODERN  = "browser_modern"
    BROWSER_LEGACY  = "browser_legacy"   # iframes, framesets, table layouts
    DESKTOP_APP     = "desktop_app"      # OS accessibility tree

class OutcomeType(str, Enum):
    SUCCESS         = "success"
    BUSINESS_RESULT = "business_result"  # expected non-success (member not found, etc.)
    RECOVERABLE     = "recoverable"      # transient; can retry
    HARD_FAILURE    = "hard_failure"     # stop and surface error


# ---------------------------------------------------------------------------
# Element locators — multi-strategy with fallback ordering
# ---------------------------------------------------------------------------

class Locator(BaseModel):
    """
    How to find a UI element on replay.

    Multiple strategies are listed in preference order.  The replay engine
    tries each in sequence and uses the first that resolves to exactly one
    interactive element. Explicit ordering > implicit heuristics.
    """
    strategy: LocatorStrategy
    value: str
    frame: Optional[str] = Field(
        None,
        description="CSS/XPath selector of the iframe to enter first, if any"
    )
    # For screenshot_coord — coordinates relative to viewport
    x: Optional[int] = None
    y: Optional[int] = None
    description: str = Field(
        ...,
        description="Human-readable explanation of why this locator was chosen "
                    "and why it is expected to be stable"
    )


class ElementTarget(BaseModel):
    """
    An element addressed by multiple locators in fallback order.
    The first locator that resolves at replay time wins.
    """
    locators: List[Locator] = Field(min_length=1)
    # Snapshot for drift detection: what text/state did we see at record time?
    snapshot_text: Optional[str] = None
    snapshot_role: Optional[str] = None


# ---------------------------------------------------------------------------
# Step actions
# ---------------------------------------------------------------------------

class NavigateAction(BaseModel):
    type: Literal[ActionType.NAVIGATE] = ActionType.NAVIGATE
    url_template: str = Field(
        ...,
        description="URL with {param_name} placeholders for runtime substitution"
    )
    wait_for: Optional[str] = Field(
        None,
        description="CSS/ARIA selector to wait for after navigation"
    )

class ClickAction(BaseModel):
    type: Literal[ActionType.CLICK] = ActionType.CLICK
    target: ElementTarget
    risk: RiskLevel = RiskLevel.SAFE

class TypeAction(BaseModel):
    type: Literal[ActionType.TYPE] = ActionType.TYPE
    target: ElementTarget
    value_template: str = Field(
        ...,
        description="Value with {param_name} placeholders; sensitive values "
                    "use {secret:param_name} and are never persisted"
    )
    clear_first: bool = True
    risk: RiskLevel = RiskLevel.SAFE

class SelectAction(BaseModel):
    type: Literal[ActionType.SELECT] = ActionType.SELECT
    target: ElementTarget
    option_template: str
    risk: RiskLevel = RiskLevel.SAFE

class WaitAction(BaseModel):
    type: Literal[ActionType.WAIT] = ActionType.WAIT
    condition: str = Field(
        ...,
        description="CSS selector, ARIA label, or 'network_idle' / 'timeout:NNNms'"
    )
    timeout_ms: int = 10_000

class ExtractAction(BaseModel):
    type: Literal[ActionType.EXTRACT] = ActionType.EXTRACT
    output_key: str = Field(
        ...,
        description="Key this value is stored under in the run output"
    )
    target: ElementTarget
    attribute: str = Field(
        "text_content",
        description="'text_content', 'value', 'href', or any HTML attribute name"
    )

class AssertAction(BaseModel):
    """
    Checkpoint: verify we are in the expected state before proceeding.
    Failure here is a hard_failure, not a business_result.
    """
    type: Literal[ActionType.ASSERT] = ActionType.ASSERT
    condition: str = Field(
        ...,
        description="Human-readable description of what should be true"
    )
    target: Optional[ElementTarget] = None
    expected_text: Optional[str] = None
    expected_url_contains: Optional[str] = None

class DismissAction(BaseModel):
    """Dismiss a known interstitial / dialog. Classified as recoverable handling."""
    type: Literal[ActionType.DISMISS] = ActionType.DISMISS
    trigger_selector: str = Field(
        ...,
        description="Selector that, when present, indicates the dialog appeared"
    )
    dismiss_target: ElementTarget
    optional: bool = Field(
        True,
        description="If True, skip silently when the dialog is not present"
    )


StepAction = Union[
    NavigateAction, ClickAction, TypeAction, SelectAction,
    WaitAction, ExtractAction, AssertAction, DismissAction
]


# ---------------------------------------------------------------------------
# Error / exceptional state handling instructions
# ---------------------------------------------------------------------------

class BusinessOutcomePattern(BaseModel):
    """
    A known, expected non-success result — not a failure, but a legitimate
    answer the caller needs to know about.

    Example: 'Member not found' after a search is a business_result, not a crash.
    """
    id: str
    description: str
    # Selector that, when visible, signals this outcome
    indicator_selector: str
    indicator_text: Optional[str] = None
    # What the caller receives
    outcome_code: str   # e.g. "MEMBER_NOT_FOUND"
    outcome_message_template: str

class RecoverableCondition(BaseModel):
    """
    A transient condition the replay engine handles automatically.

    Examples:
    - Session timeout → re-login
    - Spinner/loading → wait and retry
    - Known interstitial → dismiss it
    """
    id: str
    description: str
    indicator_selector: str
    recovery_action: Literal["wait_and_retry", "dismiss", "reload", "re_login"]
    max_retries: int = 3
    retry_delay_ms: int = 2_000

class KnownErrorHandlers(BaseModel):
    business_outcomes: List[BusinessOutcomePattern] = []
    recoverable_conditions: List[RecoverableCondition] = []


# ---------------------------------------------------------------------------
# Input / output contracts
# ---------------------------------------------------------------------------

class ParameterSchema(BaseModel):
    name: str
    type: Literal["string", "integer", "boolean", "enum"]
    required: bool = True
    description: str
    enum_values: Optional[List[str]] = None
    sensitive: bool = Field(
        False,
        description="If True, value is used at runtime but NEVER persisted in logs or artifacts"
    )

class OutputSchema(BaseModel):
    key: str
    type: Literal["string", "integer", "boolean", "list", "object"]
    description: str
    nullable: bool = False


# ---------------------------------------------------------------------------
# Step (wrapper around an action with metadata)
# ---------------------------------------------------------------------------

class Step(BaseModel):
    step_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    sequence: int
    description: str
    action: StepAction
    # Optional checkpoint after this step
    checkpoint: Optional[AssertAction] = None
    # Known recoverable conditions to check BEFORE executing this step
    precondition_checks: List[str] = Field(
        [],
        description="IDs of RecoverableCondition entries to check before acting"
    )


# ---------------------------------------------------------------------------
# Capability Artifact — the top-level schema
# ---------------------------------------------------------------------------

class TenantOverride(BaseModel):
    """
    Per-tenant/version customization of a capability.
    Overrides specific locators or URL patterns without re-recording the whole flow.
    """
    tenant_id: str
    app_version: Optional[str] = None
    # Locator overrides: step_id → replacement ElementTarget
    locator_overrides: Dict[str, ElementTarget] = {}
    url_base_override: Optional[str] = None
    notes: str = ""


class CapabilityArtifact(BaseModel):
    """
    A reusable, versioned automation capability.

    This is what the discovery agent produces and what the replay engine consumes.
    An AI agent invokes it by name with typed parameters and receives typed outputs.
    """
    # Identity
    capability_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = Field(..., description="Short slug, e.g. 'look_up_member_balance'")
    display_name: str
    description: str
    version: str = "1.0.0"

    # Target
    surface_type: SurfaceType = SurfaceType.BROWSER_MODERN
    entry_point: str = Field(
        ...,
        description="URL or application name where the flow starts"
    )
    allowed_domains: List[str] = Field(
        ...,
        description="Domains this capability is allowed to navigate to"
    )

    # Contract
    parameters: List[ParameterSchema] = []
    outputs: List[OutputSchema] = []

    # The recorded flow
    steps: List[Step] = Field(min_length=1)

    # Error/exception handling
    error_handlers: KnownErrorHandlers = Field(default_factory=KnownErrorHandlers)

    # Success condition
    success_checkpoint: AssertAction = Field(
        ...,
        description="Final assertion that confirms the goal was achieved"
    )

    # Risk classification
    overall_risk: RiskLevel = RiskLevel.SAFE
    requires_human_approval: bool = Field(
        False,
        description="If True, a human must approve before replay can proceed past HIGH-risk steps"
    )

    # Multi-tenant support
    base_tenant_id: Optional[str] = None
    tenant_overrides: List[TenantOverride] = []

    # Provenance
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_by: str = "discovery_agent"
    discovery_run_id: Optional[str] = None
    replay_count: int = 0
    last_replayed_at: Optional[datetime] = None

    # Approval state (stretch: confidence gating)
    approval_state: Literal["draft", "approved", "deprecated"] = "draft"

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


# ---------------------------------------------------------------------------
# Replay result contract
# ---------------------------------------------------------------------------

class StepResult(BaseModel):
    step_id: str
    sequence: int
    description: str
    status: Literal["success", "skipped", "failed"]
    outcome_type: Optional[OutcomeType] = None
    detail: Optional[str] = None
    screenshot_path: Optional[str] = None
    duration_ms: Optional[int] = None


class ReplayResult(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    capability_id: str
    capability_name: str
    capability_version: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None

    # Overall outcome
    outcome: OutcomeType
    outcome_code: Optional[str] = None   # for business_result
    message: str = ""

    # What the agent gets back (typed outputs declared in the artifact)
    outputs: Dict[str, Any] = {}

    # Per-step trace
    steps: List[StepResult] = []

    # Debug info on failure
    failed_step: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    failure_screenshot: Optional[str] = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


# ---------------------------------------------------------------------------
# Intervention request (human-in-the-loop)
# ---------------------------------------------------------------------------

class InterventionRequest(BaseModel):
    """Sent to the operator surface when automation cannot safely proceed."""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capability_id: str
    capability_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Context for the operator
    current_step: int
    current_step_description: str
    reason: str         # why we stopped
    reason_code: Literal[
        "stuck_no_progress",
        "high_risk_action",
        "unrecognized_state",
        "max_retries_exceeded",
        "replay_hard_failure",
        "manual_trigger"
    ]
    screenshot_path: Optional[str] = None
    page_url: Optional[str] = None

    # Control transfer
    status: Literal["pending", "active", "completed", "abandoned"] = "pending"
    operator_id: Optional[str] = None
    resumed_at: Optional[datetime] = None
    human_action_log: List[Dict[str, Any]] = []

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
