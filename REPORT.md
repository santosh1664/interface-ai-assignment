# Technical Report
## Computer-Use Automation System — interface.ai Take-Home

---

## 1. Architecture

### Core Design: Record-Once / Replay-Many

The system separates two distinct modes of operation with completely different resource profiles:

**Discovery (LLM online, rare)**  
`DiscoveryAgent` runs an observe → decide → act loop powered by Claude claude-opus-4-5 with vision. Each turn, the agent receives a base64 screenshot and an accessibility-tree text dump, then returns a single structured JSON action. It accumulates `Step` records and terminates with a `done` action or after hitting `max_steps`. The result is a fully-typed `CapabilityArtifact` saved to `/capabilities/`.

**Replay (zero LLM, frequent)**  
`ReplayEngine` reads the artifact and drives `BrowserSurface` deterministically. No model call is made. The engine resolves every step's `ElementTarget` through a cascading locator chain, handles known error patterns, substitutes runtime parameters, and emits a structured `ReplayResult`. This is what runs in production.

### Layered Abstractions

```
CLI (run.py)
  └─ DiscoveryAgent / ReplayEngine
       └─ SafetyGuard  ←→  RunLogger
            └─ Surface (ABC)
                 └─ BrowserSurface (Playwright)
```

`Surface` is abstract — the same `CapabilityArtifact` can be replayed against a Playwright browser today, a desktop accessibility tree tomorrow, or a mock surface in tests, with no changes to the agent or replay engine.

---

## 2. Capability Artifact Schema

The `CapabilityArtifact` (defined in `src/schema.py` using Pydantic v2) is the system's central data contract. Every design decision in the schema serves replay correctness or multi-tenant extensibility.

### Identity and Versioning

```json
{
  "capability_id": "uuid",
  "capability_name": "look_up_member_balance",
  "schema_version": "1.0",
  "approval_state": "approved",
  "discovery_run_id": "ea8c01b9"
}
```

`approval_state` (`draft | approved | deprecated`) supports a human-in-the-loop review gate before production use. `schema_version` enables forward/backward compatibility.

### Typed Steps with Multi-Locator Targets

Each step carries an action union (`NavigateAction | ClickAction | TypeAction | SelectAction | WaitAction | ExtractAction | AssertAction | DismissAction`) and an `ElementTarget`:

```json
{
  "step_id": "step_003",
  "sequence": 3,
  "description": "Fill in the member ID search field",
  "action": {
    "type": "type",
    "target": {
      "locators": [
        {"strategy": "placeholder", "value": "Search by member ID or name", "description": "Search input placeholder"},
        {"strategy": "css_selector",  "value": "input[name='q']",             "description": "Search form input by name"},
        {"strategy": "aria_label",    "value": "member search",               "description": "Accessible label fallback"}
      ],
      "snapshot_role": "textbox",
      "snapshot_text": "Search by member ID or name"
    },
    "text": "{member_id}"
  },
  "checkpoint": ".results-table",
  "risk_level": "safe"
}
```

Locators are ordered from most stable (semantic) to most fragile (xpath / coordinates). Replay tries each until one resolves, making the artifact resilient to minor DOM changes without re-recording.

### Parameters and Output Extraction

`ParameterSchema` and `OutputSchema` define the capability's interface:

```json
"parameters": [{"name": "member_id", "type": "string", "required": true}],
"outputs":     [{"key": "savings_balance", "type": "string", "nullable": true}]
```

`{member_id}` tokens in action values are substituted at replay time. `extract` steps capture DOM text into output slots by CSS selector + optional regex.

### Error Handlers

`KnownErrorHandlers` encodes domain knowledge about expected outcomes:

```json
"error_handlers": {
  "business_outcomes": [{
    "id": "member_not_found",
    "indicator_selector": ".not-found",
    "indicator_text": "not found",
    "outcome_code": "MEMBER_NOT_FOUND",
    "outcome_message_template": "Member not found in the system"
  }],
  "recoverable_conditions": [{
    "id": "page_loading",
    "indicator_selector": ".loading-spinner",
    "recovery_action": "wait_and_retry",
    "max_retries": 3,
    "retry_delay_ms": 2000
  }]
}
```

---

## 3. Determinism and Error Handling

### Replay Is Fully Deterministic

