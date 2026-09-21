# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Loader for operator-private constants.

Import from here, never from `private_config` directly, so the code works whether or not
the (gitignored) real file exists:

  * on the operator's machine, `private_config.py` supplies the real values;
  * on a fresh public clone or in CI, the file is absent and the neutral placeholders
    below take over — so imports succeed and the test suite still passes.

To use your real values: `cp private_config.example.py private_config.py` and edit it.

RESOLVED PER-NAME, NOT ALL-OR-NOTHING. This used to be a single
`from private_config import (A, B, C, D)` inside one try/except ImportError. Adding a new
constant to that tuple broke every operator whose private_config.py predated it: Python
raises ImportError for the ONE missing name, the except branch catches it, and all four
real values are silently replaced by the public placeholders. The privacy engine then runs
against "Jordan Rivers" and "example-lab.com" instead of the operator's real identifiers —
redaction still "works", reports no error, and protects nobody. Observed while adding
OPERATOR_USERNAMES on 2026-09-21.

Per-name `getattr` means a private_config.py written against an older template keeps every
value it does define, and only genuinely absent constants fall back.
"""

from __future__ import annotations

# Neutral public defaults. Also the values a fresh clone or CI sees.
_DEFAULTS: dict[str, object] = {
    "INTERNAL_DOMAIN": "example-lab.com",
    "OPERATOR_NAMES": ["Jordan Rivers", "Jordan A. Rivers", "J. Rivers"],
    "OPERATOR_USERNAMES": ["jrivers", "jrivers-mini"],
    "PRIVATE_TEST_IP": "10.0.0.5",
    "PI_HOST": "127.0.0.1",
}

try:
    import private_config as _pc  # type: ignore
except ImportError:  # public clone / CI — no private_config.py present
    _pc = None

INTERNAL_DOMAIN: str = getattr(_pc, "INTERNAL_DOMAIN", _DEFAULTS["INTERNAL_DOMAIN"])
OPERATOR_NAMES: list[str] = getattr(_pc, "OPERATOR_NAMES", _DEFAULTS["OPERATOR_NAMES"])
OPERATOR_USERNAMES: list[str] = getattr(
    _pc, "OPERATOR_USERNAMES", _DEFAULTS["OPERATOR_USERNAMES"]
)
PRIVATE_TEST_IP: str = getattr(_pc, "PRIVATE_TEST_IP", _DEFAULTS["PRIVATE_TEST_IP"])
PI_HOST: str = getattr(_pc, "PI_HOST", _DEFAULTS["PI_HOST"])


def using_defaults() -> list[str]:
    """Names still resolving to the PUBLIC placeholder rather than a real value.

    Exists so the silent degradation described above is at least *inspectable*: a caller
    that cares (a privacy self-test, a startup check) can ask rather than assume. An empty
    list on the operator's box means private_config.py covers everything; a non-empty list
    on a fresh clone is expected and fine.
    """
    return [
        name
        for name, default in _DEFAULTS.items()
        if globals()[name] == default and getattr(_pc, name, None) is None
    ]


__all__ = [
    "INTERNAL_DOMAIN",
    "OPERATOR_NAMES",
    "OPERATOR_USERNAMES",
    "PI_HOST",
    "PRIVATE_TEST_IP",
    "using_defaults",
]
