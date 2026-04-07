"""Phase-2 integration tests (T8).

Exercises the full monitoring stack end-to-end without hitting any real APIs:

  1. **Mock mode**: registry → cache → get_all_monitoring_data() serves the
     fixture through the exact same enrichment pipeline as live data.
  2. **Scheduler registration**: init_monitoring_poller(...) adds the refresh
     job to a real AsyncIOScheduler with the right interval/coalesce/max_instances.
  3. **Config load/save/validate cycle**: factory → validate → save → reload
     → get_client_kpi/get_client_thresholds reflect the saved state; the
     save hook invalidates the cache.
  4. **Scheduler dispatches to fetchers** with mocked Instantly/EB fetchers
     and verifies that 9 clients are fetched, Phase 2 backfills are spawned
     only for Instantly, and each backfill carries the correct generation.
  5. **Stale backfill rejection** end-to-end: Phase 1 ran twice (gen → 2),
     a Phase 2 backfill submitted with gen=1 is dropped without mutating
     the cache.

These tests use the real modules (no monkey-patching of internals beyond
the `_FETCHERS` dispatch table, which exists precisely for this purpose)
and the real registry + config loader. They do not start the scheduler,
because starting APScheduler's `AsyncIOScheduler` inside a pytest event
loop has known interaction quirks — we assert on job registration + the
plain coroutine instead.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services import (
    monitoring_cache as cache,
    monitoring_config as mc,
    monitoring_poller as mp,
    registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_state(tmp_path: Path):
    """Reset all monitoring state between tests to prevent cross-contamination."""
    registry.load_from_env()

    mc._reset_for_tests()
    # Load a factory config against a non-existent path so no disk state leaks in.
    mc.load_config(config_path=tmp_path / "does_not_exist.json")

    cache._reset_for_tests()
    mp._reset_for_tests()
    # Also restore the real fetchers in case a previous test monkey-patched them.
    from app.api.monitoring_instantly import fetch_instantly_data
    from app.api.monitoring_emailbison import fetch_emailbison_data
    mp._FETCHERS["instantly"] = fetch_instantly_data
    mp._FETCHERS["emailbison"] = fetch_emailbison_data

    yield

    cache._reset_for_tests()
    mp._reset_for_tests()
    mc._reset_for_tests()


def _fake_instantly_result() -> dict:
    """A ClientData-shaped dict matching what fetch_instantly_data returns."""
    return {
        "platform": "instantly",
        "active_campaigns": 1,
        "total_campaigns": 1,
        "sent_today": 1000,
        "first_touch_today": 600,
        "followup_today": 400,
        "replies_today": 14,
        "opps_today": 1,
        "reply_rate_today": 1.4,
        "reply_rate_7d": 1.3,
        "bounce_rate": 1.5,
        "not_contacted": 20000,
        "in_progress": 5000,
        "avg_sent_7d": 1000.0,
        "avg_replies_7d": 14.0,
        "avg_opps_7d": 1.0,
        "opp_trend": "flat",
        "reply_trend": "flat",
        "sent_trend": "flat",
        "campaigns": [
            {
                "name": "c1", "id": "cid1", "status": "active",
                "sent_today": 1000, "first_touch": 600, "followups": 400,
                "replies_today": 14, "opps_today": 1, "reply_rate": 1.4,
                "not_contacted": 20000, "in_progress": 5000,
                "total_sent": 50000, "total_leads": 100000,
                "total_completed": 60000, "total_bounced": 1000,
            },
        ],
        "daily": [],
        "_nc_backfill": ["cid1"],
        "_nc_api_key":  "fake-key",
    }


def _fake_eb_result() -> dict:
    return {
        "platform": "emailbison",
        "active_campaigns": 1,
        "total_campaigns": 1,
        "sent_today": 500,
        "first_touch_today": 0,
        "followup_today": 0,
        "replies_today": 7,
        "opps_today": 1,
        "reply_rate_today": 1.4,
        "reply_rate_7d": 1.3,
        "bounce_rate": 1.5,
        "not_contacted": 10000,
        "in_progress": None,
        "avg_sent_7d": 500.0,
        "avg_replies_7d": 7.0,
        "avg_opps_7d": 1.0,
        "opp_trend": "flat",
        "reply_trend": "flat",
        "sent_trend": "flat",
        "campaigns": [
            {
                "name": "eb1", "id": "ebc1", "status": "active",
                "sent_today": 500, "first_touch": 0, "followups": 0,
                "replies_today": 7, "opps_today": 1, "reply_rate": 1.4,
                "not_contacted": 0, "in_progress": 0,
                "total_sent": 0, "total_bounced": 150,
            },
        ],
        "daily": [],
    }


# ---------------------------------------------------------------------------
# 1. Mock mode end-to-end
# ---------------------------------------------------------------------------

def test_mock_mode_serves_all_nine_clients_through_enrichment_pipeline():
    """registry → cache → get_all_monitoring_data serves the fixture."""
    cache.set_mock_mode(True)
    try:
        result = cache.get_all_monitoring_data()
    finally:
        cache.set_mock_mode(False)

    # Every registered monitoring workspace shows up
    expected_names = {e.name for e in registry.list_monitoring_workspaces()}
    assert set(result.keys()) == expected_names
    assert len(result) == 9

    # Every entry is enriched with kpi / thresholds / platform / status
    for name, entry in result.items():
        assert "kpi" in entry, f"{name} missing kpi"
        assert "thresholds" in entry, f"{name} missing thresholds"
        assert "platform" in entry, f"{name} missing platform"
        assert "status" in entry, f"{name} missing status"


def test_mock_mode_classifications_match_fixture_edge_cases():
    """Each fixture client should classify to the documented T1 edge-case status."""
    cache.set_mock_mode(True)
    try:
        result = cache.get_all_monitoring_data()
    finally:
        cache.set_mock_mode(False)

    expected = {
        "SwishFunding":       "green",
        "MyPlace":            "amber",  # sent 1300/2000 < sent_pct_warn 0.8
        "SmartMatchApp":      "red",    # active > 0 AND sent_today = 0
        "HeyReach":           "amber",  # reply_rate 0.68 < reply_rate_warn 1.0
        "Kayse":              "red",    # bounce 6.4 > bounce_rate_red 5.0
        "Enavra":             "amber",  # pool_days ~4.8 < pool_days_warn 7
        "RankZero":           "green",
        "SwishFunding (EB)":  "amber",  # active = 0 AND total > 0
    }
    for name, want in expected.items():
        assert result[name]["status"] == want, f"{name}: expected {want}, got {result[name]['status']}"

    # Prosperly is the error client in the fixture
    assert result["Prosperly"]["status"] == "error"
    assert result["Prosperly"]["error"] == "Invalid API key"


def test_mock_mode_threshold_override_reflects_immediately():
    """Saving a new threshold changes the classification on the next read."""
    # Baseline: HeyReach is amber (reply_rate 0.68 below warn 1.0)
    cache.set_mock_mode(True)
    try:
        out = cache.get_all_monitoring_data()
        assert out["HeyReach"]["status"] == "amber"

        # Lower the warn threshold below HeyReach's reply_rate so it should go green
        new_cfg = mc._factory_config()
        new_cfg["global_thresholds"]["reply_rate_warn"] = 0.5
        new_cfg["global_thresholds"]["reply_rate_red"] = 0.3
        with tempfile.TemporaryDirectory() as td:
            mc.save_config(new_cfg, config_path=Path(td) / "cfg.json")

        # Next read should reclassify using the new thresholds
        out2 = cache.get_all_monitoring_data()
        # HeyReach reply rate is 0.68 which is now above the new 0.5 warn
        # threshold. Bounce 2.3 < 3.0 warn. Pool days = 14500/1900 ≈ 7.6 > 7
        # so rule 10 doesn't fire. Should be green now.
        assert out2["HeyReach"]["status"] == "green"
    finally:
        cache.set_mock_mode(False)


# ---------------------------------------------------------------------------
# 2. Scheduler registration
# ---------------------------------------------------------------------------

def test_scheduler_registers_monitoring_refresh_job():
    """init_monitoring_poller adds the interval job with the right config."""
    sched = AsyncIOScheduler()
    mp.init_monitoring_poller(sched)

    job = sched.get_job(mp.JOB_ID)
    assert job is not None, "monitoring_refresh job not registered"
    assert job.trigger.interval.total_seconds() == mp.DEFAULT_INTERVAL_SECONDS
    assert job.coalesce is True
    assert job.max_instances == 1


def test_scheduler_init_is_idempotent():
    """Calling init twice must not create duplicate jobs."""
    sched = AsyncIOScheduler()
    mp.init_monitoring_poller(sched)
    mp.init_monitoring_poller(sched)

    jobs_with_id = [j for j in sched.get_jobs() if j.id == mp.JOB_ID]
    assert len(jobs_with_id) == 1


def test_init_creates_shared_httpx_client_and_registers_save_hook():
    """init_monitoring_poller creates the client and wires cache.init_cache()."""
    sched = AsyncIOScheduler()
    assert mp._http_client is None
    assert len(mc._save_hooks) == 0

    mp.init_monitoring_poller(sched)

    assert isinstance(mp._http_client, httpx.AsyncClient)
    assert len(mc._save_hooks) == 1  # cache's _on_config_saved hook


@pytest.mark.asyncio
async def test_shutdown_cleans_up_client_and_cancels_inflight_backfills():
    """shutdown closes the httpx client and cancels pending backfill tasks."""
    sched = AsyncIOScheduler()
    mp.init_monitoring_poller(sched)
    assert mp._http_client is not None

    # Spawn a long-running fake backfill so shutdown has something to cancel
    async def slow():
        await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    mp._backfill_tasks.add(task)

    await mp.shutdown_monitoring_poller()

    assert mp._http_client is None
    assert len(mp._backfill_tasks) == 0
    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# 3. Config load / save / validate cycle
# ---------------------------------------------------------------------------

def test_config_factory_validates_and_round_trips(tmp_path: Path):
    """Factory defaults pass validation; save/load round-trips with per-client override."""
    cfg = mc._factory_config()
    ok, errors = mc.validate_config(cfg)
    assert ok, f"factory config failed validation: {errors}"
    # Add a per-client threshold override
    cfg["clients"]["Kayse"]["thresholds"] = {"bounce_rate_red": 8.0}

    cfg_path = tmp_path / "dashboard_config.json"
    mc.save_config(cfg, config_path=cfg_path)
    assert cfg_path.exists()
    assert not cfg_path.with_suffix(".tmp").exists()

    # Re-load and verify the override survived
    mc._reset_for_tests()
    mc.load_config(config_path=cfg_path)
    thr = mc.get_client_thresholds("Kayse")
    assert thr["bounce_rate_red"] == 8.0
    # Global thresholds still resolve (from the factory defaults in the loaded file)
    assert thr["reply_rate_warn"] == 1.0


def test_config_consistency_validation_rules():
    """All four consistency rules fire when violated."""
    bad = {
        "global_thresholds": {
            "reply_rate_warn": 0.3, "reply_rate_red": 0.5,   # warn must be > red
            "sent_pct_warn":   0.4, "sent_pct_red":   0.6,   # warn must be > red
            "bounce_rate_warn": 6.0, "bounce_rate_red": 3.0,  # warn must be < red
            "pool_days_warn":  2,    "pool_days_red":   5,    # warn must be > red
        },
        "clients": {},
    }
    ok, errors = mc.validate_config(bad)
    assert not ok
    assert any("reply_rate_warn must be > reply_rate_red" in e for e in errors)
    assert any("sent_pct_warn must be > sent_pct_red" in e for e in errors)
    assert any("bounce_rate_warn must be < bounce_rate_red" in e for e in errors)
    assert any("pool_days_warn must be > pool_days_red" in e for e in errors)


def test_save_config_invalidates_cache_via_hook(tmp_path: Path):
    """save_config fires the hook that clears cache timestamps."""
    cache.init_cache()  # register the hook
    # Seed the cache with some data for MyPlace
    gen, _, _ = cache.write_client_data("MyPlace", _fake_instantly_result())
    assert not cache.should_refresh("MyPlace")  # fresh

    mc.save_config(mc._factory_config(), config_path=tmp_path / "cfg.json")

    # Hook should have cleared timestamps → next check is True
    assert cache.should_refresh("MyPlace") is True


# ---------------------------------------------------------------------------
# 4. Scheduler dispatch end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_dispatches_to_both_platforms_and_spawns_instantly_backfills():
    """refresh_stale_monitoring fetches all 9 clients and spawns backfill for Instantly only."""
    # Inject fake httpx client + fake fetchers
    class FakeClient: pass
    mp._set_http_client_for_tests(FakeClient())

    async def fake_instantly(client, name, api_key):
        return _fake_instantly_result()

    async def fake_eb(client, name, api_key):
        return _fake_eb_result()

    mp._FETCHERS["instantly"] = fake_instantly
    mp._FETCHERS["emailbison"] = fake_eb

    # Record backfill spawns (monkey-patch the module-level function so the
    # scheduler's asyncio.create_task picks up the replacement at call time)
    spawned: list[tuple[str, list, str, int]] = []

    async def fake_backfill(name, ids, key, gen):
        spawned.append((name, ids, key, gen))

    mp._phase2_backfill = fake_backfill

    # Give every registry entry an api key
    for e in registry.list_monitoring_workspaces():
        e._api_key = f"fake-{e.slug}"

    await mp.refresh_stale_monitoring()

    # All 9 clients got cache writes, no errors
    with cache._lock:
        assert len(cache._data) == 9
        assert len(cache._errors) == 0

    # Let the fire-and-forget backfill tasks schedule
    await asyncio.sleep(0.05)

    # 7 Instantly clients → 7 backfills; 2 EB clients → 0 backfills
    instantly_names = {e.name for e in registry.list_monitoring_workspaces() if e.platform == "instantly"}
    assert len(instantly_names) == 7
    assert {s[0] for s in spawned} == instantly_names

    # Each backfill carries the generation from the Phase 1 write
    for name, ids, key, gen in spawned:
        assert gen == cache.get_generation(name)
        assert ids == ["cid1"]
        assert key == "fake-key"


@pytest.mark.asyncio
async def test_refresh_missing_api_key_produces_friendly_error():
    """Clients without an api key should land as friendly errors, not exceptions."""
    class FakeClient: pass
    mp._set_http_client_for_tests(FakeClient())

    # Leave api keys unset (load_from_env populated them from environment,
    # which in CI is usually empty)
    for e in registry.list_monitoring_workspaces():
        e._api_key = None

    await mp.refresh_stale_monitoring()

    with cache._lock:
        # Every client should have an error entry. write_error() runs the
        # raw message through _friendly_error, which maps "API key not found"
        # → "API key not configured".
        assert len(cache._errors) == 9
        assert all(msg == "API key not configured" for msg in cache._errors.values())
        # No data written
        assert len(cache._data) == 0


@pytest.mark.asyncio
async def test_refresh_fetcher_exception_is_caught_as_friendly_error():
    """A fetcher that raises must be caught and stored as a friendly error."""
    class FakeClient: pass
    mp._set_http_client_for_tests(FakeClient())

    async def raising_fetcher(client, name, api_key):
        raise RuntimeError("HTTP 401: Unauthorized")

    mp._FETCHERS["instantly"] = raising_fetcher

    # Only test Instantly clients to isolate
    entries = registry.list_monitoring_workspaces()
    instantly_entries = [e for e in entries if e.platform == "instantly"]
    eb_entries = [e for e in entries if e.platform == "emailbison"]
    for e in instantly_entries:
        e._api_key = "fake-key"
    for e in eb_entries:
        e._api_key = None  # force them into "key not found" instead

    # Suppress eb fetcher to keep the test narrow — no HTTP will be made there
    async def noop_eb(client, name, api_key):
        return _fake_eb_result()
    mp._FETCHERS["emailbison"] = noop_eb

    await mp.refresh_stale_monitoring()

    with cache._lock:
        # Every Instantly client should have the auth-failed friendly error
        for e in instantly_entries:
            assert cache._errors.get(e.name) == "Authentication failed — API key may be invalid", e.name


@pytest.mark.asyncio
async def test_mock_mode_refresh_is_noop():
    """refresh_stale_monitoring must not touch the cache when mock mode is on."""
    cache.set_mock_mode(True)
    try:
        # Even without a client set, mock mode should short-circuit safely
        await mp.refresh_stale_monitoring()
        with cache._lock:
            assert cache._data == {}
            assert cache._errors == {}
            assert cache._ts == {}
    finally:
        cache.set_mock_mode(False)


# ---------------------------------------------------------------------------
# 5. Stale backfill rejection end-to-end
# ---------------------------------------------------------------------------

def test_stale_backfill_is_rejected_by_generation_guard():
    """A Phase-2 backfill with a stale generation must NOT mutate the cache."""
    # Phase 1 lands twice → generation 2
    gen1, _, _ = cache.write_client_data("SwishFunding", _fake_instantly_result())
    gen2, _, _ = cache.write_client_data("SwishFunding", _fake_instantly_result())
    assert gen1 == 1 and gen2 == 2

    # Submit a stale backfill (from gen=1)
    applied = cache.write_backfill("SwishFunding", 1, {"cid1": 999})
    assert applied is False

    # Cached per-campaign count untouched
    with cache._lock:
        stored = cache._data["SwishFunding"]
    cid1 = next(c for c in stored["campaigns"] if c["id"] == "cid1")
    assert cid1["not_contacted"] == 20000  # original, not 999


def test_fresh_backfill_updates_cache_and_reclassifies():
    """A Phase-2 backfill with the current generation applies and reclassifies."""
    gen, _, _ = cache.write_client_data("SwishFunding", _fake_instantly_result())

    applied = cache.write_backfill("SwishFunding", gen, {"cid1": 80000})
    assert applied is True

    with cache._lock:
        stored = cache._data["SwishFunding"]
    cid1 = next(c for c in stored["campaigns"] if c["id"] == "cid1")
    assert cid1["not_contacted"] == 80000
    # Client-level nc recomputed from active campaigns
    assert stored["not_contacted"] == 80000
    # Status was reclassified (present in dict after backfill)
    assert stored["status"] in ("green", "amber", "red")
