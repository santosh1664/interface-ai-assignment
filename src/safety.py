"""
Safety & Policy Guardrails
--------------------------
Enforces:
1. Domain/action allowlist — agent cannot act outside declared scope
2. Risk classification — reversible vs irreversible actions
3. PII / secret redaction — sensitive data never persists in logs or artifacts
4. Action pre-flight checks — blocks or flags HIGH-risk steps

Design decision: allowlist is configurable via policy.yaml so it can be
tuned per-tenant without code changes. A missing allowlist entry is a
hard block (deny by default).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml

from .schema import ActionType, RiskLevel, StepAction


# ---------------------------------------------------------------------------
# PII / secret patterns to redact from logs and artifacts
# ---------------------------------------------------------------------------

_REDACT_PATTERNS: List[re.Pattern] = [
    re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),           # SSN
    re.compile(r'\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b'),  # card numbers
    re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b'),  # email
    re.compile(r'(?i)(password|token|secret|api[-_]?key)\s*[=:]\s*\S+'),
]

_REDACTION_PLACEHOLDER = "[REDACTED]"


def redact(value: str) -> str:
    """Replace any known-sensitive patterns with [REDACTED]."""
    for pattern in _REDACT_PATTERNS:
        value = pattern.sub(_REDACTION_PLACEHOLDER, value)
    return value


def redact_dict(data: Dict[str, Any], sensitive_keys: List[str]) -> Dict[str, Any]:
    """Redact specific keys from a dict (used on run outputs before logging)."""
    result = {}
    for k, v in data.items():
        if k in sensitive_keys:
            result[k] = _REDACTION_PLACEHOLDER
        elif isinstance(v, str):
            result[k] = redact(v)
        elif isinstance(v, dict):
            result[k] = redact_dict(v, sensitive_keys)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Policy loader
# ---------------------------------------------------------------------------

class Policy:
    """
    Loads allowlist and action policy from policy.yaml.

    Schema of policy.yaml:
        allowed_domains:
          - "localhost"
          - "demo.bank.example"
        allowed_action_types:
          - navigate
          - click
          - type
          - extract
          - assert
          - wait
          - dismiss
        high_risk_actions:
          - type: click
            description_contains: ["confirm", "submit", "delete", "transfer"]
        require_approval_for_high_risk: true
    """

    def __init__(self, policy_path: Optional[Path] = None):
        if policy_path is None:
            policy_path = Path(__file__).parent.parent / "config" / "policy.yaml"

        with open(policy_path) as f:
            raw = yaml.safe_load(f)

        self.allowed_domains: List[str] = raw.get("allowed_domains", [])
        self.allowed_action_types: List[str] = raw.get("allowed_action_types", [])
        self.high_risk_patterns: List[Dict] = raw.get("high_risk_actions", [])
        self.require_approval_for_high_risk: bool = raw.get(
            "require_approval_for_high_risk", True
        )
        self.max_steps: int = raw.get("max_steps", 50)
        self.step_timeout_ms: int = raw.get("step_timeout_ms", 15_000)


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

class PolicyViolation(Exception):
    """Raised when an action is outside the declared policy."""
    pass


class SafetyGuard:
    """
    Stateless guard — call check_* methods before each action.
    All violations raise PolicyViolation; caller decides whether to abort or escalate.
    """

    def __init__(self, policy: Policy):
        self.policy = policy

    # ---- domain check ----

    def check_url(self, url: str) -> None:
        """Block navigation to any domain not in the allowlist."""
        host = urlparse(url).hostname or ""
        if not any(
            host == allowed or host.endswith(f".{allowed}")
            for allowed in self.policy.allowed_domains
        ):
            raise PolicyViolation(
                f"Domain '{host}' is not in the allowlist. "
                f"Allowed: {self.policy.allowed_domains}"
            )

    # ---- action type check ----

    def check_action_type(self, action_type: ActionType) -> None:
        if action_type.value not in self.policy.allowed_action_types:
            raise PolicyViolation(
                f"Action type '{action_type.value}' is not permitted by policy."
            )

    # ---- risk classification ----

    def classify_risk(self, action: StepAction) -> RiskLevel:
        """Heuristically classify risk; explicit risk field on action takes precedence."""
        # Actions that carry an explicit risk field
        if hasattr(action, "risk"):
            if action.risk == RiskLevel.HIGH:
                return RiskLevel.HIGH

        # Heuristic: inspect description / value for high-risk keywords
        action_type = action.type
        desc_text = ""
        if hasattr(action, "description"):
            desc_text += str(action.description)
        if hasattr(action, "value_template"):
            desc_text += str(action.value_template)

        desc_lower = desc_text.lower()
        for pattern in self.policy.high_risk_patterns:
            if pattern.get("type") == action_type.value:
                keywords = pattern.get("description_contains", [])
                if any(kw in desc_lower for kw in keywords):
                    return RiskLevel.HIGH

        if action_type in (ActionType.NAVIGATE, ActionType.WAIT,
                           ActionType.EXTRACT, ActionType.ASSERT,
                           ActionType.SCREENSHOT):
            return RiskLevel.SAFE

        return RiskLevel.MODERATE

    def check_risk(self, risk: RiskLevel, step_description: str) -> None:
        """
        For HIGH-risk actions: raise if approval is required and not yet granted.
        Called by the replay engine; the engine decides to block or escalate.
        """
        if risk == RiskLevel.HIGH and self.policy.require_approval_for_high_risk:
            raise PolicyViolation(
                f"HIGH-risk step requires human approval before proceeding: "
                f"'{step_description}'"
            )

    # ---- combined pre-flight ----

    def preflight(self, action: StepAction, step_description: str) -> RiskLevel:
        """
        Full pre-flight check. Returns the action's risk level on pass.
        Raises PolicyViolation on any violation.
        """
        self.check_action_type(action.type)

        if action.type == ActionType.NAVIGATE:
            url = action.url_template.split("{")[0]   # check base URL before param substitution
            self.check_url(url)

        risk = self.classify_risk(action)
        self.check_risk(risk, step_description)
        return risk
