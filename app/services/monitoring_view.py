"""View-layer helpers for the monitoring overview.

All threshold/color/aggregation logic that the templates need lives here so
that templates stay dumb and CSS contains zero business rules. Keep this
module pure: no I/O, no cache mutations.
"""
from __future__ import annotations

from typing import Any


def aggregate_summary(data: dict[str, dict]) -> dict[str, Any]:
    """Aggregate per-client monitoring entries into chip totals.

    `data` is the dict returned by `get_all_monitoring_data()` — keys are
    client display names, values are enriched ClientData dicts (or
    error/loading entries).

    Returns:
      red_count, amber_count, green_count   — status totals (loading/error excluded)
      sent_today_total                       — sum across non-error clients
      opps_today_total                       — sum across non-error clients
      replies_today_total                    — for reply rate calc
      reply_rate_pct                         — replies / sent * 100, 0.0 if no sent
      loading                                — True if every client is in loading state
                                               (drives skeleton shimmer)
    """
    red = amber = green = 0
    sent_total = 0
    opps_total = 0
    replies_total = 0
    loading_count = 0
    total = 0

    for entry in data.values():
        total += 1
        status = entry.get("status")
        if status == "loading":
            loading_count += 1
            continue
        if status == "error":
            continue
        if status == "red":
            red += 1
        elif status == "amber":
            amber += 1
        elif status == "green":
            green += 1

        sent_total += int(entry.get("sent_today") or 0)
        opps_total += int(entry.get("opps_today") or 0)
        replies_total += int(entry.get("replies_today") or 0)

    reply_rate_pct = (replies_total / sent_total * 100.0) if sent_total > 0 else 0.0

    return {
        "red_count":           red,
        "amber_count":         amber,
        "green_count":         green,
        "sent_today_total":    sent_total,
        "opps_today_total":    opps_total,
        "replies_today_total": replies_total,
        "reply_rate_pct":      reply_rate_pct,
        "loading":             total > 0 and loading_count == total,
    }


#: Allowed sort keys (anything else falls back to "status").
SORT_OPTIONS: list[tuple[str, str]] = [
    ("status",        "Status"),
    ("sent_today",    "Sent Today"),
    ("reply_rate",    "Reply Rate"),
    ("not_contacted", "Not Contacted"),
    ("name",          "Name"),
]

#: Status sort order — red first (most urgent), then amber, green, then
#: loading/error/unknown sink to the bottom.
_STATUS_RANK = {"red": 0, "amber": 1, "green": 2, "loading": 3, "error": 4}


def _status_rank(entry: dict) -> int:
    return _STATUS_RANK.get(entry.get("status") or "", 5)


def _num(entry: dict, key: str) -> float:
    """Coerce an entry field to float for sorting; missing/None → 0."""
    v = entry.get(key)
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def sort_clients(data: dict[str, dict], sort: str) -> list[tuple[str, dict]]:
    """Return `(name, entry)` pairs sorted per the requested key.

    All sorts use client name as the secondary key so order is deterministic
    across refreshes. Numeric sorts are descending (biggest first); name sort
    is ascending; status sort puts red→amber→green at the top.
    """
    items = list(data.items())
    if sort == "sent_today":
        items.sort(key=lambda kv: (-_num(kv[1], "sent_today"), kv[0].lower()))
    elif sort == "reply_rate":
        # reply_rate_today is the per-day rate the cards display; fall back
        # to reply_rate_7d if today's is missing.
        items.sort(key=lambda kv: (
            -(_num(kv[1], "reply_rate_today") or _num(kv[1], "reply_rate_7d")),
            kv[0].lower(),
        ))
    elif sort == "not_contacted":
        items.sort(key=lambda kv: (-_num(kv[1], "not_contacted"), kv[0].lower()))
    elif sort == "name":
        items.sort(key=lambda kv: kv[0].lower())
    else:  # "status" (default) and any unknown value
        items.sort(key=lambda kv: (_status_rank(kv[1]), kv[0].lower()))
    return items


