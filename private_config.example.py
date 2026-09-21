# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Private constants template — copy to `private_config.py` and fill in YOUR values.

    cp private_config.example.py private_config.py

`private_config.py` is gitignored and MUST NOT be committed (a pre-commit hook enforces
this). It holds the operator-specific identifiers Deflector needs to recognize as private:
the internal domain the LifeOS prefilter flags, the operator name variants the privacy
engine redacts, and a real private IP used only to exercise the RFC1918 matcher in tests.

If `private_config.py` is absent (e.g. a fresh public clone or CI), the code falls back to
the neutral placeholders defined in `private_ref.py`, so everything still imports and the
test suite still passes — it just won't match YOUR real identifiers until you create it.
"""

# The internal domain whose lab/vpn/homelab/internal subdomains the prefilter flags as
# private (e.g. "acme-lab.io"). Bare apex is treated as public and not flagged.
INTERNAL_DOMAIN = "example-lab.com"

# Operator name variants the privacy engine (tier A) redacts/reroutes/blocks.
OPERATOR_NAMES = ["Jordan Rivers", "Jordan A. Rivers", "J. Rivers"]

# Local macOS account names that appear in absolute paths, launchd UserName keys and
# log examples (e.g. ["jrivers", "jrivers-mini"]). SEPARATE from OPERATOR_NAMES: those are
# display names the privacy engine redacts from traffic; these are login names that leak
# through committed FILE CONTENT -- `/Users/<you>/...`, a plist <key>UserName</key>, a
# comment naming who owns a file. That gap is not hypothetical: it let three usernames and
# a machine's directory layout reach a release branch on 2026-09-21, past a hook that was
# already scanning for the operator's real name, domain and IP.
OPERATOR_USERNAMES = ["jrivers", "jrivers-mini"]

# A real RFC1918 address from your network, used ONLY as a positive test fixture for the
# private-IP matcher. Any 10./172.16-31./192.168. address is fine.
PRIVATE_TEST_IP = "10.0.0.5"

# LAN host thin clients (e.g. PAI Pi) reach Deflector's GET /pi/models.json /
# OpenAI-compatible endpoints on. Public placeholder defaults to localhost.
PI_HOST = "127.0.0.1"
