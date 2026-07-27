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
"""

from __future__ import annotations

try:
    from private_config import (  # type: ignore
        INTERNAL_DOMAIN,
        OPERATOR_NAMES,
        PRIVATE_TEST_IP,
    )
except ImportError:  # public clone / CI — no private_config.py present
    INTERNAL_DOMAIN = "example-lab.com"
    OPERATOR_NAMES = ["Jordan Rivers", "Jordan A. Rivers", "J. Rivers"]
    PRIVATE_TEST_IP = "10.0.0.5"

__all__ = ["INTERNAL_DOMAIN", "OPERATOR_NAMES", "PRIVATE_TEST_IP"]