def normalize_sort(sort: str | None) -> str:
    """Return `sort` if it's a recognized key, else the default ("status")."""
    valid = {k for k, _ in SORT_OPTIONS}
    return sort if sort in valid else "status"


# ---------------------------------------------------------------------------
# Card view-model — T4
# ---------------------------------------------------------------------------

#: Health colors used for the card left border + status pill background.
#: Anything not in this set falls back to "gray" (loading/error/unknown).
_HEALTH_COLORS = {"red", "amber", "green"}

#: Pretty status pill labels (3.3.5).
_STATUS_PILL_LABELS = {
    "red":     "Action",
    "amber":   "Watch",
    "green":   "On Track",
    "loading": "Loading",
    "error":   "Error",
}

#: Maps health → CSS class from base.html's existing .pill system
#: (.pill-g/.pill-a/.pill-r/.pill-m). Reused so monitoring cards stay
#: consistent with QA cards and don't duplicate the pill design system.
_HEALTH_TO_PILL_CLASS = {
    "red":   "pill-r",
    "amber": "pill-a",
    "green": "pill-g",
    "gray":  "pill-m",
}

#: Bounce-rate indicator labels (3.3.22).
_BOUNCE_LABELS = {"red": "Critical", "amber": "Elevated", "green": "Normal"}

#: Trend → arrow glyph + class. The classes resolve to colors in the template.
_TREND_GLYPHS = {
    "up":   ("↑", "trend--up"),
    "down": ("↓", "trend--down"),
    "flat": ("→", "trend--flat"),
}


def _safe_int(v) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _sent_color_class(sent: int, kpi_target: float) -> str:
    """Color the SENT TODAY hero number per GOAL.md 3.3.10.

    green ≥ KPI · neutral ≥ 80% · amber ≥ 50% · red < 50%.
    Falls back to neutral if KPI is unset/0.
    """
    if kpi_target <= 0:
        return "hero--neutral"
    pct = sent / kpi_target
    if pct >= 1.0:
        return "hero--green"
    if pct >= 0.8:
        return "hero--neutral"
    if pct >= 0.5:
        return "hero--amber"
    return "hero--red"


def _runway_days(not_contacted: int, avg_sent_7d: float) -> float | None:
    """Estimate days of runway given not_contacted leads and 7-day avg send.

    Returns None if avg_sent_7d is 0 (we can't divide by zero — runway is
    "infinite" but the template shows it as ">99d").
    """
    if avg_sent_7d <= 0:
        return None
    return not_contacted / avg_sent_7d


def _runway_color_class(runway: float | None) -> str:
    """Color the NOT CONTACTED hero per GOAL.md 3.3.13.

    green ≥ 7d · amber ≥ 3d · red < 3d. None (infinite) → green.
    """
    if runway is None:
        return "hero--green"
    if runway >= 7:
        return "hero--green"
    if runway >= 3:
        return "hero--amber"
    return "hero--red"


def _runway_text(runway: float | None) -> str:
    if runway is None or runway > 99:
        return ">99d runway"
    return f"{runway:.1f}d runway"


def _reply_rate_class(reply_rate: float, thresholds: dict) -> str:
    """Color reply rate by global thresholds (5.2.1)."""
    warn = _safe_float(thresholds.get("reply_rate_warn", 1.0))
    red  = _safe_float(thresholds.get("reply_rate_red", 0.5))
    if reply_rate >= warn:
        return "hero--green"
    if reply_rate >= red:
        return "hero--amber"
    return "hero--red"


def _bounce_status(bounce_rate: float, thresholds: dict) -> str:
    """Return 'red' / 'amber' / 'green' for bounce rate per global thresholds."""
    warn = _safe_float(thresholds.get("bounce_rate_warn", 3.0))
    red  = _safe_float(thresholds.get("bounce_rate_red", 5.0))
    if bounce_rate >= red:
        return "red"
    if bounce_rate >= warn:
        return "amber"
    return "green"


