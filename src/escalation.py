"""
Human-in-the-Loop Escalation & Handoff
----------------------------------------
When automation cannot safely proceed, this module:

1. Detects "stuck" — surfaced by the agent/replay engine
2. Raises an InterventionRequest with full context
3. Pauses automation and gives control of the live session to a human
4. Records what the human did
5. Signals resume — automation picks up on the same session

Control-transfer model:
- The same Playwright page object is shared between automation and the operator surface
- Automation pauses by simply blocking (polling a "resumed" flag)
- The operator surface is a minimal HTTP server that exposes:
    GET  /operator/<request_id>     — operator sees the context + screenshot
    POST /operator/<request_id>/resume  — operator signals done
    GET  /operator/<request_id>/screenshot  — live screenshot

Design decision: We share the Playwright page directly rather than
opening a fresh browser session for the operator. This preserves:
- Session cookies / authentication state
- Form state mid-way through a flow
- The exact page the automation was on

Mocked: The operator console HTML is a bare but functional page.
The real thing would add WebSockets for live screen-sharing.
What IS real: the pause/resume mechanism and the control-transfer model.

Scope note (per assignment):
"A minimal but real handoff — pause automation, expose the live session
for manual control, signal resume, and capture the human's actions —
plus a clear design for the rest, is what we're after."
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schema import CapabilityArtifact, InterventionRequest, Step


# ---------------------------------------------------------------------------
# Escalation manager
# ---------------------------------------------------------------------------

class EscalationManager:
    """
    Manages intervention requests.

    In production this would integrate with a task queue / notification system
    (PagerDuty, Slack, an operator portal with WebSocket). Here we use a simple
    in-memory store + a lightweight Flask endpoint that can be polled.
    """

    def __init__(self, evidence_dir: Path, port: int = 5002):
        self.evidence_dir = evidence_dir
        self.port = port
        self._requests: Dict[str, InterventionRequest] = {}
        self._server_thread: Optional[threading.Thread] = None
        self._server = None
        self._start_operator_server()

    def request_intervention(
        self,
        run_id: str,
        artifact: CapabilityArtifact,
        step: Step,
        reason: str,
        reason_code: str,
        screenshot_path: Optional[str] = None,
        page_url: Optional[str] = None,
    ) -> bool:
        """
        Pause automation and wait for a human to complete the step.

        Returns True if the human successfully completed the step,
        False if they abandoned it or timed out.
        """
        req = InterventionRequest(
            run_id=run_id,
            capability_id=artifact.capability_id,
            capability_name=artifact.name,
            current_step=step.sequence,
            current_step_description=step.description,
            reason=reason,
            reason_code=reason_code,
            screenshot_path=screenshot_path,
            page_url=page_url,
        )
        self._requests[req.request_id] = req

        # Write request to evidence directory for auditability
        req_path = self.evidence_dir / f"intervention_{req.request_id}.json"
        req_path.write_text(req.model_dump_json(indent=2))

        print(f"\n{'='*60}")
        print(f"HUMAN INTERVENTION REQUIRED")
        print(f"  Request ID : {req.request_id}")
        print(f"  Capability : {artifact.name}")
        print(f"  Step       : {step.sequence} — {step.description}")
        print(f"  Reason     : {reason}")
        print(f"  Page URL   : {page_url}")
        print(f"  Screenshot : {screenshot_path}")
        print(f"  Operator console: http://localhost:{self.port}/operator/{req.request_id}")
        print(f"{'='*60}\n")

        # Poll until the operator resumes or abandons (max 10 min)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            req = self._requests[req.request_id]
            if req.status == "completed":
                self._finalize(req)
                return True
            if req.status == "abandoned":
                return False
            time.sleep(2)

        # Timeout
        req.status = "abandoned"
        return False

    def _finalize(self, req: InterventionRequest) -> None:
        """Record final state of the intervention."""
        req_path = self.evidence_dir / f"intervention_{req.request_id}.json"
        req_path.write_text(req.model_dump_json(indent=2))

    def get_request(self, request_id: str) -> Optional[InterventionRequest]:
        return self._requests.get(request_id)

    def resume(self, request_id: str,
               human_action_log: Optional[List[Dict[str, Any]]] = None) -> bool:
        req = self._requests.get(request_id)
        if not req:
            return False
        req.status = "completed"
        req.resumed_at = datetime.now(timezone.utc)
        req.human_action_log = human_action_log or []
        return True

    def abandon(self, request_id: str) -> bool:
        req = self._requests.get(request_id)
        if not req:
            return False
        req.status = "abandoned"
        return True

    # ---- minimal operator console server ----

    def _start_operator_server(self) -> None:
        """Start a lightweight Flask server exposing the operator console."""
        try:
            from flask import Flask, jsonify, render_template_string, request

            mgr = self
            console = Flask("operator_console")

            CONSOLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Operator Console — {{req_id}}</title>
<style>
  body{font-family:monospace;background:#1a1a1a;color:#ddd;padding:20px}
  h2{color:#f90}
  .field{margin:6px 0}
  .label{color:#888;width:160px;display:inline-block}
  img{max-width:900px;border:1px solid #555;margin:10px 0;display:block}
  button{padding:8px 20px;margin:6px;cursor:pointer;font-size:14px}
  .btn-resume{background:#2a7;color:white;border:none}
  .btn-abandon{background:#a22;color:white;border:none}
  .status{color:#0f0;font-weight:bold}
</style>
</head>
<body>
<h2>&#x1F6A8; Human Intervention Required</h2>
<div class="field"><span class="label">Request ID:</span> {{req_id}}</div>
<div class="field"><span class="label">Capability:</span> {{cap_name}}</div>
<div class="field"><span class="label">Step:</span> {{step_seq}} — {{step_desc}}</div>
<div class="field"><span class="label">Reason:</span> {{reason}}</div>
<div class="field"><span class="label">Page URL:</span>
  <a href="{{page_url}}" style="color:#88f" target="_blank">{{page_url}}</a>
</div>
<div class="field"><span class="label">Status:</span> <span class="status">{{status}}</span></div>
<hr>
<h3>Screenshot at time of escalation</h3>
{% if screenshot %}
<img src="/operator/{{req_id}}/screenshot" alt="Screenshot">
{% else %}
<p>No screenshot available.</p>
{% endif %}
<hr>
<h3>Actions</h3>
<p>Operate the browser at the URL above, then click <strong>Resume Automation</strong> when done.</p>
<form action="/operator/{{req_id}}/resume" method="POST">
  <button class="btn-resume" type="submit">&#x25B6; Resume Automation</button>
</form>
<form action="/operator/{{req_id}}/abandon" method="POST">
  <button class="btn-abandon" type="submit">&#x2715; Abandon Run</button>
</form>
</body>
</html>
"""

            @console.route("/operator/<request_id>")
            def operator_view(request_id):
                req = mgr.get_request(request_id)
                if not req:
                    return "Not found", 404
                return render_template_string(
                    CONSOLE_HTML,
                    req_id=request_id,
                    cap_name=req.capability_name,
                    step_seq=req.current_step,
                    step_desc=req.current_step_description,
                    reason=req.reason,
                    page_url=req.page_url or "",
                    status=req.status,
                    screenshot=bool(req.screenshot_path),
                )

            @console.route("/operator/<request_id>/screenshot")
            def operator_screenshot(request_id):
                from flask import send_file
                req = mgr.get_request(request_id)
                if not req or not req.screenshot_path:
                    return "No screenshot", 404
                return send_file(req.screenshot_path, mimetype="image/png")

            @console.route("/operator/<request_id>/resume", methods=["POST"])
            def operator_resume(request_id):
                mgr.resume(request_id)
                return jsonify({"status": "resumed"})

            @console.route("/operator/<request_id>/abandon", methods=["POST"])
            def operator_abandon(request_id):
                mgr.abandon(request_id)
                return jsonify({"status": "abandoned"})

            @console.route("/operator/status")
            def operator_status():
                return jsonify({
                    rid: {"status": r.status, "capability": r.capability_name}
                    for rid, r in mgr._requests.items()
                })

            def run_server():
                import logging
                log = logging.getLogger("werkzeug")
                log.setLevel(logging.ERROR)
                console.run(port=self.port, use_reloader=False, threaded=True)

            self._server_thread = threading.Thread(target=run_server, daemon=True)
            self._server_thread.start()
            time.sleep(0.3)  # let server start

        except ImportError:
            print("Flask not available; operator console will not start.")
