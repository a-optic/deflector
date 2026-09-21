# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The destination classifier must fail CLOSED.

Regression guard for a latent defect: the privacy destination used to be

    destination = OLLAMA_CLOUD if client_key == "cloud" else LOCAL

which answers "is this the one cloud key I know about?" rather than "is this
off-box". Every other key -- including any vendor added later -- landed in the
LOCAL bucket, and LOCAL means "data never leaves the machine, skip redaction".
privacy/engine.py gates Tier B secret blocking, the restricted-provider reroute
and every redaction tier behind `is_cloud`, so a new upstream would have sent
prompts out with secrets and PII intact, with no error and nothing logged.

Offline. Run: .venv/bin/python -m pytest tests/test_privacy_destination.py -q
"""

import main
from privacy import Redact
from privacy.config import LOCAL, PrivacyConfig
from privacy.tier_a import compile_tier_a


class TestFailsClosed:
    def test_unrecognised_key_is_not_local(self):
        # THE test. Any unmapped upstream must not be treated as this machine.
        for key in ("groq", "openrouter", "cerebras", "", "clou", "CLOUD"):
            assert main._provider_for(key) != LOCAL, key

    def test_unrecognised_key_takes_the_cloud_path(self):
        # Not-LOCAL is only half of it -- what matters is that the engine's
        # is_cloud() agrees, since that is what actually gates redaction.
        cfg = PrivacyConfig()
        assert cfg.is_cloud(main._provider_for("groq")) is True

    def test_unknown_remote_is_in_neither_trust_class(self):
        # It must not be `trusted` (that is a claim we have not made) and must
        # not be `restricted` (that would fire the Anthropic reroute path).
        cfg = main.get_config()
        assert main.UNKNOWN_REMOTE not in cfg.trusted
        assert main.UNKNOWN_REMOTE not in cfg.restricted


class TestExistingRoutesUnchanged:
    def test_local_upstreams_still_local(self):
        assert main._provider_for("main") == LOCAL
        assert main._provider_for("tasks") == LOCAL

    def test_ollama_cloud_still_labelled(self):
        assert main._provider_for("cloud") == main.OLLAMA_CLOUD


class TestRedactionActuallyRuns:
    """Assert on the Decision, not the label -- behaviour, not plumbing."""

    def _cfg(self):
        return PrivacyConfig(
            trusted=frozenset(["ollama-cloud"]),
            restricted=frozenset(["anthropic"]),
            redact_list=compile_tier_a([
                {"id": "op_name", "placeholder": "PERSON", "action": "auto",
                 "values": ["Wilhelmina Thorncastle"]},
            ]),
        )

    def _body(self):
        return {"model": "m", "messages": [
            {"role": "user", "content": "email Wilhelmina Thorncastle today"}]}

    def test_unknown_vendor_gets_redacted(self):
        decision = main.privacy_evaluate(
            self._body(), main._provider_for("some-new-vendor"), self._cfg())
        assert isinstance(decision, Redact)
        sent = decision.body["messages"][0]["content"]
        assert "Wilhelmina Thorncastle" not in sent
        assert decision.mapping

    def test_local_is_still_left_alone(self):
        # The other half of the contract: on-box traffic is NOT redacted, which
        # is why mislabelling a vendor as local was so quiet.
        decision = main.privacy_evaluate(
            self._body(), main._provider_for("main"), self._cfg())
        assert not isinstance(decision, Redact)


class TestStartupWarning:
    """These pin `_warn_unclassified_providers`, which reads the OPERATOR'S real
    ~/.agentstop/provider-trust.yaml through get_config().

    Two of the three used to rely on that file's contents implicitly, and one
    broke the day a vendor was pre-classified in it: the fixture had picked
    "groq" as a label it assumed would never be classified, and once groq was
    given a trust class the function correctly reported nothing unclassified.
    The behaviour was right and the test was reading live state.

    So all three now pin the trust classes explicitly, the way
    test_warns_rather_than_raising already did. What the operator happens to
    have classified is not this file's business.
    """

    def test_silent_when_every_label_is_classified(self, monkeypatch):
        monkeypatch.setattr(main, "PROVIDER_LABELS",
                            {"main": LOCAL, "cloud": "ollama-cloud"})
        monkeypatch.setattr(main, "get_config",
                            lambda: PrivacyConfig(trusted=frozenset({"ollama-cloud"})))
        assert main._warn_unclassified_providers() == []

    def test_names_an_unclassified_label(self, monkeypatch):
        monkeypatch.setattr(main, "PROVIDER_LABELS",
                            {"main": LOCAL, "newvendor": "newvendor"})
        monkeypatch.setattr(main, "get_config",
                            lambda: PrivacyConfig(trusted=frozenset({"ollama-cloud"}),
                                                  restricted=frozenset({"anthropic"})))
        assert main._warn_unclassified_providers() == ["newvendor"]

    def test_warns_rather_than_raising(self, monkeypatch):
        # Must not be fatal: provider-trust.yaml is optional, so a fresh
        # checkout has nothing classified and still has to boot.
        monkeypatch.setattr(main, "PROVIDER_LABELS", {"cloud": "ollama-cloud"})
        monkeypatch.setattr(main, "get_config", lambda: PrivacyConfig())
        assert main._warn_unclassified_providers() == ["ollama-cloud"]
