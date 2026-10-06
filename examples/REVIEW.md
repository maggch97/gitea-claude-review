# Review rules

<!-- Copy to .gitea/claude/REVIEW.md in your repository and adapt.
     The action reads this file from the reviewed checkout on every run. -->

Report only problems that matter for the merge decision, highest first:

1. Bugs: wrong behaviour, crashes, data loss, leaks, races, swallowed errors,
   broken edge cases.
2. Regression risk in configuration, build scripts, CI, public APIs and
   persisted formats.
3. Security: injection, path traversal, unsafe deserialization, secrets in
   code, missing authorization checks.
4. Maintainability issues with a concrete cost. No style or naming nits.

Project conventions (examples — replace with yours):

- Fail fast: missing or invalid required data must raise, not fall back to a
  silent default.
- Units must be explicit in names when values cross module boundaries
  (`lengthMm`, `timeoutMs`).
- Do not open generated output or vendored code (`build/**`, `vendor/**`).
