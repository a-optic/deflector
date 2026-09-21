# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
lifeos_gate tests: redact-and-escalate instead of always falling back to local.
Run: .venv/bin/python -m pytest test_lifeos_gate.py -q

CREDENTIAL-SHAPED FIXTURES BELOW ARE DELIBERATE. They are meant to look like real
keys, because a fixture no scanner would flag proves nothing about a detector.
GitHub push protection blocks pushes containing them; that block is the system
working, and is resolved as "used in tests" -- never by sanitising the fixture.
Splitting the literal or swapping in an obviously-fake value weakens coverage
silently, since the suite still passes. See CONTRIBUTING.md, "Credential-shaped
test fixtures are deliberate".
"""

import main


def _body(text: str) -> dict:
    return {"model": "lifeos-cloud-code", "messages": [{"role": "user", "content": text}]}


def test_lifeos_gate_redacts_and_escalates(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)
    body = _body("aws key AKIAABCDEFGHIJKLMNOP")
    effective_model, extras, redact_decision = main.lifeos_gate(
        "lifeos-cloud-code", "mixed", body
    )
    assert effective_model == main.RT["lifeos_prefixes"]["lifeos-cloud-code"]
    assert redact_decision is not None
    out = redact_decision.body["messages"][0]["content"]
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    ph = next(iter(redact_decision.mapping))
    assert redact_decision.mapping[ph] == "AKIAABCDEFGHIJKLMNOP"
    # Original body untouched -- redaction only applies to the outbound copy.
    assert "AKIAABCDEFGHIJKLMNOP" in body["messages"][0]["content"]


def test_lifeos_gate_ssh_key_still_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)
    body = _body("-----BEGIN OPENSSH PRIVATE KEY-----")
    effective_model, extras, redact_decision = main.lifeos_gate(
        "lifeos-cloud-code", "mixed", body
    )
    assert effective_model == main.RT["lifeos_local_fallback"]
    assert redact_decision is None


def test_lifeos_gate_home_ip_redacts_and_escalates(monkeypatch, tmp_path):
    # ip_private used to force a local fallback and accounted for 99.5% of all
    # prefilter refusals, making the cloud lanes unreachable for homelab work.
    # It now masks the address and escalates instead.
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)
    body = _body("reverse proxy is at 192.168.1.50")
    effective_model, extras, redact_decision = main.lifeos_gate(
        "lifeos-cloud-code", "mixed", body
    )
    assert effective_model == main.RT["lifeos_prefixes"]["lifeos-cloud-code"]
    assert redact_decision is not None
    out = redact_decision.body["messages"][0]["content"]
    assert "192.168.1.50" not in out
    assert "192.168.1.50" in redact_decision.mapping.values()
    # caller's original body is never mutated -- only the outbound copy
    assert "192.168.1.50" in body["messages"][0]["content"]


def test_lifeos_gate_clean_body_escalates_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)
    body = _body("please refactor this function")
    effective_model, extras, redact_decision = main.lifeos_gate(
        "lifeos-cloud-code", "mixed", body
    )
    assert effective_model == main.RT["lifeos_prefixes"]["lifeos-cloud-code"]
    assert redact_decision is None


def test_lifeos_gate_private_sensitivity_never_escalates(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)
    body = _body("anything at all")
    effective_model, extras, redact_decision = main.lifeos_gate(
        "lifeos-cloud-code", "private", body
    )
    assert effective_model == main.RT["lifeos_local_fallback"]
    assert redact_decision is None


def test_log_route_detail_key_no_longer_collides(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)
    main._log_route("privacy-block", "modelA", "REJECT", extra={"detail": "secret:aws_akid"})
    # log files are date-stamped; resolve the same way the writer does
    import retention
    rec_line = (retention.log_path(tmp_path, "routing")
                .read_text().strip().splitlines()[-1])
    import json
    rec = json.loads(rec_line)
    assert rec["reason"] == "privacy-block"
    assert rec["detail"] == "secret:aws_akid"
