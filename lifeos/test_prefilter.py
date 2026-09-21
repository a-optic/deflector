# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Baseline tests for LifeOS pre-filter.
Run with: python -m pytest lifeos/test_prefilter.py

CREDENTIAL-SHAPED FIXTURES BELOW ARE DELIBERATE. They are meant to look like real
keys, because a fixture no scanner would flag proves nothing about a detector.
GitHub push protection blocks pushes containing them; that block is the system
working, and is resolved as "used in tests" -- never by sanitising the fixture.
Splitting the literal or swapping in an obviously-fake value weakens coverage
silently, since the suite still passes. See CONTRIBUTING.md, "Credential-shaped
test fixtures are deliberate".
"""

from lifeos.prefilter import BLOCK_CATEGORIES, has_block, is_safe, redact, scan, summarize
from private_ref import INTERNAL_DOMAIN, PRIVATE_TEST_IP


def _cats(text: str) -> set[str]:
    return {h.category for h in scan(text)}


class TestSafeText:
    def test_empty(self):
        assert is_safe("")

    def test_plain_prose(self):
        assert is_safe("Reviewed the personal journal for last week. No action items.")

    def test_public_ip_not_flagged(self):
        assert is_safe("Public DNS: 8.8.8.8 and 1.1.1.1")

    def test_version_string_not_flagged(self):
        assert is_safe("Ollama version 0.11.3")


class TestPrivateIP:
    def test_class_a(self):
        assert "ip_private" in _cats(f"Studio at {PRIVATE_TEST_IP}")

    def test_class_b(self):
        assert "ip_private" in _cats("Router 172.16.0.1")

    def test_class_c(self):
        assert "ip_private" in _cats("mini 192.168.1.20")

    def test_boundary_172_15_not_flagged(self):
        # 172.15.x.x is public, not RFC1918
        assert "ip_private" not in _cats("Reach 172.15.0.1")

    def test_boundary_172_32_not_flagged(self):
        assert "ip_private" not in _cats("Reach 172.32.0.1")


class TestHostname:
    def test_lab_subdomain(self):
        assert "hostname_lab" in _cats(f"Reach studio.lab.{INTERNAL_DOMAIN}")

    def test_vpn_subdomain(self):
        assert "hostname_lab" in _cats(f"wg peer at gw.vpn.{INTERNAL_DOMAIN}")

    def test_root_domain_not_flagged(self):
        # bare apex is planned public; only lab/vpn/internal/homelab subdomains flagged
        assert "hostname_lab" not in _cats(f"Site: {INTERNAL_DOMAIN}")


class TestCredentials:
    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123XYZdef456"
        assert "cred_jwt" in _cats(f"Bearer {jwt}")

    def test_github_pat(self):
        assert "cred_github_pat" in _cats("token=ghp_abcdefghijklmnopqrstuvwxyz012345")

    def test_aws_access_key(self):
        assert "cred_aws" in _cats("AKIAIOSFODNN7EXAMPLE in config")

    def test_slack_bot(self):
        assert "cred_slack" in _cats("xoxb-1234567890-abcdefghij")

    def test_openai_key(self):
        assert "cred_openai" in _cats("OPENAI=sk-proj-abcdefghij0123456789xyz")

    def test_anthropic_key(self):
        assert "cred_openai" in _cats("ANTHROPIC=sk-ant-api03-abcdefghij0123456789xyz")

    def test_ssh_priv_header(self):
        assert "cred_ssh_priv" in _cats("-----BEGIN OPENSSH PRIVATE KEY-----")

    def test_labeled_secret_blob(self):
        assert "cred_base64_lg" in _cats('secret="AbCdEf0123456789XyZaBcDe"')

    def test_random_base64_alone_not_flagged(self):
        # No preceding "secret"/"token" label — treat as noise.
        assert "cred_base64_lg" not in _cats("AbCdEf0123456789XyZaBcDe")


class TestSummarize:
    def test_multi_category(self):
        text = f"Studio {PRIVATE_TEST_IP} with ghp_abcdefghijklmnopqrstuvwxyz012345"
        cats = summarize(scan(text))
        assert cats["ip_private"] == 1
        assert cats["cred_github_pat"] == 1

    def test_empty_summary(self):
        assert summarize(scan("clean text")) == {}


class TestBlockRedactSplit:
    """Only full-private-key material still forces a hard fallback to local.
    Everything else -- credentials AND home-network addresses/hostnames -- is
    redacted in place and still escalates (see BLOCK_CATEGORIES in
    lifeos/prefilter.py for the audit-log numbers behind that split)."""

    def test_block_categories(self):
        assert BLOCK_CATEGORIES == {"cred_ssh_priv"}

    def test_has_block_ssh_priv(self):
        assert has_block(scan("-----BEGIN OPENSSH PRIVATE KEY-----")) == "cred_ssh_priv"

    def test_ip_private_no_longer_blocks(self):
        # 99.5% of historical refusals were this category alone; it redacts now.
        hits = scan(f"reach the box at {PRIVATE_TEST_IP}")
        assert any(h.category == "ip_private" for h in hits)  # still detected
        assert has_block(hits) is None                         # but no longer blocking

    def test_hostname_lab_no_longer_blocks(self):
        hits = scan(f"ssh gw.vpn.{INTERNAL_DOMAIN}")
        assert any(h.category == "hostname_lab" for h in hits)
        assert has_block(hits) is None

    def test_has_block_none_on_credential_only(self):
        assert has_block(scan("token: AKIAIOSFODNN7EXAMPLE")) is None

    def test_redact_credential_hit(self):
        text = "token: AKIAIOSFODNN7EXAMPLE"
        mapping: dict = {}
        out = redact(text, scan(text), mapping, "n1")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        ph = next(iter(mapping))
        assert ph in out and mapping[ph] == "AKIAIOSFODNN7EXAMPLE"

    def test_redact_masks_private_ip_and_hostname(self):
        text = f"box at {PRIVATE_TEST_IP} via gw.vpn.{INTERNAL_DOMAIN}"
        mapping: dict = {}
        out = redact(text, scan(text), mapping, "n1")
        assert PRIVATE_TEST_IP not in out
        assert INTERNAL_DOMAIN not in out
        assert set(mapping.values()) >= {PRIVATE_TEST_IP}

    def test_redact_still_excludes_ssh_key(self):
        # the one category redact() must leave alone -- callers block on it first
        text = "key -----BEGIN RSA PRIVATE KEY----- here"
        mapping: dict = {}
        out = redact(text, scan(text), mapping, "n1")
        assert "-----BEGIN RSA PRIVATE KEY-----" in out
        assert not mapping

    def test_same_value_reuses_one_placeholder(self):
        # the same host named repeatedly must not look like several hosts
        text = f"{PRIVATE_TEST_IP} then {PRIVATE_TEST_IP} then {PRIVATE_TEST_IP}"
        mapping: dict = {}
        out = redact(text, scan(text), mapping, "n1")
        assert PRIVATE_TEST_IP not in out
        assert len(mapping) == 1
        ph = next(iter(mapping))
        assert out.count(ph) == 3

    def test_distinct_values_get_distinct_placeholders(self):
        text = "a 10.0.0.1 b 10.0.0.2"
        mapping: dict = {}
        out = redact(text, scan(text), mapping, "n1")
        assert len(mapping) == 2
        assert set(mapping.values()) == {"10.0.0.1", "10.0.0.2"}

    def test_placeholder_stable_across_slots_via_shared_mapping(self):
        # mapping is shared across every text slot of one request
        mapping: dict = {}
        counters: dict = {}
        a = redact(f"first {PRIVATE_TEST_IP}", scan(f"first {PRIVATE_TEST_IP}"),
                   mapping, "n1", counters)
        b = redact(f"second {PRIVATE_TEST_IP}", scan(f"second {PRIVATE_TEST_IP}"),
                   mapping, "n1", counters)
        assert len(mapping) == 1
        ph = next(iter(mapping))
        assert ph in a and ph in b

    def test_redact_overlap_merge(self):
        # cred_jwt and cred_base64_lg can both match the same labeled blob --
        # must collapse to one placeholder, not two overlapping ones.
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123XYZdef456"
        text = f"token: {jwt}"
        mapping: dict = {}
        out = redact(text, scan(text), mapping, "n1")
        assert out.count("<PII_") == 1
        assert jwt not in out
