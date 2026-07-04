# Team Workflow

This directory documents how maintainers and contributors collaborate on
screenscribe. It is **public-safe**: everything here is meant to be read by
anyone working with the project.

## What lives in the public repo

The public repository contains only product-safe material:

- Source code
- Public documentation
- Neutral, reproducible examples
- A public changelog and release notes
- Sanitized tests and fixtures

If you are a public contributor, this is everything you need. You can clone the
repo, run the tests, build the docs, and ship changes without anything else.

## Optional local private workspace

Maintainers **may** optionally attach a local **private context workspace**
(typically a git-ignored `.private/` directory) for internal handoffs,
decisions, debug scenarios, and raw captures. This is a convenience for people
who run the tool against real, non-public material every day.

A few things to be clear about:

- The private workspace is **not part of the public repo**. It is never tracked,
  committed, or published here.
- Public contributors **do not need it**. Nothing in the public workflow depends
  on it.
- Internal context must **never** leak into public code, docs, examples, or
  tests.
- If you are unsure whether something is public-safe, **treat it as private**.

## The boundary

The single most important document here is
[`public-private-boundary.md`](./public-private-boundary.md). Read it before you
add files, write docs, or hand work between machines or agents. It defines
exactly what is public, what is private, and how to route anything you are
unsure about.

## Other documents

- [`agent-handoff-template.md`](./agent-handoff-template.md) — a public-safe
  template for handing a task to a teammate or an AI agent.
- [`release-checklist.md`](./release-checklist.md) — the checklist to run before
  cutting a release.
