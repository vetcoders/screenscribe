# Release Checklist

Run through this checklist before cutting a release. The aim is a clean,
public-safe release with no internal context, no raw material, and a fresh,
reviewable history.

## Quality gates

- [ ] **Tests** pass on a clean checkout.
- [ ] **Lint** passes with no warnings.
- [ ] **Typecheck** passes.
- [ ] **Security scan** passes (dependency audit and static analysis).
- [ ] **Coverage check** passes (if a coverage gate applies).

## Documentation

- [ ] **Docs check** — README and usage reflect the released behavior and
      version.
- [ ] **Changelog updated** — the release entry is present and accurate.
- [ ] Public examples still run and produce the documented output.

## Leak and hygiene scan

- [ ] **Leak scan** — no private names, internal URLs, credentials, tokens, or
      project-specific data anywhere in the release surface.
- [ ] **Fresh-history check** — the release branch has a clean, intentional
      commit history with no stray or experimental commits.
- [ ] **No raw logs or media** — no logs, recordings, audio/video captures,
      screenshots, or zip bundles are committed.
- [ ] **No private context** — nothing from `.private/` (handoffs, decisions,
      private prompts, real fixtures, eval or incident notes) is included.
- [ ] **No internal agent notes** — no agent scratchpads, handoffs, or
      operator-only working notes are committed.
- [ ] **No local machine paths** — no absolute user/home paths or private
      machine names appear in tracked files.
- [ ] **No secret-like placeholders** — no values that look like real keys or
      tokens; placeholders are obviously fake.
- [ ] **No old branch history** — no leftover content carried over from
      abandoned or internal branches.
- [ ] **No internal artifacts** — no generated debug outputs, internal tooling
      config, or operator-only files.

## Final confirmation

- [ ] If anything above is uncertain, it was treated as private and excluded.
- [ ] Version number and release notes are correct and final.
- [ ] A fresh clone of the release builds and runs from scratch.