def _campaign_status_counts(campaigns: list[dict]) -> tuple[int, int, int]:
    """Return (active, paused, completed) campaign counts."""
    active = paused = completed = 0
    for c in campaigns or []:
        s = c.get("status")
        if s == "active":
            active += 1
        elif s == "paused":
            paused += 1
        elif s == "completed":
            completed += 1
    return active, paused, completed


def _instantly_workspace_url(workspace_id: str | None) -> str | None:
    if not workspace_id:
        return None
    return f"https://app.instantly.ai/app/analytics/overview?selected_wks={workspace_id}"


def build_card_view(
    name: str,
    entry: dict,
    workspace_id: str | None,
    slug: str | None = None,
) -> dict[str, Any]:
    """Compute a flat view-model dict for one client card.

    Reads `status` directly from `entry` per the Phase 2→3a contract — does
    NOT call `_classify_client`. Returns everything the template needs as
    pre-formatted strings or CSS class names; the template stays dumb.

    `slug` should be the canonical registry slug. Falls back to a derived
    value only when no slug is supplied (e.g. ad-hoc tests).
    """
    canonical_slug = slug or name.lower().replace(" ", "_")
    status = entry.get("status") or "unknown"
    health = status if status in _HEALTH_COLORS else "gray"

    # Loading state — sparse view-model, template uses .is_loading flag.
    if status == "loading":
        return {
            "name":        name,
            "status":      status,
            "health":      "gray",
            "platform":    entry.get("platform") or "",
            "is_loading":  True,
            "is_error":    False,
            "clickable":   False,
            "slug":        canonical_slug,
        }

    # Error state — sparse view-model, friendly message from cache.
    if status == "error":
        return {
            "name":        name,
            "status":      status,
            "health":      "red",
            "platform":    entry.get("platform") or "",
            "is_loading":  False,
            "is_error":    True,
            "error_msg":   entry.get("error") or "Unknown error",
            "clickable":   False,
            "slug":        canonical_slug,
        }

    kpi        = entry.get("kpi") or {}
    thresholds = entry.get("thresholds") or {}
    campaigns  = entry.get("campaigns") or []
    platform   = entry.get("platform") or ""

    # ----- Upper-left: name + platform + status pill + campaign counts ----
    active_n, paused_n, completed_n = _campaign_status_counts(campaigns)
    # Fall back to client-level active_campaigns count if no campaign list
    if not campaigns:
        active_n = _safe_int(entry.get("active_campaigns"))

    # ----- Upper-mid: SENT TODAY -----
    sent_today  = _safe_int(entry.get("sent_today"))
    first_touch = _safe_int(entry.get("first_touch_today"))
    followups   = _safe_int(entry.get("followup_today"))
    sent_kpi    = _safe_float(kpi.get("sent", 0))
    sent_class  = _sent_color_class(sent_today, sent_kpi)
    sent_trend_glyph, sent_trend_class = _TREND_GLYPHS.get(
        entry.get("sent_trend") or "flat", _TREND_GLYPHS["flat"]
    )
    avg_sent_7d = _safe_float(entry.get("avg_sent_7d"))
    if avg_sent_7d > 0:
        sent_vs_avg_pct = ((sent_today - avg_sent_7d) / avg_sent_7d) * 100.0
        sent_vs_avg_text = f"vs 7d avg: {sent_vs_avg_pct:+.0f}%"
    else:
        sent_vs_avg_text = "vs 7d avg: —"

    # ----- Upper-right: NOT CONTACTED -----
    not_contacted = _safe_int(entry.get("not_contacted"))
    runway = _runway_days(not_contacted, avg_sent_7d)
    runway_class = _runway_color_class(runway)
    runway_text = _runway_text(runway)

    # ----- Lower-left: REPLY RATE -----
    reply_rate_today = _safe_float(entry.get("reply_rate_today"))
    reply_rate_7d    = _safe_float(entry.get("reply_rate_7d"))
    reply_rate_class = _reply_rate_class(reply_rate_today, thresholds)
    reply_trend_glyph, reply_trend_class = _TREND_GLYPHS.get(
        entry.get("reply_trend") or "flat", _TREND_GLYPHS["flat"]
    )
    replies_today = _safe_int(entry.get("replies_today"))
    opps_today    = _safe_int(entry.get("opps_today"))

    # ----- Lower-right: BOUNCE RATE -----
    bounce_rate = _safe_float(entry.get("bounce_rate"))
    bounce_health = _bounce_status(bounce_rate, thresholds)
    bounce_label  = _BOUNCE_LABELS[bounce_health]
    in_progress   = _safe_int(entry.get("in_progress"))

    # ----- Action row -----
    instantly_url = _instantly_workspace_url(workspace_id) if platform == "instantly" else None

    return {
        # identity
        "name":           name,
        "slug":           canonical_slug,
        "status":         status,
        "health":         health,
        "is_loading":     False,
        "is_error":       False,
        "clickable":      True,
        "platform":       platform,
        "status_label":   _STATUS_PILL_LABELS.get(status, status.title()),
        "pill_class":     _HEALTH_TO_PILL_CLASS.get(health, "pill-m"),

        # campaign counts
        "active_n":       active_n,
        "paused_n":       paused_n,
        "completed_n":    completed_n,

        # SENT TODAY zone
        "sent_today":         sent_today,
        "sent_today_fmt":     f"{sent_today:,}",
        "sent_class":         sent_class,
        "first_touch":        first_touch,
        "followups":          followups,
        "sent_trend_glyph":   sent_trend_glyph,
        "sent_trend_class":   sent_trend_class,
        "sent_vs_avg_text":   sent_vs_avg_text,

        # NOT CONTACTED zone
        "not_contacted":      not_contacted,
        "not_contacted_fmt":  f"{not_contacted:,}",
        "runway_class":       runway_class,
        "runway_text":        runway_text,

        # REPLY RATE zone
        "reply_rate_today":     reply_rate_today,
        "reply_rate_today_fmt": f"{reply_rate_today:.2f}%",
        "reply_rate_7d_fmt":    f"{reply_rate_7d:.2f}%",
        "reply_rate_class":     reply_rate_class,
        "reply_trend_glyph":    reply_trend_glyph,
        "reply_trend_class":    reply_trend_class,
        "replies_today":        replies_today,
        "opps_today":           opps_today,

        # BOUNCE RATE zone
        "bounce_rate_fmt":   f"{bounce_rate:.2f}%",
        "bounce_health":     bounce_health,
        "bounce_class":      f"hero--{bounce_health}",
        "bounce_label":      bounce_label,
        "in_progress":       in_progress,
        "in_progress_fmt":   f"{in_progress:,}",

        # action row
        "instantly_url":     instantly_url,
    }




