# Fix Phase Backlog

Deferred items from rigor-mode pre-mortems during the Phase 1 + Phase 2 investigation.

---

## From Phase 1 critic

- [ ] Wrap `refresh_all_clients_sync` in `asyncio.wait_for(timeout=25.0)` to guarantee
  response under Render's 30s ceiling. Currently a slow upstream fetch can cause the
  refresh endpoint to time out without a user-visible error.

- [ ] Run `TARGET_URL` e2e suite against the live URL post-deploy to confirm behaviour
  on real slow fetches (mock mode cannot reproduce timing-dependent paths).

---

## From Phase 2 diagnostician

- [ ] Harden `_fetch_campaign_daily` error handling — replace the blanket
  `# noqa: BLE001` exception swallow with explicit exception class catching +
  propagation to the cache layer so failed campaigns surface as error cards instead
  of silently dropping their `sent_today` share.

  Current behaviour: any exception → `{"sent": 0, ...}` returned silently, warning
  logged only. A 429 burst or Instantly outage would silently zero individual campaign
  contributions and produce exactly the kind of `sent_today` divergence that was
  originally reported as a bug.

  Suggested approach (Option A — minimal): re-raise on exception, let
  `asyncio.gather(return_exceptions=True)` at line 281 catch it, add a
  `partial_fetch=True` flag on the workspace dict that `monitoring_view` renders
  as a warning badge. Option B (bigger): return sentinel `{"sent": None}` and
  filter at aggregation with an explicit partial-data UI indicator.
