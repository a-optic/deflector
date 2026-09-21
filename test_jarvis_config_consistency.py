# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""config.yaml's model-id lists must not drift from each other.

Two real bugs motivated this: `jarvis_models` named `lfm2.5:8b` while every
other reference to the tier-A model (including config.yaml's own pi_clients
dropdown, 130 lines away) said `lfm2.5:latest` -- nothing ever requested the
`:8b` form, so the drift was invisible until read closely. And
`jarvis_cloud_fallback` named a model that had already been dropped from
`jarvis_models` (`glm-4.7-flash`) after a config edit. Neither drift was a
runtime error; both were "config that describes a system slightly different
from the one running."

This does not assert every model appears in the Pi dropdown -- some routing
tiers are intentionally not directly selectable (qwen3.5:4b, for instance,
predates this test and is still Jarvis-internal only). It only pins the two
concrete invariants that actually broke.

Offline. Run: .venv/bin/python -m pytest test_jarvis_config_consistency.py -q
"""

import re

import main

RT = main.RT


class TestFallbackIsAConfiguredTier:
    def test_jarvis_cloud_fallback_is_a_member_of_jarvis_models(self):
        assert RT["jarvis_cloud_fallback"] in RT["jarvis_models"]


class TestNoLatestVersusExplicitTagDuplicates:
    """The `repo:tag` split means `foo:latest` and `foo:8b` look unrelated to
    a diff but name the same family -- exactly the shape lfm2.5 drifted into.

    NARROWED 2026-09-20: the original check rejected ANY two tags sharing a
    repo, which also rejects legitimately distinct size variants. `qwen3.5:4b`
    and `qwen3.5:9b` are different models (3.4GB vs 6.6GB, both installed and
    separately benchmarked), not a stale duplicate.

    The drift this exists to catch is ALIASING: `:latest` resolves to one of
    the explicit tags, so listing both means one entry is redundant and may be
    silently stale. Two explicit, different tags cannot alias each other, so
    they are allowed. The original lfm2.5 bug (`:latest` + `:8b`) still fires.
    """

    def _repo(self, model_id: str) -> str:
        return re.split(r"[:/]", model_id, maxsplit=1)[0] if ":" in model_id else model_id

    def _tag(self, model_id: str) -> str:
        return model_id.split(":", 1)[1] if ":" in model_id else "latest"

    def test_jarvis_models_has_no_latest_plus_explicit_tag_alias(self):
        by_repo: dict[str, list[str]] = {}
        for model_id in RT["jarvis_models"]:
            by_repo.setdefault(self._repo(model_id), []).append(model_id)

        for repo, ids in by_repo.items():
            if len(ids) < 2:
                continue
            aliasing = [m for m in ids if self._tag(m) == "latest"]
            assert not aliasing, (
                f"{aliasing!r} uses the ':latest' alias alongside explicitly "
                f"tagged {[m for m in ids if m not in aliasing]!r} for repo "
                f"{repo!r} -- ':latest' resolves to one of them, so one entry "
                f"is redundant and may be stale (this is the lfm2.5 bug)")