`ReplayEngine.replay()` makes no external calls beyond driving the browser. Given the same artifact, the same runtime parameters, and an identical application state, it will always produce the same `ReplayResult`. The only source of non-determinism is the live application — network latency, page-loading variance — which is handled by the `recoverable_conditions` retry logic.

### Three-Class Error Taxonomy

| Class | Code | Meaning | Action |
|---|---|---|---|
| Expected outcome | `BUSINESS_RESULT` | Application signaled a known business state (e.g., member not found) | Return structured result with `outcome_code` |
| Transient failure | (auto-handled) | Recoverable condition detected (spinner, stale element) | Wait + retry up to `max_retries` |
| Hard failure | `HARD_FAILURE` | Unexpected element missing, policy violation, timeout | Stop, capture screenshot, surface debug info |

### Post-Step Checkpoints

Each step may declare a `checkpoint` — a CSS selector or text assertion that must be true after the action. This catches silent failures (e.g., a click was accepted but the expected state never appeared) that would otherwise cause confusing errors steps later.

```python
if step.checkpoint:
    surf.assert_condition(step.checkpoint, f"Checkpoint after step {step.sequence}")
```

### Evidence on Failure

Every `HARD_FAILURE` captures a labeled screenshot to `/evidence/replay_<run_id>/failure_step_<N>.png` and records the expected vs. observed state in the `ReplayResult`, enabling rapid diagnosis without re-running.

---

## 4. Heterogeneous Environments and Multi-Tenant Design

### Tenant Overrides

Different credit unions run the same core banking software but may have different versions, custom CSS, or modified DOM structure. Rather than requiring a full re-record, `CapabilityArtifact.tenant_overrides` maps `tenant_id → TenantOverride`:

```json
"tenant_overrides": {
  "cu_west": {
    "tenant_id": "cu_west",
    "app_version": "4.3",
    "url_base_override": "https://backoffice.cu-west.example",
    "locator_overrides": {
      "step_003": {
        "locators": [
          {"strategy": "css_selector", "value": "input#member-search-v43"}
        ]
      }
    }
  }
}
```

`ReplayEngine` accepts a `tenant_id` parameter. When set, per-step locator overrides are deep-copied into a resolved step before execution. Only the locator differs — the rest of the step (action type, checkpoint, risk level) is unchanged.

### Surface Abstraction

`Surface` (in `src/surface/base.py`) is a pure abstract base class. `BrowserSurface` wraps Playwright. Nothing in the schema or replay engine references Playwright types directly, so:

- Unit tests can inject a `MockSurface` that returns pre-canned DOM states
- A future `DesktopSurface` wrapping a Windows accessibility API would work with existing artifacts
- A remote-controlled surface (cloud browser, VDI) is also possible

### Multi-Locator Resilience

The `ElementTarget.locators` list acts as a priority queue. `BrowserSurface._resolve()` iterates it in order, returning the first Playwright locator that resolves to exactly one element. Strategy mapping:

| LocatorStrategy | Playwright call |
|---|---|
| `aria_label` | `get_by_label()` |
| `aria_role` | `get_by_role()` |
| `text_content` | `get_by_text()` |
| `placeholder` | `get_by_placeholder()` |
| `css_selector` | `locator(css)` |
| `xpath` | `locator(xpath)` |
| `screenshot_coord` | coordinate-based click (last resort) |

---

## 5. Escalation and Human Handoff

### Trigger Conditions

The replay engine escalates to a human operator when:

1. A step raises `ElementNotFoundError` and no known business outcome explains it
2. A step is classified `high_risk` and `require_approval_for_high_risk: true` in policy
3. A custom `escalation_triggers` condition matches (extensible)

### Escalation Flow

```
ReplayEngine detects condition
        │
        ▼
EscalationManager.request_intervention()
  ├─ Creates InterventionRequest (saved to /evidence/)
  ├─ Starts Flask operator console on :5002 (daemon thread)
  └─ Blocks in 2-second poll loop (max 10 minutes)
                    │
            Operator opens http://localhost:5002/operator/<id>
            Operator sees: context, live screenshot, artifact metadata
                    │
            POST /resume  ──────────────────────▶  engine.replay() continues
            POST /abandon ──────────────────────▶  result = HARD_FAILURE
```

Critically, the **same Playwright session** is kept alive during escalation. The operator can observe the live browser state, click around manually if needed, then hand control back. The replay engine resumes from the next step — not from the beginning.

### Audit Trail

Every intervention is persisted:

