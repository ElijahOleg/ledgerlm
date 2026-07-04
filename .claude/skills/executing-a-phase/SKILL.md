---
name: executing-a-phase
description: LedgerLM's phase-gated build workflow. Use at the START of every session on this repo and whenever asked to execute, continue, or resume a phase, work toward a gate, or "continue the build" — even if the request just names a feature that belongs to a phase. Covers scoping work to the current phase, the deviation protocol, exit-criteria verification, the gate report format, and the hard-stop rule.
---

# Executing a phase

This repo is built under a contract: DESIGN.md defines phases with gates, and a human
reviews at every hard gate. The gates only work if each session builds exactly its
phase and reports honestly. Scope creep here doesn't just waste time — it dumps
unreviewed work on the human and erodes the contract that makes the whole build
trustworthy.

## Before writing any code

1. Read DESIGN.md end to end. §9 defines the current phase's scope and exit criteria;
   §3 lists principles enforced in every phase; §13 is the decision log.
2. Read CLAUDE.md for the current-phase marker and any in-flight notes.
3. If the marker, DESIGN.md, and the user's instruction disagree about which phase is
   current, ask before proceeding.
4. Briefly restate to the user the phase goal and the exit criteria being built toward.

## Scope discipline

Build only what the current phase's **Build** list specifies. The main failure mode is
"quickly also" adding later-phase items or unrequested polish — resist it, and park
such ideas in the gate report's Notes section instead.

If in-scope work genuinely cannot be completed as specified:
- **Architectural fork** (the spec'd approach is wrong or impossible): stop and ask.
- **Tactical mismatch** (small local change makes it work): implement the minimal
  deviation and record it via the deviation protocol.

## Deviation protocol

A deviation is anything shipped that differs from DESIGN.md — a schema tweak, a
renamed command, an added dependency, altered semantics. For each one, record: what
changed, why the spec'd version didn't work, and a proposed Decision Log row (one
sentence of decision + one of rationale). Never deviate silently: an unrecorded
deviation damages the contract more than the deviation itself.

## While building

- Small conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), each
  leaving the tree green.
- Run `ruff check` and `pytest` after each meaningful unit of work; never end a
  session red.
- Tests never touch the network. Anything needing live API keys is env-gated and
  skipped by default.

## Verifying exit criteria

Run each exit criterion literally, the way a user would (fresh shell / clean venv
where the criterion implies it). Capture the command and trimmed output as evidence.
A criterion that cannot pass is a blocker to raise at the gate — not a criterion to
reinterpret.

## Gate report — produce exactly this, then stop

```
## Gate N report — <phase name>

### Built
<one line per deliverable, with key commit hashes>

### Exit criteria — evidence
<criterion → command → result, one block per criterion>

### Deviations from DESIGN.md
<"none" | numbered list, each with its proposed Decision Log row>

### Notes for next phase
<parked ideas, risks spotted, TODOs deferred by design>
```

## Gate types

- **Hard gate** (Phases 1, 1.5, 2, 3, 4): after the report, STOP. Do not start the
  next phase; do not refactor "while waiting". Update CLAUDE.md's marker to
  "awaiting Gate N review".
- **Soft gate** (Phase 0 only): self-verify the exit criteria, commit, and continue
  into Phase 1 in the same session.

## After the human approves a gate

1. Append accepted deviations to DESIGN.md §13 (next D-number) and make any agreed
   section edits; commit as `docs(design): ...`.
2. If review showed a skill in .claude/skills/ gave wrong or missing guidance, fix
   the skill in the same commit wave — the harness is contract, like DESIGN.md.
3. Update CLAUDE.md's marker to the next phase. Only then may the next phase begin,
   normally in a fresh session.
