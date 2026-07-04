# Definition of Done

A change is **done** only when the gate that proves it is green has actually
been re-run against the current state — not when the code is written, and not
when an earlier run was green before the last edit.

## The checklist

- [ ] **Verifier green, freshly.** `make verify` (which runs
      `scripts/ss_verify.py`) prints `RESULT: READY` on the **current** tree,
      after the last commit. Do not carry a stale READY from an earlier state.
      If the gate has not been re-run since the last change, the honest status
      is "focused gates green; full gate pending" — not "done".
- [ ] **Runtime-affecting changes carry a runtime witness.** A change that
      touches behaviour — Python *or* the served JavaScript — is backed by a
      test that exercises the real path, not just a mechanism in isolation. For
      JS, that is the F0 node-vm canary in `tests/test_f0_js_runtime_smoke.py`;
      for browser-level behaviour, the `tests/e2e` suite (`--run-e2e`).
- [ ] **Propagation gate.** When a fix must reach the user through a built
      artifact (wheel, inlined asset, generated report), verify it in the
      **served** output — not only in the repo source. A committed fix is not a
      shipped fix until it lands in what the user receives.
- [ ] **No silent skips on the critical surface.** A gate that would skip for a
      missing tool fails-closed under CI instead (e.g. the JS canary requires
      `node` under `CI=true`). A skip is only acceptable where it is explicitly
      declared safe (e.g. local development).
- [ ] **Scope declared, not assumed.** What the gate measures and what it does
      not is written down (see [COVERAGE_SCOPE.md](COVERAGE_SCOPE.md)). An
      undeclared gap is a defect.

## Why "[x] only when the verifier is green"

Marking a box because the work *should* pass is how a cold reader — human or
agent — repeats finished work or ships a regression. The box tracks **runtime
truth**: it flips when the gate, re-run on the current state, says so. Over-
claiming READY without re-running the gate is the failure this document exists
to prevent.
