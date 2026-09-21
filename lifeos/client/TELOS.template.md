# TELOS — <your name>

<!--
TEMPLATE. Copy to ~/.pi/agent/TELOS.md and fill in with your own content.

This file is the SCHEMA only. The skills in ./skills/ parse these headings by name,
so the structure matters; the prose under each heading is yours and is never
committed anywhere. `.gitignore` and the pre-commit hook both refuse a file named
TELOS.md specifically so a filled-in copy cannot be committed by accident.

That separation is the whole design. Every skill reads this document at runtime via
`lifeos_read_telos()` and injects it into the prompt — nothing is duplicated into the
skill files. It means the machinery can be published while the content stays on your
machine, and it means a skill can never drift from the document it claims to be
grounded in.

`lifeos_read_telos()` searches $LIFEOS_TELOS, then ~/.pi/agent/TELOS.md, then
~/.config/USER/TELOS/TELOS.md, and returns NON-ZERO if none is found. Skills call it
under `set -e`, so a missing TELOS aborts the skill rather than producing an
ungrounded answer that looks grounded. That behaviour exists because the opposite
once happened: a hardcoded path meant TELOS-grounded reviews ran ungrounded for five
weeks, and it only surfaced because one skill was articulate enough to write
"Cannot score. TELOS not provided in input" into its own output, where it sat unread.
-->

## Identity

Who you are, in a few lines. Read as general context by every skill; no skill parses
it for structure.

## Values (ranked, use for tiebreakers)

An ordered list. The order is what matters — `decision-helper` and
`future-business-context` break ties with it, so two values in the wrong order
produce confidently wrong advice.

## North star (this life-stage)

One sentence. The thing everything else serves right now.

## Horizons

### 90 days — active bets (pick 3, in order)

Three concrete bets, ordered. `weekly-review-personal` scores progress against these
and will say so when there is none.

### 1 year — <target date>

### 5 years — <target date>

### Lifetime — what an 80-year-old you is proud of

## Non-negotiables (bright lines — never violate)

**The most load-bearing section.** `presence-check` audits daily against it,
`health-score` reads its body-floor thresholds, and `weekly-review-personal` returns a
HELD / AT-RISK / VIOLATED verdict for each line.

Write them as checkable statements, not aspirations — a skill can test "ship one thing
every week" against logged data; it cannot test "be more productive." Include any
numeric floors here rather than in the skills, which deliberately hardcode none.

## Current constraint

The single binding constraint right now. Keeps advice grounded in what is actually
possible this month.

## Failure modes (catch early)

Patterns that precede a bad stretch. `presence-check` watches for sustained evidence
of these and alerts; `weekly-review-personal` reports on them. Describe the early
signal, not the eventual outcome.

## Decision heuristics

The rules you want applied when choices are close. `decision-helper` applies **only**
what is written here — it supplies none of its own — so anything missing simply will
not be considered.

## Problems (the problems in the world you're trying to solve)

## Strategies (how you'll overcome the challenges)

### S1: <name>

### S2: <name>

### S3: <name>

## Wisdom (favorite lessons and aphorisms)

## Voice preferences (how LifeOS skills speak to me)

Tone, length, and bluntness. Applies to every skill's output. Worth being specific:
these skills are terse by default and will stay that way unless told otherwise.