# ---------------------------------------------------------------------------
# Drill-down view-model — Phase 3b T3-T6
# ---------------------------------------------------------------------------


def compute_alerts(entry: dict) -> list[dict]:
    """Evaluate the 8 alert conditions from GOAL.md 3.5.1-3.5.8.

    Returns a list of `{level, message}` dicts in priority order. Multiple
    can fire — the template renders all of them.
    """
    alerts: list[dict] = []
    thresholds = entry.get("thresholds") or {}

    pool_warn = _safe_float(thresholds.get("pool_days_warn", 7))
    pool_red  = _safe_float(thresholds.get("pool_days_red", 3))
    reply_warn = _safe_float(thresholds.get("reply_rate_warn", 1.0))
    reply_red  = _safe_float(thresholds.get("reply_rate_red", 0.5))
    bounce_warn = _safe_float(thresholds.get("bounce_rate_warn", 3.0))
    bounce_red  = _safe_float(thresholds.get("bounce_rate_red", 5.0))

    not_contacted = _safe_int(entry.get("not_contacted"))
    avg_sent_7d = _safe_float(entry.get("avg_sent_7d"))
    sent_today = _safe_int(entry.get("sent_today"))
    pool_days = _runway_days(not_contacted, avg_sent_7d)
    reply_rate = _safe_float(entry.get("reply_rate_today"))
    bounce_rate = _safe_float(entry.get("bounce_rate"))
    active_camps = _safe_int(entry.get("active_campaigns"))
    total_camps = _safe_int(entry.get("total_campaigns"))

    # 3.5.1 / 3.5.2 — pool runway
    if pool_days is not None:
        if pool_days < pool_red:
            alerts.append({
                "level": "red",
                "message": f"Lead pool critically low — {pool_days:.1f} days of leads remaining at current send rate",
            })
        elif pool_days < pool_warn:
            alerts.append({
                "level": "amber",
                "message": f"Lead pool running low — {pool_days:.1f} days of leads remaining",
            })

    # 3.5.3 / 3.5.4 — reply rate (only if sent > 50)
    if sent_today > 50:
        if reply_rate < reply_red:
            alerts.append({
                "level": "red",
                "message": f"Reply rate critically low at {reply_rate:.2f}%",
            })
        elif reply_rate < reply_warn:
            alerts.append({
                "level": "amber",
                "message": f"Reply rate below target at {reply_rate:.2f}% (target: {reply_warn:.1f}%)",
            })

    # 3.5.5 — no active campaigns
    if active_camps == 0 and total_camps > 0:
        alerts.append({
            "level": "red",
            "message": "No active campaigns — all campaigns are paused or completed",
        })

    # 3.5.6 — active but zero sent
    if active_camps > 0 and sent_today == 0:
        alerts.append({
            "level": "red",
            "message": "Active campaigns but 0 emails sent today",
        })

    # 3.5.7 / 3.5.8 — bounce rate
    if bounce_rate > bounce_red:
        alerts.append({
            "level": "red",
            "message": f"Bounce rate critically high at {bounce_rate:.2f}%",
        })
    elif bounce_rate > bounce_warn:
        alerts.append({
            "level": "amber",
            "message": f"Bounce rate elevated at {bounce_rate:.2f}%",
        })

    return alerts


