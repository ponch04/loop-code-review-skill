# Review dimensions — what counts as an actionable finding

Read this when triaging a finding you're unsure about, or when a reviewer's finding looks like preference dressed as risk. A finding is actionable when it names a **specific location**, a **concrete risk or defect**, and the **evidence** in the repository. Anything else is a comment.

## Comprehensibility and change safety

Test: could a competent engineer without the author reconstruct the change's responsibility, main control/data flow, state transitions, invariants, and failure behaviour from names, types, boundaries and structure?

- Actionable: "`applyDelta` mutates `state.items` in place but callers in `sync.ts` assume immutability — a future retry path would double-apply." Names the symbol, the obstacle, and the maintenance scenario.
- Not actionable: "the function is long", "consider renaming", "domain logic is hard to follow" (essential complexity is not a defect).
- Prefer fixes that make the code explain itself (structure, names, types, seams). Comments are for intent, constraints and non-obvious reasons — not a bandage for opaque code.

## Correctness and operational risk

Behavioural regressions, invalid assumptions, security or privacy exposure, data-integrity problems, poor failure handling, unsafe operational consequences (migrations, retries, idempotency, resource leaks). Must be tied to the scoped change, or to something the scoped change makes worse.

## Test evidence

Passing tests demonstrate execution, not quality. For each test used as evidence:

- Does it exercise the changed behaviour (not just import/instantiate)?
- Would it fail for a plausible regression?
- Does it assert an observable contract rather than implementation detail?
- Are mocks only at real boundaries (network, clock, filesystem), never faking the result under test?

Missing tests are a finding only when the behaviour is testable and the risk warrants coverage. "Tests not needed because <concrete reason>" is an acceptable resolution. Do not chase score-only test polish.

## Reuse and local fit

Before accepting a "should reuse X" finding, the reviewer must name X (component, hook, design-system primitive, shared client/service/util) and state the practical benefit. "Avoid duplication" without a candidate is not a finding. Do not request reuse merely for uniformity.

## Architecture and conventions

Derive boundaries from the scoped code, neighbours, and project guidance (CLAUDE.md, AGENTS.md, ADRs, lint config). A deviation is a finding only when it conflicts with an identifiable rule or precedent. Do not impose an architecture the project doesn't have.

## Score anchors

- **10.0** understandable, safe to inherit, no known in-scope defects, validation complete and green.
- **9.5** no actionable findings; only optional/subjective nits; validation sufficient and green.
- **< 9.5** at least one actionable finding remains, or validation evidence missing/failing.

If score and findings conflict, findings win; treat the number as calibration noise and do not request another pass to reconcile it. If adjudication later invalidates a finding, keep the recorded score and label it pre-adjudication rather than inventing a replacement.

## Handling reviewer output

| Situation | Action |
|---|---|
| Required output missing (no summary, no score) | Ask the same reviewer once |
| Vague concern, no concrete finding | Ask the same reviewer once for specifics; if none, it's not a finding |
| You disagree with a finding | Reply with repo/validation evidence; ask the same reviewer to reconsider |
| Still disputed and material | One fresh adjudicator for that finding only |
| Reviewer can't give a credible review after one clarification | Replace with a fresh reviewer (explicit exception, note it in the report) |
| Scoped files changed mid-review | Discard the review; validate; new pass |