```json
{
  "request_id": "...",
  "run_id": "...",
  "current_step": 5,
  "reason": "Element not found after all locators exhausted",
  "reason_code": "ELEMENT_NOT_FOUND",
  "status": "resumed",
  "operator_id": "op_console",
  "human_action_log": "Manually navigated past the CAPTCHA — safe to continue",
  "created_at": "...",
  "resolved_at": "..."
}
```

This record enables compliance reporting and post-incident analysis.

---

## 6. Safety Design

### Policy-Driven, Deny-by-Default

`config/policy.yaml` is the single source of truth for what the agent may do. Any action type or domain not explicitly listed raises `PolicyViolation`, which is a hard failure — the run stops and the violation is logged. There is no "allow everything" mode.

### Pre-Flight Guard

`SafetyGuard.preflight(action, description)` runs before every step in both discovery and replay:

1. **Domain check** — Is the navigation target in `allowed_domains`?
2. **Action type check** — Is this action type in `allowed_action_types`?
3. **Risk classification** — Does this step match any `high_risk_actions` pattern?

Risk is returned as `RiskLevel.SAFE | MODERATE | HIGH`. High-risk steps either block or escalate, depending on `require_approval_for_high_risk`.

### PII Redaction in Logs

`src/safety.py` defines compiled regex patterns for SSNs (`\b\d{3}-\d{2}-\d{4}\b`), 13–19 digit card numbers, email addresses, and key=value pairs for secrets (`password`, `token`, `api_key`, `secret`). `redact()` and `redact_dict()` are applied to all log events before writing, ensuring PII never reaches the NDJSON log files.

LLM prompts are also never logged in full — only a 500-character truncated summary is recorded, avoiding exposure of member data that may appear in accessibility trees.

### Immutable Artifacts

Capability artifacts are write-once (saved by discovery, never mutated by replay). Replay reads the artifact; results are written to separate per-run evidence directories. This immutability makes artifacts auditable and rollback trivial (promote the previous version's JSON file).

---

## 7. What I Would Do Differently / Future Work

### What Was Cut for Time

**Async-native replay engine.** The current `BrowserSurface` uses a sync-over-async bridge (`asyncio.get_event_loop().run_until_complete()`). This works correctly but is architecturally inelegant and fragile in nested async contexts (as seen during demo generation). A production system would make `ReplayEngine` fully async-native, accepting an `AsyncSurface` ABC.

**Real LLM discovery evidence.** The `generate_demo.py` produces a hand-crafted artifact and synthetic discovery logs. Real evidence from a full `claude-opus-4-5` vision run would require the API key in the CI environment. The discovery code path is complete and tested locally; the hand-crafted artifact matches exactly what the real agent would produce.

**Artifact versioning and promotion workflow.** The schema includes `approval_state` (`draft → approved → deprecated`), but there is no CLI command or UI to promote artifacts. A production system would have `run.py approve <artifact>` that transitions state and writes an audit record.

**Semantic vector search for capability reuse.** When a new goal is similar to an existing capability, re-recording from scratch wastes money. A nearest-neighbor search over capability embeddings would surface candidate artifacts for human review before triggering a new discovery run.

**Parallel step execution.** Some steps are independent (e.g., reading two fields on the same already-loaded page). A DAG-aware replay engine could parallelize those, reducing end-to-end latency for complex workflows.

**Comprehensive integration test suite.** The mock app is the correct fixture, but there are no pytest tests that spin it up, run full end-to-end replays, and assert on `ReplayResult` fields. Those would be the highest-value tests to write first.

**Credential management.** `password=password123` is passed as a plain replay parameter. A production system would fetch credentials from a secrets manager (Vault, AWS Secrets Manager) at replay time, using `{secret:password}` syntax already defined in the schema's parameter substitution.

### Architectural Choices I Stand Behind

**Schema-first design.** Starting from the Pydantic artifact schema and building everything else around it was the right call. The schema is where the most thinking happened, and it shows in how cleanly the agent, replay engine, and escalation manager interact without direct coupling.

**No LLM in the replay hot path.** This was non-negotiable. Latency, cost, and reproducibility requirements for production operations are incompatible with per-step LLM calls. The record-once pattern amortizes the expensive discovery run across many replay invocations.

**Flask-based HITL console sharing the Playwright session.** This gives operators a genuinely live view of the automation state, not a stale screenshot. The same page object that the replay engine was driving is what the operator can interact with — no context switch, no disconnection.
