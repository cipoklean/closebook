# NOTES.md — live-sandbox discrepancies & learnings

Per closebook.md §12: when a sandbox call behaves differently from the spec,
write a failing test that captures the real behaviour, adapt, and note it here.

## 2026-10-06
- (pending first live run) — response shapes for GET /balances/current and
  GET /fx/rates/current are normalized defensively in the module wrappers;
  verify against the live sandbox and delete the defensive branches if
  unnecessary.