_CAMPAIGN_STATUS_PILL = {
    "active":    "pill-g",
    "paused":    "pill-m",
    "completed": "pill-blue",
}

_CAMPAIGN_GROUP_LABELS = {
    "active":    "Active",
    "paused":    "Paused",
    "completed": "Completed",
    "other":     "Other",
}


def build_campaign_row_view(campaign: dict, platform: str) -> dict[str, Any]:
    """Pre-format a single campaign row for the drill-down sub-table.

    EmailBison fields are normalized: first_touch=0, followups=0,
    not_contacted=0, in_progress="N/A".
    """
    is_eb = platform == "emailbison"
    raw_status = (campaign.get("status") or "").lower()
    status = raw_status if raw_status in ("active", "paused", "completed") else "other"

    sent = _safe_int(campaign.get("sent_today"))
    first_touch = 0 if is_eb else _safe_int(campaign.get("first_touch"))
    followups = 0 if is_eb else _safe_int(campaign.get("followups"))
    replied = _safe_int(campaign.get("replies_today"))
    opps = _safe_int(campaign.get("opps_today"))
    reply_rate = _safe_float(campaign.get("reply_rate"))
    not_contacted = 0 if is_eb else _safe_int(campaign.get("not_contacted"))
    in_progress_raw = campaign.get("in_progress")
    in_progress_fmt = "N/A" if is_eb or in_progress_raw is None else f"{_safe_int(in_progress_raw):,}"

    pill_class = _CAMPAIGN_STATUS_PILL.get(status, "pill-m")

    return {
        "name":             campaign.get("name") or "(unnamed)",
        "id":               campaign.get("id"),
        "status":           status,
        "status_label":     status.title(),
        "pill_class":       pill_class,
        "sent_today":       sent,
        "sent_today_fmt":   f"{sent:,}",
        "first_touch":      first_touch,
        "first_touch_fmt":  f"{first_touch:,}",
        "followups":        followups,
        "followups_fmt":    f"{followups:,}",
        "replied":          replied,
        "replied_fmt":      f"{replied:,}",
        "opps":             opps,
        "opps_fmt":         f"{opps:,}",
        "reply_rate_fmt":   f"{reply_rate:.2f}%",
        "not_contacted_fmt": f"{not_contacted:,}",
        "in_progress_fmt":  in_progress_fmt,
    }


