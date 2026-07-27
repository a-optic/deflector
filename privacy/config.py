# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Privacy config loader (v4.0 §1).

Reads the two operator-owned files:
  ~/.agentstop/provider-trust.yaml   trust classes (trusted / restricted)
  ~/.agentstop/redact-list.yaml      static exact-value redaction entries (Tier A)

Both are watched by mtime and recompiled on change. Local-only, never transmitted.

Destinations are provider labels, not model ids:
  "local"        implicitly fully trusted (data never leaves the box)
  "ollama-cloud" trusted   -> redact + rehydrate
  "anthropic"    restricted -> reroute off cloud when carrying static identity
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

import yaml

from privacy.tier_a import CompiledEntry, compile_tier_a

LOCAL = "local"

_DIR = pathlib.Path(os.path.expanduser("~/.agentstop"))
TRUST_PATH = _DIR / "provider-trust.yaml"
REDACT_PATH = _DIR / "redact-list.yaml"


@dataclass
class PrivacyConfig:
    trusted: frozenset[str] = frozenset()
    restricted: frozenset[str] = frozenset()
    redact_list: list[CompiledEntry] = field(default_factory=list)
    tier_d_enabled: bool = False  # global default; a request header may opt in per-call

    def is_cloud(self, destination: str) -> bool:
        """Any non-local provider. Redaction tiers B-D run only on the cloud path."""
        return destination != LOCAL

    def is_restricted(self, destination: str) -> bool:
        return destination in self.restricted

    def is_trusted(self, destination: str) -> bool:
        return destination in self.trusted


# --- mtime-watched cache -------------------------------------------------------

_cache: PrivacyConfig | None = None
_mtimes: tuple[float, float] | None = None


def _current_mtimes() -> tuple[float, float]:
    def mt(p: pathlib.Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return (mt(TRUST_PATH), mt(REDACT_PATH))


def _load() -> PrivacyConfig:
    trust_doc: dict = {}
    if TRUST_PATH.exists():
        trust_doc = yaml.safe_load(TRUST_PATH.read_text()) or {}
    pt = (trust_doc.get("provider_trust") or {})
    trusted = frozenset(pt.get("trusted") or [])
    restricted = frozenset(pt.get("restricted") or [])

    redact_doc: dict = {}
    if REDACT_PATH.exists():
        redact_doc = yaml.safe_load(REDACT_PATH.read_text()) or {}
    entries = compile_tier_a(redact_doc.get("entries") or [])

    return PrivacyConfig(trusted=trusted, restricted=restricted, redact_list=entries)


def get_config() -> PrivacyConfig:
    """Return the current config, reloading iff either file changed on disk."""
    global _cache, _mtimes
    now = _current_mtimes()
    if _cache is None or now != _mtimes:
        _cache = _load()
        _mtimes = now
    return _cache
