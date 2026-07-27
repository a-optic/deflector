# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Baseline tests for LifeOS pre-filter. Run with: python -m pytest lifeos/test_prefilter.py"""

from lifeos.prefilter import scan, is_safe, summarize
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
