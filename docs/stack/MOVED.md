# Moved

Two files that lived here are now part of the published LifeOS client kit, so the
repo carries one copy rather than two that drift:

| was | now |
|---|---|
| `docs/stack/lifeos-call.sh` | `lifeos/client/skills/_lib/call.sh` |
| `docs/stack/openjarvis-config.toml` | `lifeos/client/openjarvis-config.toml` |
| `docs/stack/dispatch-skill/` | `lifeos/client/skills/dispatch/` |

They moved rather than being copied on purpose. Both are live client files, and a
second copy under `docs/` would be a snapshot that silently stops matching the one
skills actually source — the failure mode this repo has paid for more than once.

See `lifeos/client/README.md` for how they fit together.
