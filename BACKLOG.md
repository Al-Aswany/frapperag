Backlog

## EM-03 Timeout (non-regression)

- **Question:** "List all stock entries of type Transfer for item FAKE-ITEM-ZZZ-9999."
- **Expected:** decline (mode: `decline`, any_of: cannot/not have/only/unable/not able)
- **Observed:** SKIP — timeout after 120.6s. Passed in prior run (9.1s, matched 'cannot').
- **Root cause:** Session-level timeout cascade. Vague/Capability questions in the same run took 69s and 51s respectively, exhausting the session budget before EM-03 could execute.
- **Fix options:**
  1. Increase per-session or per-question timeout beyond 120s.
  2. Run Empty Results in its own isolated session (separate from slow categories).
  3. Investigate why VG-01 (69s) and CA-02 (51s) are slow — may be a sidecar retry/backoff issue.

 
Production needs nginx in front so socket.io actually works and users don't eat the 2s poll floor on every message.
Keep the polling loop even after socket.io works — it's cheap insurance against dropped events from reconnects, worker restarts, and backgrounded tabs.