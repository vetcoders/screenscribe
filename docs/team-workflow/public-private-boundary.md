# Public / Private Boundary

This is the most important document in the team workflow. It defines what may
live in the public repository, what must stay in private context, and how to
route anything you are unsure about.

The goal is durable: **internal context must never leak into the public product.**

---

## The one rule

> **If you are unsure whether something is public-safe, treat it as private.**

When in doubt, do not commit it to the public repo. Route it to private context
or local storage instead. It is always cheaper to move something into public
later than to scrub a leak after it ships.

---

## Three places things can live

### 1. Public repo

Product-safe material that is meant to be seen by anyone:

- Source code intended for release
- Public documentation (README, usage, governance)
- Neutral, reproducible examples
- Public changelog and release notes
- Sanitized tests and fixtures
- Contribution, security, and code-of-conduct docs

**Never** belongs in the public repo (route it to private context or local
storage instead):

- Real logs, recordings, or screen/audio/video captures
- Zip bundles, raw exports, or generated debug outputs
- Private project recipes or runbooks
- Gold fixtures or real session data
- Private prompts and agent scratchpads
- Release-room notes and internal decisions
- Local machine paths and private machine names
- Private product codenames
- Customer or user data

### 2. Private context (`.private/context/`)

Internal working material that supports the product but is not part of it:

- Agent handoffs
- Release-room notes
- Decisions and rationale
- Private recipes and runbooks
- Real debug scenarios
- Real fixtures and sample inputs
- Evaluation reports
- Incident notes
- Private prompts
- Internal architecture notes

A `.private/context/` workspace typically organizes this material as:

```
.private/context/
  agents/        # agent scratchpads and per-agent working notes
  releases/      # release-room notes and cut logs
  decisions/     # decisions and rationale
  recipes/       # private recipes and runbooks
  fixtures/      # real fixtures and sample inputs
  incidents/     # incident notes and postmortems
  inbox/         # per-machine / per-agent handoff drops
  data/          # small private datasets referenced by the above
```

### 3. Raw / local storage (`private/` or external)

Heavy or sensitive raw material that should never enter version control at all:

- Logs
- Recordings
- Audio / video captures
- Zip bundles
- Screenshots
- Generated debug outputs

---

## Routing rules

Use these rules to decide where a new artifact belongs:

| If it is...                                    | Route it to...               |
| ---------------------------------------------- | ---------------------------- |
| Public code, docs, or neutral examples         | the public repo              |
| Internal analysis, handoffs, decisions         | `.private/context/`          |
| Raw logs, media, or bundles                    | `private/` or local storage  |

**Never** write project-specific or internal context into public docs,
examples, or tests. Public surfaces describe the product in general, neutral
terms only.

The same rules stated for agents, compactly:

```
Public code/docs/examples   -> public repo.
Internal analysis/handoff/decisions -> .private/context.
Raw logs/media/bundles      -> private/local storage.
Never write project-specific context into public docs/examples/tests.
If unsure, treat as private.
```

---

## Multi-agent inbox rule

When multiple machines or agents drop work into private context, each writes to
its own timestamped file:

```
.private/context/inbox/<machine-or-agent>/<timestamp>-<topic>.md
```

For example:

```
.private/context/inbox/laptop/2026-06-05T120000Z-release-audit.md
.private/context/inbox/workstation/2026-06-05T121500Z-debug-handoff.md
```

A human or curator agent can later consolidate these inbox notes into canonical
documents.

- An agent creates a **new** timestamped file by default.
- An agent does **not** edit a shared document without an explicit instruction
  to do so.

This keeps handoffs append-only and conflict-free, and makes it obvious who
wrote what and when.

---

## Git tracking

`.private/` is **git-ignored** and is **never** tracked by the public repo.

`.private/context/` may itself be a **separate private git repository** with its
own history. Either way, none of it is ever committed to or published from the
public repo.

Before adding a new top-level directory, confirm it is covered by `.gitignore`
if it is meant to be private.
