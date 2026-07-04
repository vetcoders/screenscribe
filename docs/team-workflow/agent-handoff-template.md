# Agent Handoff Template

Use this template to hand a task to a teammate or an AI agent. Keep it
**public-safe**: describe the work in neutral terms, and never paste internal
context, real captures, or sensitive data into a handoff that lives in the
public repo. If the task involves private material, store the handoff in private
context instead (see [`public-private-boundary.md`](./public-private-boundary.md)).

Copy the section below and fill it in.

---

## Task

A one- or two-sentence summary of what needs to be done and why.

## Repo / branch

- Repo: `<repository>`
- Base branch: `<base-branch>`
- Working branch: `<working-branch>`

## Files touched

List the files created, modified, or removed:

- `path/to/file` — what changed and why

## Summary

A short narrative of what was done and the current state of the work.

## Validation

How the work was checked. Include the commands run and their outcome:

- Tests: `<command>` — result
- Lint: `<command>` — result
- Typecheck: `<command>` — result
- Manual checks: what was verified by hand

## Risks / open questions

Anything that could break, regress, or surprise the next person, plus any
unresolved questions for the receiver:

- Risk — likelihood / impact / mitigation

## Next steps

What the receiver should do next, in order:

1. ...
2. ...

## Private context location (if applicable)

If there is supporting private material (real debug scenarios, raw captures,
internal notes), point to where it lives. Use a placeholder, **not** a real URL
or path:

- Private context: `<private-context-location>`

> Do not paste private content into this handoff if it will live in the public
> repo. Reference it by location only.
