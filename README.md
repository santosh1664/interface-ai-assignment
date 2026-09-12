# Computer-Use Automation System
### interface.ai Take-Home Project

A production-grade backend for AI agents operating legacy bank/credit-union back-office software through the UI (no API). The system discovers workflows once with an LLM, records them as typed, versioned **Capability Artifacts**, and replays them deterministically in production — with no LLM in the hot path.

---

## Architecture Overview

```
Discovery (LLM online)          Production Replay (LLM offline)
────────────────────────        ─────────────────────────────────
Goal (natural language)         CapabilityArtifact (JSON)
        │                               │
        ▼                               ▼
DiscoveryAgent                  ReplayEngine
  • Screenshots + AX tree         • Multi-locator fallback
  • Claude claude-opus-4-5        • Param substitution
  • JSON action loop              • Error taxonomy
        │                         • Policy guard
        ▼                         • HITL escalation
CapabilityArtifact ────────────────────────────────────────────▶
        │                               │
        ▼                               ▼
/capabilities/*.json          BUSINESS_RESULT | SUCCESS | HARD_FAILURE
/evidence/discovery_*/
```

---

## Quick Start

### Prerequisites

```bash
pip install -r requirements.txt          # anthropic, playwright, flask, pydantic, pyyaml
playwright install chromium              # browser binary
```

### 1 — Start the mock bank app

```bash
python run.py serve-mock
# → http://localhost:5001   credentials: admin / password123
# → Members: 10001 (Alice), 10002 (Bob), 10003 (Carol/suspended)
```

### 2 — Run discovery (requires `ANTHROPIC_API_KEY`)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python run.py discover \
  --goal "look up member 10001 and read their savings balance" \
  --entry http://localhost:5001 \
  --name look_up_member_balance
# Writes: capabilities/look_up_member_balance_<run_id>.json
#         evidence/discovery_<run_id>/
```

### 3 — Replay the artifact (no API key needed)

```bash
# Happy path
python run.py replay \
  --artifact "look_up_member_balance_*.json" \
  --params member_id=10001 password=password123

# Business outcome — member not found
python run.py replay \
  --artifact "look_up_member_balance_*.json" \
  --params member_id=99999 password=password123
```

### 4 — Generate full demo evidence (no API key needed)

```bash
python generate_demo.py
# Builds artifact, writes synthetic discovery logs, runs both replays
# All output under evidence/ and capabilities/
```

---

## Project Structure

```
interface-ai-assignment/
├── src/
│   ├── schema.py          # Pydantic v2 CapabilityArtifact + all types
│   ├── agent.py           # LLM discovery loop (Claude claude-opus-4-5 + vision)
│   ├── replay.py          # Deterministic replay engine (zero LLM)
│   ├── safety.py          # Policy guard + PII redaction
│   ├── logger.py          # Structured NDJSON run logging
│   ├── escalation.py      # HITL operator console (Flask)
│   └── surface/
│       ├── base.py        # Abstract Surface ABC
│       └── browser.py     # Playwright implementation
├── mock_app/
│   ├── server.py          # Flask "CoreBanker v4.2" (hostile legacy HTML)
│   └── templates/         # Table-based, no test IDs, confirmation dialogs
├── config/
│   └── policy.yaml        # Domain/action allowlist + risk classification
├── capabilities/          # Saved CapabilityArtifact JSON files
├── evidence/              # Per-run logs + screenshots
├── run.py                 # CLI: discover | replay | serve-mock
├── generate_demo.py       # End-to-end demo without API key
├── requirements.txt
├── README.md
└── REPORT.md
```

---

## Running Tests / Verification

```bash
# Verify schema round-trips cleanly
python -c "
from src.schema import CapabilityArtifact
import json, pathlib
p = sorted(pathlib.Path('capabilities').glob('*.json'))[-1]
a = CapabilityArtifact.model_validate_json(p.read_text())
print('Steps:', len(a.steps), ' | Risk:', a.overall_risk.value)
"

# Check PII redaction
python -c "
from src.safety import redact
print(redact('SSN 123-45-6789 and card 4111111111111111'))
"

# Verify policy guard
python -c "
from src.safety import Policy, SafetyGuard
from pathlib import Path
p = Policy(Path('config/policy.yaml'))
g = SafetyGuard(p)
from src.schema import RiskLevel
r = g.preflight({'type': 'click', 'description': 'click confirm button'}, 'confirm')
print('Risk:', r.value)
"
```

---

## Configuration

**`config/policy.yaml`** controls everything the agent/replay is permitted to do:

| Field | Purpose |
|---|---|
| `allowed_domains` | Domains the browser may navigate to |
| `allowed_action_types` | Action categories permitted in any artifact |
| `high_risk_actions` | Patterns that escalate to human approval |
| `require_approval_for_high_risk` | `true` = pause + escalate; `false` = block |
| `max_steps` | Hard cap on steps per run |
| `step_timeout_ms` | Per-step timeout |

---

## Human-in-the-Loop Escalation

When a high-risk or unrecognized condition is detected during replay, the engine calls `EscalationManager.request_intervention()`, which:

1. Saves an `InterventionRequest` record to `/evidence/`
2. Starts a Flask operator console on port 5002
3. Blocks (polling every 2 s, 10-min max) until operator responds

Operator sees: step context, live screenshot, artifact metadata.  
Operator actions: **Resume** (with free-text log) or **Abandon**.

```
GET  http://localhost:5002/operator/<request_id>          # operator console
GET  http://localhost:5002/operator/<request_id>/screenshot
POST http://localhost:5002/operator/<request_id>/resume
POST http://localhost:5002/operator/<request_id>/abandon
GET  http://localhost:5002/operator/status
```

---

## Safety Design

- **Deny-by-default**: any domain or action type not in `policy.yaml` raises `PolicyViolation`
- **PII redaction**: SSNs, card numbers, emails, and secret key-value pairs are redacted before logging
- **Risk classification**: steps are rated `safe / moderate / high` — high-risk steps require human approval before proceeding
- **Immutable artifact**: discovery artifacts are write-once; production replay is read-only

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | Discovery only | Claude API calls during LLM-driven discovery |

Replay, the mock app, and the demo generator require **no API key**.
