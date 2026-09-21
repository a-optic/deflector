# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Private addressing is left intact for a TRUSTED destination, and only there.

Presidio does not distinguish private from public -- measured, `10.10.1.60`,
`127.0.0.1` and `34.36.133.15` all score exactly 0.6. Across 15.6 MB of real Pi
session content, 400 of 424 IPv4 tokens (94%) were private or loopback. Masking
those corrupts prompts about this very stack (`127.0.0.1:11434`, `10.10.1.60`)
while protecting nothing: RFC1918 space is shared by millions of networks, the
external IP is never sent, and the infrastructure sits behind a VPN.

The scoping is the point. `unknown-remote` -- a provider with no trust
classification -- keeps the strict behaviour, because `_provider_for()` fails
closed and this must not undo that. So the flag keys on is_trusted(), not
is_cloud().

Offline. Run: .venv/bin/python -m pytest privacy/test_tier_c_ip_scope.py -q
"""

import pytest

from privacy.config import LOCAL, PrivacyConfig
from privacy.engine import Proceed, Redact, privacy_evaluate
from privacy.rehydrate import RehydrateStream
from privacy.tier_c import _is_non_identifying_ip, available

pytestmark = pytest.mark.skipif(not available(), reason="Presidio not installed")

PRIVATE = "10.10.1.60"
PUBLIC = "34.36.133.15"


def _cfg():
    return PrivacyConfig(trusted=frozenset(["ollama-cloud"]),
                         restricted=frozenset(["anthropic"]))


def _body(text):
    return {"model": "m", "messages": [{"role": "user", "content": text}]}


def _sent(dest, text):
    """The text as it would actually leave for `dest`."""
    d = privacy_evaluate(_body(text), dest, _cfg())
    if isinstance(d, Proceed):
        return d.body["messages"][0]["content"], {}
    assert isinstance(d, Redact), f"unexpected {type(d).__name__} for {dest}"
    return d.body["messages"][0]["content"], d.mapping


class TestPrivateAddressing:
    def test_survives_verbatim_to_a_trusted_destination(self):
        sent, _ = _sent("ollama-cloud", f"ssh into {PRIVATE} and restart ollama")
        assert PRIVATE in sent

    def test_is_masked_for_a_restricted_destination(self):
        sent, mapping = _sent("anthropic", f"ssh into {PRIVATE} and restart ollama")
        assert PRIVATE not in sent
        assert PRIVATE in mapping.values()

    def test_is_masked_for_an_unclassified_destination(self):
        # THE scoping test. `unknown-remote` is is_cloud=True and is_trusted=False,
        # so it must keep the strict behaviour -- otherwise this change would
        # quietly undo _provider_for()'s fail-closed guarantee.
        sent, _ = _sent("unknown-remote", f"ssh into {PRIVATE} and restart ollama")
        assert PRIVATE not in sent

    def test_masked_value_is_restored_on_the_way_back(self):
        sent, mapping = _sent("anthropic", f"ssh into {PRIVATE} now")
        ph = next(k for k, v in mapping.items() if v == PRIVATE)
        assert PRIVATE in RehydrateStream(mapping).feed(f"try {ph} first")

    def test_loopback_too(self):
        sent, _ = _sent("ollama-cloud", "ollama listens on 127.0.0.1 for local calls")
        assert "127.0.0.1" in sent


class TestPublicAddressingIsUnaffected:
    @pytest.mark.parametrize("dest", ["ollama-cloud", "anthropic", "unknown-remote"])
    def test_public_ip_is_still_masked_everywhere_off_box(self, dest):
        sent, _ = _sent(dest, f"the endpoint resolved to {PUBLIC} last night")
        assert PUBLIC not in sent


class TestNoCollateralLoosening:
    def test_other_entities_still_redacted_for_trusted(self):
        # The change touches one entity type under one condition. If PERSON or
        # EMAIL_ADDRESS stopped firing for trusted, the filter is too broad.
        sent, _ = _sent("ollama-cloud",
                        f"mail Bartholomew Quenneville at bq@example.com about {PRIVATE}")
        assert "bq@example.com" not in sent
        assert PRIVATE in sent, "the IP allowlist is the only thing that should relax"

    def test_local_is_untouched_as_before(self):
        sent, _ = _sent(LOCAL, f"ssh into {PRIVATE} and mail bq@example.com")
        assert PRIVATE in sent and "bq@example.com" in sent


class TestTheClassifierItself:
    @pytest.mark.parametrize("token", [
        "10.10.1.60", "192.168.1.5", "172.16.0.1", "127.0.0.1", "169.254.1.1",
    ])
    def test_non_identifying(self, token):
        assert _is_non_identifying_ip(token)

    @pytest.mark.parametrize("token", [
        "34.36.133.15", "8.8.8.8", "1.1.1.1",
        "10.10.1.600",      # not an address at all -- a parser gets this right,
        "999.1.1.1",        # a regex on leading octets would not
        "not-an-ip", "",
    ])
    def test_identifying_or_not_an_address(self, token):
        assert not _is_non_identifying_ip(token)
