# Contributing to Privacy Deflector

Thanks for your interest. Privacy Deflector is a privacy-and-efficiency gate for LLM traffic, and
contributions of all sizes are welcome — bug fixes, tests, docs, new privacy detectors, routing
tiers, or efficiency heuristics.

## Contribution model

This is a single-maintainer project. There's no formal contributor pipeline, but the door's open:

| Your submission | Commitment |
|---|---|
| Follows the format below | ~1 month SLA for review |
| Doesn't follow the format | Backlog — reviewed as bandwidth allows |
| Bug fixes / obvious, small, well-tested fixes | Prioritized |

**Format that gets prioritized review:**

- **Title:** `[component] short description` — e.g. `[privacy] fix tier_a boundary regex on unicode input`
- **Branch:** `feat/<short-desc>` or `fix/<short-desc>`
- **Commits:** one logical commit per change, no `WIP`/squash-later history
- **PR body:** use `.github/pull_request_template.md` (What changed / Why it matters / Testing done / Files changed)

If you need something merged faster than the SLA, fork and maintain your own branch — MPL-2.0
lets you keep downstream modifications private; only changes to *this repo's* files need to come
back under MPL-2.0 if redistributed.

## License of contributions

Privacy Deflector is licensed under the **Mozilla Public License 2.0** (see `LICENSE`). By submitting a
contribution you agree it is licensed under MPL-2.0. MPL is file-level copyleft: changes to
this repo's own source files stay open under MPL, but the license does not restrict combining
Privacy Deflector with other code in a larger work.

New source files should carry the MPL header:

```python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
```

## Never commit personal data

Privacy Deflector is a privacy tool — keep it clean of real identifiers.

- Operator-specific values (your internal domain, names, private IPs) live **only** in
  `private_config.py`, which is **gitignored**. Copy it from the template:
  ```bash
  cp private_config.example.py private_config.py
  ```
- Code and tests read these values through `private_ref.py`, which falls back to neutral
  placeholders when `private_config.py` is absent — so a fresh clone and CI work with no secrets.
  **Never hardcode a real domain / name / IP** in source or tests; reference `private_ref` instead.
- Install the pre-commit guard, which refuses to commit `private_config.py` or any staged line
  containing your real identifiers:
  ```bash
  git config core.hooksPath scripts/hooks
  ```

## Development setup

Requires Python 3.14 and [`uv`](https://docs.astral.sh/uv/) (use `uv pip`, never `pip`).

```bash
cp private_config.example.py private_config.py     # then edit
git config core.hooksPath scripts/hooks
uv venv --python 3.14 .venv
uv pip install -p .venv/bin/python fastapi uvicorn httpx pyyaml tiktoken
# optional Tier C PII: uv pip install -p .venv/bin/python presidio-analyzer && \
#   .venv/bin/python -m spacy download en_core_web_lg
```

## Tests

Keep the suite green and add tests for new behavior:

```bash
.venv/bin/python -m pytest -q      # 48 passing
```

Tests must pass **with and without** `private_config.py` present (the placeholder fallback path is
what CI exercises).

## Pull requests

1. Fork and branch from `main` using `feat/<desc>` or `fix/<desc>`.
2. Make focused changes; match the style, naming, and comment density of the surrounding code.
3. Add or update tests; run the full suite.
4. Fill out `.github/pull_request_template.md` — title as `[component] short description`.
5. One logical commit per change; avoid `WIP` or squash-later history.

Properly formatted PRs get a ~1 month review SLA; others land in the backlog. See
[Contribution model](#contribution-model) above.

## Reporting privacy or security issues

If you find a way for sensitive data to slip past the privacy gate, or any other security issue,
please report it privately to the maintainer rather than opening a public issue.