def group_campaigns_by_status(
    campaigns: list[dict],
    platform: str,
) -> list[dict]:
    """Group campaigns into 4 ordered buckets, sorted by sent_today DESC within each.

    Returns a list of `{key, label, count, open, rows}` dicts in render order:
    active, paused, completed, other. `open` indicates whether the group is
    expanded by default (active + paused = open; completed + other = collapsed).
    """
    buckets: dict[str, list[dict]] = {"active": [], "paused": [], "completed": [], "other": []}
    for camp in campaigns or []:
        row = build_campaign_row_view(camp, platform)
        buckets[row["status"]].append(row)

    for rows in buckets.values():
        rows.sort(key=lambda r: -r["sent_today"])

    open_default = {"active": True, "paused": True, "completed": False, "other": False}
    groups = []
    for key in ("active", "paused", "completed", "other"):
        rows = buckets[key]
        groups.append({
            "key":   key,
            "label": _CAMPAIGN_GROUP_LABELS[key],
            "count": len(rows),
            "open":  open_default[key],
            "rows":  rows,
        })
    return groups


def _kpi_cards(entry: dict, platform: str) -> dict[str, Any]:
    """Compose KPI mini-card view-models for the drill-down."""
    is_eb = platform == "emailbison"
    kpi = entry.get("kpi") or {}
    thresholds = entry.get("thresholds") or {}

    sent_today = _safe_int(entry.get("sent_today"))
    sent_kpi = _safe_float(kpi.get("sent", 0))
    sent_pct = min(100.0, (sent_today / sent_kpi * 100.0)) if sent_kpi > 0 else 0.0
    sent_class = _sent_color_class(sent_today, sent_kpi)

    first_touch = 0 if is_eb else _safe_int(entry.get("first_touch_today"))
    followups = 0 if is_eb else _safe_int(entry.get("followup_today"))

    reply_rate_today = _safe_float(entry.get("reply_rate_today"))
    reply_rate_class = _reply_rate_class(reply_rate_today, thresholds)
    reply_warn = _safe_float(thresholds.get("reply_rate_warn", 1.0))
    # cap reply axis at 2× warn or 2.0% whichever larger so target line stays visible
    reply_axis_max = max(reply_warn * 2.0, 2.0, reply_rate_today * 1.2)
    reply_target_pct = min(100.0, (reply_warn / reply_axis_max * 100.0)) if reply_axis_max > 0 else 0.0
    reply_value_pct = min(100.0, (reply_rate_today / reply_axis_max * 100.0)) if reply_axis_max > 0 else 0.0

    replies_today = _safe_int(entry.get("replies_today"))
    opps_today = _safe_int(entry.get("opps_today"))
    avg_replies_7d = _safe_float(entry.get("avg_replies_7d"))
    avg_opps_7d = _safe_float(entry.get("avg_opps_7d"))

    bounce_rate = _safe_float(entry.get("bounce_rate"))
    bounce_health = _bounce_status(bounce_rate, thresholds)
    bounce_warn = _safe_float(thresholds.get("bounce_rate_warn", 3.0))
    bounce_axis_max = max(bounce_warn * 2.0, 6.0, bounce_rate * 1.2)
    bounce_warn_pct = min(100.0, (bounce_warn / bounce_axis_max * 100.0)) if bounce_axis_max > 0 else 0.0
    bounce_value_pct = min(100.0, (bounce_rate / bounce_axis_max * 100.0)) if bounce_axis_max > 0 else 0.0

    not_contacted = _safe_int(entry.get("not_contacted"))
    avg_sent_7d = _safe_float(entry.get("avg_sent_7d"))
    runway = _runway_days(not_contacted, avg_sent_7d)
    runway_class = _runway_color_class(runway)
    runway_text = _runway_text(runway)

    in_progress_raw = entry.get("in_progress")
    in_progress_fmt = "N/A" if is_eb or in_progress_raw is None else f"{_safe_int(in_progress_raw):,}"
    in_progress_class = "hero--neutral" if (is_eb or in_progress_raw is None) else "hero--neutral"

    return {
        "not_contacted_fmt": f"{not_contacted:,}",
        "runway_text":       runway_text,
        "runway_class":      runway_class,

        "in_progress_fmt":   in_progress_fmt,
        "in_progress_class": in_progress_class,

        "sent_today_fmt":    f"{sent_today:,}",
        "sent_class":        sent_class,
        "sent_kpi_fmt":      f"{int(sent_kpi):,}" if sent_kpi > 0 else "—",
        "sent_pct":          sent_pct,

        "first_touch_fmt":   f"{first_touch:,}",
        "followups_fmt":     f"{followups:,}",

        "reply_rate_fmt":    f"{reply_rate_today:.2f}%",
        "reply_rate_class":  reply_rate_class,
        "reply_target_fmt":  f"{reply_warn:.1f}%",
        "reply_target_pct":  reply_target_pct,
        "reply_value_pct":   reply_value_pct,

        "replied_fmt":       f"{replies_today:,}",
        "opps_fmt":          f"{opps_today:,}",
        "avg_replies_7d_fmt": f"{avg_replies_7d:.0f}",
        "avg_opps_7d_fmt":   f"{avg_opps_7d:.1f}",

        "bounce_rate_fmt":   f"{bounce_rate:.2f}%",
        "bounce_class":      f"hero--{bounce_health}",
        "bounce_warn_fmt":   f"{bounce_warn:.1f}%",
        "bounce_warn_pct":   bounce_warn_pct,
        "bounce_value_pct":  bounce_value_pct,
    }


def build_drilldown_view(
    name: str,
    entry: dict,
    workspace_id: str | None,
    platform: str,
    slug: str | None = None,
) -> dict[str, Any]:
    """Top-level drill-down view-model.

    Returns a dict with `alerts`, `kpis`, `campaign_groups`, `instantly_url`,
    `is_emailbison`, plus a `card` view (the same one the overview uses) for
    the hero zones.
    """
    is_eb = platform == "emailbison"
    instantly_url = _instantly_workspace_url(workspace_id) if platform == "instantly" else None

    return {
        "alerts":          compute_alerts(entry),
        "kpis":            _kpi_cards(entry, platform),
        "campaign_groups": group_campaigns_by_status(entry.get("campaigns") or [], platform),
        "instantly_url":   instantly_url,
        "is_emailbison":   is_eb,
        "card":            build_card_view(name, entry, workspace_id, slug=slug),
    }


__all__ = [
    "aggregate_summary",
    "sort_clients",
    "normalize_sort",
    "build_card_view",
    "SORT_OPTIONS",
    # Phase 3b
    "compute_alerts",
    "build_campaign_row_view",
    "group_campaigns_by_status",
    "build_drilldown_view",
]
