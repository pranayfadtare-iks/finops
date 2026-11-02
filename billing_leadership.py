"""Billing-driven leadership helpers, organised around business units: who spends what, what changed, and what to do about it.

Pure pandas / plotly / HTML strings: no Streamlit and no Google API calls, so everything here can be tested on its own.

Data shape
----------
The app loads one grouped billing frame (see ``load_leadership_billing`` in app.py) with one row per
``period`` ("current" = the sidebar dates, "previous" = the same-length period right before it) and per
combination of the dimensions below. ``net_cost`` is cost after credits, exactly like the Overview.
A second, daily frame (``load_leadership_daily``) has ``period``, ``usage_date``, ``business_unit`` and ``net_cost``.

Dimensions
----------
``service`` and ``project`` come straight from the billing export. ``feature``, ``owner``, ``client`` and
``environment`` are *labels* on the resource (or, when the resource has none, on its project). Teams spell label
keys differently, so each accepts several keys, tried in order; override them with env vars such as
``FINOPS_LABEL_FEATURE="feature,application"``.

The organisation dimensions are derived from the ``feature`` label with ``bu_mapping.json`` (the BU billing plan):

* ``application``     the BU-plan application a feature belongs to (Optimix, Scribble, VCA ...); unmapped features keep their name.
* ``business_unit``   Ambulatory / Acute / Scribing / Platform / Tech Shared; ``Unmapped`` for features outside the plan.
* ``feature_cluster`` from the plan's feature -> cluster table, else the ``feature-cluster`` label with its spellings cleaned up.

All of them are plain BigQuery expressions (``dim_sql``), so the same values can be filtered on in SQL everywhere.
Every clickable chart returns ``(figure, lookup)``; ``lookup[curve][point]`` is the key behind each slice or bar
and ``resolve_click`` turns a Streamlit selection payload back into that key.
"""
import html
import json
import os
import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from rightsizing import NOT_LABELED
from vm_charts import FONT, INK, TEXT, finish

# --------------------------------------------------------------------------- dimensions
BU, CLUSTER, APP, FEATURE = "business_unit", "feature_cluster", "application", "feature"
UNMAPPED = "Unmapped"
NON_BU = (UNMAPPED, NOT_LABELED)          # buckets that are not a real business unit
DERIVED = (BU, APP, FEATURE)              # dimensions read from the feature label
DEFAULT_LABEL_KEYS = {
    FEATURE: ["feature", "application"],
    CLUSTER: ["feature-cluster"],
    "owner": ["owner", "app_dev_owner", "app-dev-owner", "app_support_owner", "app-support-owner"],
    "client": ["client"],
    "environment": ["environment"],
}
TITLES = {BU: "Business unit", CLUSTER: "Feature cluster", APP: "Application", FEATURE: "Feature", "owner": "Owner",
          "client": "Client", "environment": "Environment", "service": "Service", "project": "Project"}
ALL_DIMS = [BU, CLUSTER, APP, FEATURE, "owner", "client", "environment", "service", "project"]
SPIKE_DIMS = [APP, "service", "project", "owner", "client"]          # non-BU increases worth an insight
MATRIX_DIMS = [APP, CLUSTER, "service", "project", "environment", "owner"]
FOCUS_DIMS = [APP, "service", "project", "environment"]
COVERAGE_DIMS = [BU, FEATURE, CLUSTER, "owner", "client", "environment"]
PROD_VALUES = {"prod", "production", "prd", "live"}
_KEY_OK = re.compile(r"^[A-Za-z0-9_.\-]+$")

# colours: one validated categorical order; business units keep the plan's order so a unit is always the same colour
BU_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#6250d6", "#e87ba4", "#008300", "#e34948"]
PIE_COLORS = BU_COLORS
OTHER_COLOR, UNLABELED_COLOR, UNMAPPED_COLOR = "#C9D3E0", "#94A3B8", "#B8A88A"
BASE_COLOR, UP_COLOR, DOWN_COLOR = "#2a78d6", "#d03b3b", "#0ca30c"
GOOD_COLOR, WARN_COLOR, BAD_COLOR = "#0ca30c", "#eda100", "#d03b3b"
HEAT_SCALE = [[0.0, "#cde2fb"], [0.35, "#86b6ef"], [0.7, "#2a78d6"], [1.0, "#0d366b"]]   # one hue, light -> dark
SPIKE_PCT, SPIKE_SHARE = 0.25, 0.02   # an "increase" insight needs +25% and at least 2% of total spend
PIE_HEIGHT = 360


# --------------------------------------------------------------------------- BU billing plan (bu_mapping.json)
MAPPING_FILE = os.getenv("FINOPS_BU_MAPPING", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bu_mapping.json"))


def _norm(value):
    return str(value).strip().lower()


def load_mapping(path=MAPPING_FILE):
    """Read the BU billing plan into lookup dicts (feature -> application, application -> BU, feature -> cluster, label -> cluster)."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    feature_app, app_bu, apps_by_bu, feature_cluster, cluster_alias = {}, {}, {}, {}, {}
    for bu, apps in raw["business_units"].items():
        apps_by_bu[bu] = list(apps)
        for app, aliases in apps.items():
            app_bu[app] = bu
            for alias in aliases:
                feature_app[_norm(alias)] = app
    for cluster, features in raw.get("feature_clusters", {}).items():
        for feature in features:
            feature_cluster[_norm(feature)] = cluster
    for cluster, labels in raw.get("cluster_label_aliases", {}).items():
        for label in labels:
            cluster_alias[_norm(label)] = cluster
    return dict(bus=list(raw["business_units"]), apps_by_bu=apps_by_bu, feature_app=feature_app, app_bu=app_bu,
                feature_cluster=feature_cluster, cluster_alias=cluster_alias, clusters=list(raw.get("feature_clusters", {})))


MAPPING = load_mapping()


def label_keys(dim):
    """Label keys used for a dimension (env override ``FINOPS_LABEL_<DIM>``). Unsafe keys are dropped."""
    raw = os.getenv(f"FINOPS_LABEL_{dim.upper()}", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()] or DEFAULT_LABEL_KEYS[dim]
    safe = [k for k in keys if _KEY_OK.match(k)]
    return safe or DEFAULT_LABEL_KEYS[dim]


def source_keys(dim):
    """Label keys a dimension is read from (business unit and application come from the feature label)."""
    if dim in DERIVED:
        return label_keys(FEATURE)
    return label_keys(dim) if dim in DEFAULT_LABEL_KEYS else []


def _lit(text):
    """BigQuery string literal."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _label_sql(keys):
    """First non-empty label value: each key on the resource, then on its project, before trying the next key."""
    tries = [f"NULLIF(LOWER(TRIM((SELECT l.value FROM UNNEST({field}) l WHERE l.key='{k}' LIMIT 1))),'')"
             for k in keys for field in ("labels", "project.labels")]
    return "COALESCE(" + ",".join(tries) + f",'{NOT_LABELED}')"


def _case(expr, pairs, default):
    if not pairs:
        return default
    return f"(CASE {expr} " + " ".join(f"WHEN {_lit(k)} THEN {_lit(v)}" for k, v in pairs) + f" ELSE {default} END)"


def dim_sql(dim):
    """BigQuery expression for a dimension. Raises KeyError for anything that is not a known dimension."""
    if dim not in TITLES:
        raise KeyError(dim)
    if dim == "service":
        return "COALESCE(service.description,'Unspecified service')"
    if dim == "project":
        return "COALESCE(project.name,project.id,'Unspecified project')"
    feature = _label_sql(label_keys(FEATURE))
    apps = sorted(MAPPING["feature_app"].items())
    if dim == FEATURE:
        return feature
    if dim == APP:
        return _case(feature, apps, feature)
    if dim == BU:
        return _case(feature, [(f, MAPPING["app_bu"][a]) for f, a in apps] + [(NOT_LABELED, NOT_LABELED)], _lit(UNMAPPED))
    if dim == CLUSTER:
        label = _label_sql(label_keys(CLUSTER))
        return _case(feature, sorted(MAPPING["feature_cluster"].items()), _case(label, sorted(MAPPING["cluster_alias"].items()), label))
    return _label_sql(label_keys(dim))


# --------------------------------------------------------------------------- formatting helpers
def arrow_text(delta, delta_pct, fmt_money):
    """'▲ ₹1.2M (+23%)' / '▼ ₹300K (-8%)' / 'no change'."""
    if abs(delta) < 0.5:
        return "no change"
    sign = "▲" if delta > 0 else "▼"
    pct = "" if pd.isna(delta_pct) else f" ({delta_pct:+.0%})"
    return f"{sign} {fmt_money(abs(delta))}{pct}"


def bu_name(value):
    """Display name for a business unit value."""
    return "No feature label" if value == NOT_LABELED else str(value)


def display(dim, value):
    """Display name for any dimension value."""
    if dim == BU:
        return bu_name(value)
    return "Not labeled" if value == NOT_LABELED else str(value)


def is_unit(value):
    return value not in NON_BU


def _short(text, n=26):
    return text if len(text) <= n else text[:n - 1] + "…"


def _short_unique(values, n=26):
    """Shortened labels, falling back to the full names when truncation makes two of them equal (Plotly would merge them)."""
    labels = [_short(str(v), n) for v in values]
    return labels if len(set(labels)) == len(labels) else [str(v) for v in values]


def _alpha(hex_color, a):
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


# --------------------------------------------------------------------------- tables
def prepare(df):
    """Coerce numeric columns and make sure every dimension is filled."""
    out = df.copy()
    for col in ("gross_cost", "net_cost"):
        out[col] = pd.to_numeric(out.get(col, 0), errors="coerce").fillna(0)
    for dim in ALL_DIMS:
        if dim not in out:
            out[dim] = NOT_LABELED
        out[dim] = out[dim].fillna(NOT_LABELED).astype(str)
    return out


def prepare_daily(df):
    """Same clean-up for the daily frame (period, usage_date, business_unit, net_cost)."""
    out = df.copy()
    out["net_cost"] = pd.to_numeric(out.get("net_cost", 0), errors="coerce").fillna(0)
    out["usage_date"] = pd.to_datetime(out["usage_date"], errors="coerce")
    out[BU] = out[BU].fillna(NOT_LABELED).astype(str) if BU in out else NOT_LABELED
    return out


def group_table(df, by):
    """Cost per value of ``by`` for both periods, with change and share of the current total."""
    cols = ["value", "cost", "prev", "delta", "delta_pct", "share"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    g = df.pivot_table(index=by, columns="period", values="net_cost", aggfunc="sum", fill_value=0.0)
    for p in ("current", "previous"):
        if p not in g:
            g[p] = 0.0
    t = pd.DataFrame({"value": g.index, "cost": g["current"].values, "prev": g["previous"].values})
    t["delta"] = t["cost"] - t["prev"]
    t["delta_pct"] = np.where(t["prev"] > 0, t["delta"] / t["prev"].where(t["prev"] > 0, 1), np.nan)
    total = t["cost"].sum()
    t["share"] = t["cost"] / total if total > 0 else 0.0
    return t.sort_values("cost", ascending=False).reset_index(drop=True)


def select_rows(df, dim, value):
    return df[df[dim] == value]


def _driver(rows, by=APP, up=True):
    """(name, delta) of the biggest increase (or decrease) of ``by`` inside ``rows``, or None."""
    t = group_table(rows, by)
    t = t[t["delta"] > 0] if up else t[t["delta"] < 0]
    if t.empty:
        return None
    r = t.sort_values("delta", ascending=not up).iloc[0]
    return display(by, r["value"]), float(r["delta"])


def unmapped_features(df, n=25):
    """Features outside the BU billing plan, biggest first, with the cluster they carry (to extend bu_mapping.json)."""
    rows = select_rows(df, BU, UNMAPPED)
    t = group_table(rows, FEATURE)
    if t.empty:
        return t.assign(cluster=[])
    cur = rows[rows["period"] == "current"]
    cluster = (cur.groupby([FEATURE, CLUSTER])["net_cost"].sum().reset_index().sort_values("net_cost", ascending=False)
               .drop_duplicates(FEATURE).set_index(FEATURE)[CLUSTER])
    t["cluster"] = t["value"].map(cluster).fillna(NOT_LABELED)
    return t[t["cost"] > 0].head(n)


# --------------------------------------------------------------------------- headline numbers
def run_rate(daily, today, bu=None):
    """Average daily net cost x 30. Today is dropped (billing data for it is incomplete) when other days exist.

    The average is taken over every day of the period, so a business unit with no cost on some days is not overstated."""
    cur = daily[daily["period"] == "current"]
    days = pd.to_datetime(cur["usage_date"]).dt.normalize()
    keep = days < pd.Timestamp(today) if days.nunique() > 1 else pd.Series(True, index=cur.index)
    n_days = days[keep].nunique()
    if bu is not None:
        keep = keep & (cur[BU] == bu)
    return float(cur.loc[keep, "net_cost"].sum() / n_days * 30) if n_days else 0.0


def summary(df, daily, today):
    cur_total = float(df.loc[df["period"] == "current", "net_cost"].sum())
    prev_total = float(df.loc[df["period"] == "previous", "net_cost"].sum())
    gross_cur = float(df.loc[df["period"] == "current", "gross_cost"].sum())
    delta = cur_total - prev_total
    bus = group_table(df, BU)
    real = bus["value"].map(is_unit).astype(bool)
    known = bus[real & (bus["cost"] > 0)]
    top_bu = known.iloc[0] if len(known) else None
    miss = bus[~real]
    unattributed, unattributed_prev = float(miss["cost"].sum()), float(miss["prev"].sum())
    env = group_table(df, "environment")
    env_known = env[(env["value"] != NOT_LABELED) & (env["cost"] > 0)]
    nonprod = float(env_known.loc[~env_known["value"].str.lower().isin(PROD_VALUES), "cost"].sum())
    env_total = float(env_known["cost"].sum())
    apps = group_table(df, APP)
    return dict(
        cost=cur_total, prev=prev_total, delta=delta, delta_pct=(delta / prev_total if prev_total > 0 else float("nan")),
        credits=gross_cur - cur_total, run_rate=run_rate(daily, today), bu_count=len(known),
        app_count=int(((apps["cost"] > 0) & (apps["value"] != NOT_LABELED)).sum()),
        top_bu=None if top_bu is None else (top_bu["value"], float(top_bu["cost"]), float(top_bu["share"])),
        unattributed=unattributed, unattributed_pct=(unattributed / cur_total if cur_total > 0 else 0.0),
        unmapped=float(bus.loc[bus["value"] == UNMAPPED, "cost"].sum()), unlabeled=float(bus.loc[bus["value"] == NOT_LABELED, "cost"].sum()),
        assigned_pct=(1 - unattributed / cur_total) if cur_total > 0 else 0.0,
        assigned_prev_pct=(1 - unattributed_prev / prev_total) if prev_total > 0 else float("nan"),
        nonprod=nonprod, nonprod_pct=(nonprod / env_total if env_total > 0 else 0.0),
    )


def _signal(r):
    """One-word health signal for a scorecard row."""
    if r["value"] == NOT_LABELED:
        return "⚠️ No label"
    if r["value"] == UNMAPPED:
        return "⚠️ Not in plan"
    if r["prev"] <= 0 and r["cost"] > 0:
        return "🆕 New"
    if pd.isna(r["delta_pct"]):
        return "—"
    if r["delta_pct"] >= SPIKE_PCT:
        return "🔺 Rising fast"
    if r["delta_pct"] >= 0.05:
        return "↗️ Rising"
    if r["delta_pct"] <= -0.05:
        return "↘️ Falling"
    return "➡️ Steady"


def _top_of(cur, by):
    """Per business unit: the value of ``by`` with the most cost, and that cost."""
    return (cur.groupby([BU, by])["net_cost"].sum().reset_index()
            .sort_values("net_cost", ascending=False).drop_duplicates(BU).set_index(BU))


def bu_scorecard(df, daily, today):
    """One row per business unit: spend, share, change, run rate, daily trend, top application / service, non-prod and owner coverage.

    Real units are sorted by spend; 'Unmapped' and 'no feature label' always come last."""
    t = group_table(df, BU)
    t = t[(t["cost"] != 0) | (t["prev"] != 0)].copy()
    cur = df[df["period"] == "current"]
    positive = t["cost"].where(t["cost"] > 0)
    app, svc = _top_of(cur, APP), _top_of(cur, "service")
    env = cur[cur["environment"] != NOT_LABELED]
    env_cost = env.groupby(BU)["net_cost"].sum()
    nonprod = env[~env["environment"].str.lower().isin(PROD_VALUES)].groupby(BU)["net_cost"].sum()
    owned = cur[cur["owner"] != NOT_LABELED].groupby(BU)["net_cost"].sum()
    n_apps = cur[cur["net_cost"] > 0].groupby(BU)[APP].nunique()
    t["apps"] = t["value"].map(n_apps).fillna(0).astype(int)
    t["top_app"] = t["value"].map(app[APP]).map(lambda v: display(APP, v) if isinstance(v, str) else "—")
    t["top_app_share"] = (t["value"].map(app["net_cost"]) / positive).clip(0, 1)
    t["top_service"] = t["value"].map(svc["service"]).fillna("—")
    t["nonprod_pct"] = (t["value"].map(nonprod).fillna(0) / t["value"].map(env_cost).where(lambda s: s > 0)).clip(0, 1)
    t["owner_pct"] = (t["value"].map(owned).fillna(0) / positive).clip(0, 1)
    t["run_rate"] = [run_rate(daily, today, v) for v in t["value"]]
    cur_d = daily[daily["period"] == "current"]
    if len(cur_d):
        p = cur_d.pivot_table(index="usage_date", columns=BU, values="net_cost", aggfunc="sum", fill_value=0.0).sort_index()
        t["trend"] = [p[v].round(2).tolist() if v in p else [] for v in t["value"]]
    else:
        t["trend"] = [[] for _ in range(len(t))]
    t["signal"] = [_signal(r) for _, r in t.iterrows()]
    real = t["value"].map(is_unit).astype(bool)
    t = pd.concat([t[real], t[t["value"] == UNMAPPED], t[t["value"] == NOT_LABELED]]).reset_index(drop=True)
    t["rank"] = range(1, len(t) + 1)
    return t


def bu_colors():
    """Business unit -> colour, in the order of the BU billing plan (stable whatever the filters)."""
    colors = {bu: BU_COLORS[i % len(BU_COLORS)] for i, bu in enumerate(MAPPING["bus"])}
    colors[UNMAPPED], colors[NOT_LABELED] = UNMAPPED_COLOR, UNLABELED_COLOR
    return colors


# --------------------------------------------------------------------------- insights
KIND_STYLE = {   # kind: (accent, icon, tag)
    "risk": (BAD_COLOR, "⛔", "Needs action"),
    "watch": ("#c98500", "⚠️", "Watch"),
    "good": (GOOD_COLOR, "✅", "Going well"),
    "info": (BASE_COLOR, "💡", "Insight"),
}
_KIND_ORDER = {"risk": 0, "watch": 1, "good": 2, "info": 3}


def insights(df, score, fmt_money, limit=6):
    """Plain-language findings for leaders, business units first. Each: kind, title, detail, impact, dim, value (what to open)."""
    items = []
    total = float(df.loc[df["period"] == "current", "net_cost"].sum())
    if total <= 0:
        return items
    named = score[score["value"].map(is_unit).astype(bool) & (score["cost"] > 0)]
    # 1. the biggest unit and its biggest application
    if len(named) >= 2:
        top = named.iloc[0]
        detail = (f"{fmt_money(top['cost'])} across {int(top['apps'])} application(s); {top['top_app']} alone is {top['top_app_share']:.0%} of the unit."
                  if pd.notna(top["top_app_share"]) else f"{fmt_money(top['cost'])} in the selected period.")
        items.append(dict(kind="info", impact=float(top["cost"]), dim=BU, value=top["value"],
                          title=f"{top['value']} is the largest business unit: {top['share']:.0%} of spend", detail=detail))
    # 2. spend that cannot be charged to a business unit
    unmapped = float(score.loc[score["value"] == UNMAPPED, "cost"].sum())
    if unmapped / total >= 0.05:
        names = ", ".join(f"{r['value']} ({fmt_money(r['cost'])})" for _, r in unmapped_features(df, 3).iterrows())
        items.append(dict(kind="risk" if unmapped / total >= 0.20 else "watch", impact=unmapped, dim=BU, value=UNMAPPED,
                          title=f"{fmt_money(unmapped)} ({unmapped / total:.0%}) is in features outside the BU billing plan",
                          detail=(f"Biggest: {names}. " if names else "") + "Add them to bu_mapping.json so they roll up to a unit."))
    unlabeled = float(score.loc[score["value"] == NOT_LABELED, "cost"].sum())
    if unlabeled / total >= 0.05:
        projects = group_table(select_rows(df, BU, NOT_LABELED), "project")
        where = (f"Largest: project {projects.iloc[0]['value']} ({fmt_money(projects.iloc[0]['cost'])}). "
                 if len(projects) and projects.iloc[0]["cost"] > 0 else "")
        items.append(dict(kind="risk" if unlabeled / total >= 0.30 else "watch", impact=unlabeled, dim=BU, value=NOT_LABELED,
                          title=f"{fmt_money(unlabeled)} ({unlabeled / total:.0%}) has no feature label",
                          detail=where + f"Without the '{source_keys(BU)[0]}' label it cannot be mapped to a business unit."))
    # 3. business units growing fast, with the application behind it
    grown = named[(named["prev"] > 0) & (named["delta_pct"] >= SPIKE_PCT) & (named["delta"] >= SPIKE_SHARE * total)]
    for _, r in grown.sort_values("delta", ascending=False).head(2).iterrows():
        drv = _driver(select_rows(df, BU, r["value"]))
        items.append(dict(kind="risk" if r["delta"] >= 0.08 * total else "watch", impact=float(r["delta"]), dim=BU, value=r["value"],
                          title=f"{r['value']} is up {r['delta_pct']:.0%} (+{fmt_money(r['delta'])})",
                          detail=(f"Mostly {drv[0]} (+{fmt_money(drv[1])}). " if drv else "") + "Confirm with the unit's owner that the growth is planned."))
    # 4. the unit that saved the most
    saved = named[(named["prev"] > 0) & (named["delta"] <= -SPIKE_SHARE * total / 2)]
    if len(saved):
        r = saved.sort_values("delta").iloc[0]
        drv = _driver(select_rows(df, BU, r["value"]), up=False)
        items.append(dict(kind="good", impact=float(-r["delta"]), dim=BU, value=r["value"],
                          title=f"{r['value']} cut spend by {abs(r['delta_pct']):.0%} (−{fmt_money(-r['delta'])})",
                          detail=(f"Biggest drop: {drv[0]} (−{fmt_money(-drv[1])}). " if drv else "") + "Worth sharing what worked with other units."))
    # 5. non-production heavy and 6. unowned spend inside large units
    big = named[named["cost"] >= 0.05 * total]
    heavy = big[big["nonprod_pct"] >= 0.40].sort_values("cost", ascending=False)
    if len(heavy):
        r = heavy.iloc[0]
        items.append(dict(kind="watch", impact=float(r["cost"] * r["nonprod_pct"]), dim=BU, value=r["value"],
                          title=f"{r['nonprod_pct']:.0%} of {r['value']} spend runs outside production",
                          detail=f"About {fmt_money(r['cost'] * r['nonprod_pct'])} in non-production. Shutdown schedules and smaller sizes usually apply."))
    unowned = big[big["owner_pct"] < 0.5].sort_values("cost", ascending=False)
    if len(unowned):
        r = unowned.iloc[0]
        items.append(dict(kind="watch", impact=float(r["cost"] * (1 - r["owner_pct"])), dim=BU, value=r["value"],
                          title=f"{1 - r['owner_pct']:.0%} of {r['value']} spend has no owner",
                          detail="Without an owner label nobody inside the unit is accountable for it. Ask the unit to tag its resources."))
    # 7. the biggest non-BU spike (an application, service, project ...)
    spikes = []
    for dim in SPIKE_DIMS:
        t = group_table(df, dim)
        t = t[(t["value"] != NOT_LABELED) & (t["prev"] > 0) & (t["delta_pct"] >= SPIKE_PCT) & (t["delta"] >= SPIKE_SHARE * total)]
        spikes += [(float(r["delta"]), dim, r) for _, r in t.iterrows()]
    if spikes:
        amount, dim, r = max(spikes, key=lambda s: s[0])
        items.append(dict(kind="watch", impact=amount, dim=dim, value=r["value"],
                          title=f"{TITLES[dim]} {r['value']} is up {r['delta_pct']:.0%} (+{fmt_money(r['delta'])})",
                          detail="Compared with the previous period. Open it to see which business units and resources drove it."))
    # 8. one service dominating the bill
    services = group_table(df, "service")
    if len(services) and services.iloc[0]["share"] >= 0.40:
        top = services.iloc[0]
        items.append(dict(kind="info", impact=float(top["cost"]), dim="service", value=top["value"],
                          title=f"{top['value']} is {top['share']:.0%} of all spend",
                          detail="One service dominates the bill. Check committed-use discounts and which units drive its usage."))
    return sorted(items, key=lambda i: (_KIND_ORDER[i["kind"]], -i["impact"]))[:limit]


def insight_card(item):
    """HTML for one insight card (styled by LEAD_CSS). Text comes from billing labels, so it is escaped."""
    accent, icon, tag = KIND_STYLE[item["kind"]]
    return (f'<div class="ins" style="--accent:{accent}"><div class="ins-tag">{icon} {tag}</div>'
            f'<div class="ins-title">{html.escape(item["title"])}</div>'
            f'<div class="ins-detail">{html.escape(item["detail"])}</div></div>')


# --------------------------------------------------------------------------- HTML: header, sections, KPI strip
LEAD_CSS = """
<style>
.lhead{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px;padding:6px 2px 12px;border-bottom:1px solid #e3e8ef;margin:-6px 0 6px}
.lhead b{font-size:1.15rem;letter-spacing:-.01em;color:#0F172A}
.lhead span{color:#64748B;font-size:.85rem}
.lsec{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin:28px 0 10px}
.lsec b{font-size:1.08rem;color:#0F172A;letter-spacing:-.01em}
.lsec span{color:#64748B;font-size:.86rem}
.kgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:6px 0 4px}
.ktile{background:#fff;border:1px solid #e3e8ef;border-radius:12px;padding:14px 16px 12px;box-shadow:0 1px 3px rgba(15,23,42,.05);border-top:4px solid var(--accent)}
.ktile-l{font-size:.72rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#64748B}
.ktile-v{font-size:1.75rem;font-weight:720;color:#0F172A;line-height:1.2;margin-top:4px;letter-spacing:-.02em}
.ktile-d{font-size:.84rem;margin-top:2px;color:#475569}
.ktile-f{font-size:.76rem;color:#64748B;margin-top:8px;padding-top:7px;border-top:1px dashed #e3e8ef}
.ins{background:#fff;border:1px solid #e3e8ef;border-left:5px solid var(--accent);border-radius:10px;padding:12px 14px 10px;
     box-shadow:0 1px 2px rgba(15,23,42,.04);min-height:128px;margin-bottom:6px}
.ins-tag{font-size:.72rem;font-weight:750;letter-spacing:.04em;text-transform:uppercase;color:var(--accent)}
.ins-title{color:#0F172A;font-size:1rem;font-weight:700;line-height:1.3;margin:4px 0}
.ins-detail{color:#64748B;font-size:.83rem;line-height:1.38}
.story{background:#fff;border:1px solid #e3e8ef;border-radius:10px;padding:12px 16px;color:#334155;font-size:.95rem;line-height:1.5;margin:4px 0 12px}
.story b{color:#0F172A}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;background:#fff;border:1px solid #e3e8ef;font-size:.8rem;color:#334155}
.chip i{width:9px;height:9px;border-radius:50%;display:inline-block}
.up{color:#d03b3b;font-weight:650}.down{color:#0a7d0a;font-weight:650}
</style>
"""


def header_bar(title, note=""):
    return f'<div class="lhead"><b>{html.escape(title)}</b><span>{html.escape(note)}</span></div>'


def section(title, note=""):
    return f'<div class="lsec"><b>{html.escape(title)}</b>{f"<span>{html.escape(note)}</span>" if note else ""}</div>'


def bu_chips(colors):
    """Legend row: one chip per business unit of the BU billing plan; hover shows its applications."""
    chips = "".join(f'<span class="chip" title="{html.escape(", ".join(MAPPING["apps_by_bu"][bu]), quote=True)}">'
                    f'<i style="background:{colors[bu]}"></i><b>{html.escape(bu)}</b>&nbsp;· {len(MAPPING["apps_by_bu"][bu])} apps</span>'
                    for bu in MAPPING["bus"])
    return f'<div class="chips">{chips}</div>'


def change_span(delta, delta_pct, fmt_money, suffix=""):
    """Coloured '▲ ₹1.2M (+23%)' for HTML. More spend is red, less is green."""
    if abs(delta) < 0.5:
        return f"no change{html.escape(suffix)}"
    cls = "up" if delta > 0 else "down"
    return f'<span class="{cls}">{html.escape(arrow_text(delta, delta_pct, fmt_money))}</span>{html.escape(suffix)}'


def kpi_tile(label, value, delta_html="", foot="", accent=BASE_COLOR):
    """One headline tile. ``delta_html`` is trusted HTML (from change_span); everything else is escaped."""
    return (f'<div class="ktile" style="--accent:{accent}"><div class="ktile-l">{html.escape(label)}</div>'
            f'<div class="ktile-v">{html.escape(str(value))}</div><div class="ktile-d">{delta_html or "&nbsp;"}</div>'
            f'<div class="ktile-f">{html.escape(foot) if foot else "&nbsp;"}</div></div>')


def kpi_strip(sm, fmt_money):
    """The five headline tiles: spend, run rate, business units, mapped share, non-production."""
    has_prev = sm["prev"] > 0
    spend_delta = change_span(sm["delta"], sm["delta_pct"], fmt_money, " vs previous") if has_prev else "no previous-period data"
    if pd.isna(sm["assigned_prev_pct"]):
        tag_delta = ""
    else:
        moved = round((sm["assigned_pct"] - sm["assigned_prev_pct"]) * 100)
        tag_delta = (f'<span class="{"down" if moved > 0 else "up"}">{moved:+d} pts</span> vs previous' if moved else "no change vs previous")
    tag_color = GOOD_COLOR if sm["assigned_pct"] >= 0.9 else WARN_COLOR if sm["assigned_pct"] >= 0.6 else BAD_COLOR
    top = sm["top_bu"]
    tiles = [
        kpi_tile("Net spend", fmt_money(sm["cost"]), spend_delta, f"Credits applied {fmt_money(sm['credits'])}"),
        kpi_tile("Run rate / 30 days", fmt_money(sm["run_rate"]), f"{fmt_money(sm['run_rate'] / 30)} per day on average",
                 "Based on complete billing days", "#6250d6"),
        kpi_tile("Business units", f"{sm['bu_count']}", (f"Largest: <b>{html.escape(_short(top[0], 22))}</b> ({top[2]:.0%})" if top else "none mapped yet"),
                 f"{sm['app_count']} applications with spend", "#1baf7a"),
        kpi_tile("Mapped to a unit", f"{sm['assigned_pct']:.0%}", tag_delta,
                 f"{fmt_money(sm['unmapped'])} unmapped · {fmt_money(sm['unlabeled'])} no label", tag_color),
        kpi_tile("Non-production", f"{sm['nonprod_pct']:.0%}", f"{fmt_money(sm['nonprod'])} of tagged environments",
                 "Dev / test / staging spend", "#eb6834"),
    ]
    return '<div class="kgrid">' + "".join(tiles) + "</div>"


def focus_story(r, rows, n_units, fmt_money):
    """Short summary of one business unit for the deep-dive section. Returns trusted HTML."""
    name = html.escape(bu_name(r["value"]))
    rank = f", #{int(r['rank'])} of {n_units}" if is_unit(r["value"]) else ""
    parts = [f"<b>{name}</b> spent <b>{html.escape(fmt_money(r['cost']))}</b> ({r['share']:.0%} of total{rank})."]
    if r["prev"] > 0:
        drv = _driver(rows, up=r["delta"] >= 0)
        why = f", mainly <b>{html.escape(str(drv[0]))}</b> ({html.escape(arrow_text(drv[1], np.nan, fmt_money))})" if drv else ""
        parts.append(f"That is {change_span(r['delta'], r['delta_pct'], fmt_money)} vs the previous period{why}." if abs(r["delta"]) >= 0.5
                     else "Spend is unchanged from the previous period.")
    else:
        parts.append("There was no spend in the previous period.")
    if r["top_app"] != "—" and pd.notna(r["top_app_share"]):
        service = f" and its biggest service <b>{html.escape(str(r['top_service']))}</b>" if r["top_service"] != "—" else ""
        parts.append(f"Its biggest application is <b>{html.escape(str(r['top_app']))}</b> ({r['top_app_share']:.0%} of the unit){service}.")
    return '<div class="story">' + " ".join(parts) + "</div>"


# --------------------------------------------------------------------------- spend pies
def pie_slices(t, dim=None, top=8, color_map=None):
    """Slices for one pie: the biggest values, an 'Other' slice for the rest, and the not-labeled / unmapped buckets last.

    ``key`` is what a click opens (None for 'Other': it is not one thing)."""
    t = t[t["cost"] > 0]
    special = list(NON_BU) if dim == BU else [NOT_LABELED]
    named = t[~t["value"].isin(special)]
    rows = [dict(label=display(dim, r["value"]), key=r["value"], cost=r["cost"], prev=r["prev"],
                 color=(color_map or {}).get(r["value"], PIE_COLORS[i % len(PIE_COLORS)]))
            for i, (_, r) in enumerate(named.head(top).iterrows())]
    rest = named.iloc[top:]
    if len(rest):
        rows.append(dict(label=f"Other ({len(rest)})", key=None, cost=rest["cost"].sum(), prev=rest["prev"].sum(), color=OTHER_COLOR))
    for v in special:
        miss = t[t["value"] == v]
        if len(miss):
            rows.append(dict(label=display(dim, v), key=v, cost=miss["cost"].sum(), prev=miss["prev"].sum(),
                             color=UNMAPPED_COLOR if v == UNMAPPED else UNLABELED_COLOR))
    out = pd.DataFrame(rows, columns=["label", "key", "cost", "prev", "color"])
    out["delta"] = out["cost"] - out["prev"]
    out["delta_pct"] = np.where(out["prev"] > 0, out["delta"] / out["prev"].where(out["prev"] > 0, 1), np.nan)
    out["share"] = out["cost"] / out["cost"].sum() if len(out) else 0.0
    return out


def _pie(slices, title, subtitle, fmt_money, clickable, height=PIE_HEIGHT):
    """Donut with the total in the middle and a legend (name · share) on the right."""
    names = _short_unique(slices["label"].tolist(), 24)
    legend = [f"{n}  {s:.0%}" for n, s in zip(names, slices["share"])]
    change = [arrow_text(r["delta"], r["delta_pct"], fmt_money) if r["prev"] > 0 else "new" for _, r in slices.iterrows()]
    fig = go.Figure(go.Pie(
        labels=legend, values=slices["cost"], sort=False, direction="clockwise", hole=0.58,
        marker=dict(colors=slices["color"].tolist(), line=dict(color="white", width=2)),
        text=[f"{s:.0%}" if s >= 0.06 else "" for s in slices["share"]], textinfo="text", textposition="inside",
        insidetextorientation="horizontal", textfont=dict(color="white", size=12, family=FONT),
        customdata=np.column_stack([slices["label"].values, slices["cost"].map(fmt_money).values, slices["prev"].map(fmt_money).values, change]),
        hovertemplate=("<b>%{customdata[0]}</b><br>Net cost: <b>%{customdata[1]}</b> · %{percent}<br>Previous period: %{customdata[2]}"
                       "<br>Change: %{customdata[3]}" + ("<br><i>Click for details</i>" if clickable else "") + "<extra></extra>"),
        domain=dict(x=[0.0, 0.52], y=[0.0, 1.0])))
    fig.add_annotation(text=f"<b>{fmt_money(slices['cost'].sum())}</b><br><span style='font-size:11px;color:#64748B'>total spend</span>",
                       x=0.26, y=0.5, xref="paper", yref="paper", showarrow=False, font=dict(size=16, color=INK, family=FONT))
    card_layout(fig, title, subtitle, height, legend=True)
    fig.update_layout(legend=dict(orientation="v", x=0.58, y=0.5, xanchor="left", yanchor="middle", font=dict(size=12, color=TEXT),
                                  itemclick=False, itemdoubleclick=False, tracegroupgap=6))
    return fig


def spend_pie(rows, by, fmt_money, title=None, subtitle=None, top=8, color_map=None, clickable=True, height=PIE_HEIGHT):
    """Donut of spend for one dimension. Returns (figure or None, lookup, options): ``options`` are the values a user can open."""
    slices = pie_slices(group_table(rows, by), by, top, color_map)
    if slices.empty:
        return None, [], []
    fig = _pie(slices, title or f"Spend by {TITLES[by].lower()}",
               subtitle if subtitle is not None else ("Share of net cost · click a slice for details." if clickable else "Share of net cost."),
               fmt_money, clickable, height)
    return fig, [slices["key"].tolist()], [k for k in slices["key"] if k is not None]


def breakdown_pie(rows, by, fmt_money, top=8):
    """Non-clickable donut used inside the details pop-up. Returns (figure or None, full table)."""
    t = group_table(rows, by)
    t = t[t["cost"] > 0]
    if t.empty:
        return None, t
    return _pie(pie_slices(t, by, top), f"By {TITLES[by].lower()}", "Share of this selection.", fmt_money, clickable=False), t


def bu_sunburst(df, colors, fmt_money, top_apps=10):
    """Business unit -> application, sized by net cost (current period). Click a unit to zoom into its applications."""
    cur = df[df["period"] == "current"]
    g = cur.groupby([BU, APP])["net_cost"].sum().reset_index()
    g = g[g["net_cost"] > 0]
    if g.empty:
        return None
    units = g.groupby(BU)["net_cost"].sum().sort_values(ascending=False)
    order = [u for u in units.index if is_unit(u)] + [u for u in NON_BU if u in units.index]
    total = float(units.sum())
    nodes = []   # (id, label, parent, value, colour, cost text, share text)
    for u in order:
        color = colors.get(u, OTHER_COLOR)
        nodes.append((u, bu_name(u), "", float(units[u]), color, fmt_money(units[u]), f"{units[u] / total:.0%} of total"))
        children = g[g[BU] == u].sort_values("net_cost", ascending=False)
        for _, r in children.head(top_apps).iterrows():
            nodes.append((f"{u}/{r[APP]}", _short(display(APP, r[APP]), 22), u, float(r["net_cost"]), _alpha(color, 0.62),
                          fmt_money(r["net_cost"]), f"{r['net_cost'] / units[u]:.0%} of {bu_name(u)}"))
        rest = children.iloc[top_apps:]
        if len(rest):
            cost = float(rest["net_cost"].sum())
            nodes.append((f"{u}/__other", f"Other ({len(rest)})", u, cost, _alpha(color, 0.35), fmt_money(cost), f"{cost / units[u]:.0%} of {bu_name(u)}"))
    ids, labels, parents, values, node_colors, cost_text, share_text = zip(*nodes)
    fig = go.Figure(go.Sunburst(
        ids=ids, labels=labels, parents=parents, values=values, branchvalues="total", sort=False,
        marker=dict(colors=list(node_colors), line=dict(color="white", width=2)), insidetextorientation="radial",
        textfont=dict(family=FONT, size=12), customdata=np.column_stack([cost_text, share_text]), maxdepth=2,
        hovertemplate="<b>%{label}</b><br>Net cost: <b>%{customdata[0]}</b><br>%{customdata[1]}<extra></extra>"))
    finish(fig, "Business units and their applications", "Inner ring = business unit, outer ring = application · click a unit to zoom in.", 470, legend=False)
    fig.update_layout(margin=dict(l=8, r=8, b=8))
    return fig


# --------------------------------------------------------------------------- trends and change
def complete_days(daily):
    """Drop the trailing days whose billing is still arriving, so daily lines do not end in a false cliff.

    The export lags, so the last day or two can be far below normal: a trailing day is dropped while its total spend is under
    half the median of the seven days before it (at most three days, measured on all spend in ``daily``)."""
    d = daily.copy()
    d["usage_date"] = pd.to_datetime(d["usage_date"])
    totals = d[d["period"] == "current"].groupby("usage_date")["net_cost"].sum().sort_index()
    cut = None
    for _ in range(3):
        if len(totals) < 9 or totals.iloc[-1] >= 0.5 * totals.iloc[-8:-1].median():
            break
        cut, totals = totals.index[-1], totals.iloc[:-1]
    return d[d["usage_date"] < cut] if cut is not None else d


def bu_trend(daily, score, colors, start_day, end_day, fmt_money):
    """Daily spend stacked by business unit, with the previous period's total as a dotted line (complete days only)."""
    d = complete_days(daily)
    cur = d[d["period"] == "current"].pivot_table(index="usage_date", columns=BU, values="net_cost", aggfunc="sum", fill_value=0.0).sort_index()
    if cur.empty:
        return None
    fig = go.Figure()
    for g in [v for v in score["value"] if v in cur.columns]:
        fig.add_trace(go.Scatter(
            x=cur.index, y=cur[g], name=_short(bu_name(g), 22), mode="lines", stackgroup="bu",
            line=dict(width=1, color="white"), fillcolor=colors.get(g, OTHER_COLOR),
            customdata=cur[g].map(fmt_money).values, hovertemplate=f"{html.escape(bu_name(g))}: <b>%{{customdata}}</b><extra></extra>"))
    days = (pd.Timestamp(end_day) - pd.Timestamp(start_day)).days + 1
    prev = d[d["period"] == "previous"].groupby("usage_date")["net_cost"].sum().sort_index()
    if len(prev):
        fig.add_trace(go.Scatter(
            x=prev.index + pd.Timedelta(days=days), y=prev.values, name="Previous period (total)", mode="lines",
            line=dict(color=INK, width=2, dash="dot"), customdata=prev.map(fmt_money).values,
            hovertemplate="Previous period total: <b>%{customdata}</b><extra></extra>"))
    finish(fig, "Daily spend by business unit", "Stacked net cost per complete day · dotted line = previous period's daily total, shifted to line up.", 470)
    fig.update_layout(hovermode="x unified", legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="left", x=0, font=dict(size=11)),
                      margin=dict(b=70))
    fig.update_xaxes(tickformat="%d %b")
    fig.update_yaxes(tickformat=",.2s", rangemode="tozero")
    return fig


def bridge(df, dim, fmt_money, n=8, title=None, subtitle=None, clickable=True):
    """Waterfall from the previous period's total to the current one, one bar per biggest mover of ``dim``."""
    t = group_table(df, dim)
    prev_total, cur_total = float(t["prev"].sum()), float(t["cost"].sum())
    if prev_total <= 0:
        return None, []
    t = t[t["delta"].abs() >= 0.5]
    movers = t.reindex(t["delta"].abs().sort_values(ascending=False).index)
    top, rest = movers.head(n).sort_values("delta", ascending=False), movers.iloc[n:]
    labels = ["Previous period"] + _short_unique([display(dim, v) for v in top["value"]], 24)
    values, measures, keys = [prev_total] + top["delta"].tolist(), ["absolute"] + ["relative"] * len(top), [None] + top["value"].tolist()
    texts = [f"<b>{fmt_money(prev_total)}</b>"] + [arrow_text(r["delta"], r["delta_pct"], fmt_money) for _, r in top.iterrows()]
    rest_delta = float(rest["delta"].sum())
    if abs(rest_delta) >= 0.5:
        labels.append(f"{len(rest)} others")
        values.append(rest_delta)
        measures.append("relative")
        keys.append(None)
        texts.append(arrow_text(rest_delta, np.nan, fmt_money))
    labels.append("Selected period")
    values.append(0)
    measures.append("total")
    keys.append(None)
    texts.append(f"<b>{fmt_money(cur_total)}</b>")
    fig = go.Figure(go.Waterfall(
        orientation="h", y=labels, x=values, measure=measures, text=texts, textposition="outside", cliponaxis=False,
        textfont=dict(size=11, color=TEXT), connector=dict(line=dict(color="#CBD5E1", width=1)),
        increasing=dict(marker=dict(color=UP_COLOR)), decreasing=dict(marker=dict(color=DOWN_COLOR)),
        totals=dict(marker=dict(color="#64748B")),
        hovertemplate="<b>%{y}</b><br>%{text}" + ("<br><i>Click for details</i>" if clickable else "") + "<extra></extra>"))
    running = np.cumsum(values[:-1])
    low, high = min(prev_total, cur_total, *running), max(prev_total, cur_total, *running)
    pad = (high - low) * 0.35 or high * 0.1
    fig.update_xaxes(range=[max(0, low - pad), high + pad], showticklabels=False, showgrid=False)
    fig.update_yaxes(title="", automargin=True, autorange="reversed")
    fig.update_layout(bargap=0.3)
    finish(fig, title or f"What moved spend, by {TITLES[dim].lower()}",
           subtitle or "From the previous period's total to this one · red = more spend, green = less" + (" · click a bar." if clickable else "."),
           max(300, 110 + 34 * len(labels)), legend=False)
    return fig, [keys]


def bu_matrix(df, by, fmt_money, top_x=10):
    """Heatmap: which business unit spends on which application / service / project ... (current period)."""
    cur = df[df["period"] == "current"]
    if cur["net_cost"].sum() <= 0:
        return None
    bus = cur.groupby(BU)["net_cost"].sum().sort_values(ascending=False)
    rows = [v for v in bus.index if is_unit(v) and bus[v] > 0] + [v for v in NON_BU if bus.get(v, 0) > 0]
    cols_all = cur.groupby(by)["net_cost"].sum().sort_values(ascending=False)
    cols = [v for v in cols_all.index if cols_all[v] > 0][:top_x]
    sub = cur[cur[BU].isin(rows)].copy()
    sub["col"] = np.where(sub[by].isin(cols), sub[by], "Other")
    p = sub.pivot_table(index=BU, columns="col", values="net_cost", aggfunc="sum", fill_value=0.0)
    order = cols + (["Other"] if "Other" in p.columns else [])
    p = p.reindex(index=rows, columns=order, fill_value=0.0)
    total = float(cur["net_cost"].sum())
    row_share = p.div(p.sum(axis=1).where(lambda s: s > 0), axis=0).fillna(0)
    text = [[fmt_money(v) if v >= 0.01 * total else "" for v in row] for row in p.values]
    custom = np.dstack([np.vectorize(fmt_money)(p.values), (row_share.values * 100).round(0)])
    fig = go.Figure(go.Heatmap(
        z=p.where(p > 0).values, x=_short_unique([display(by, v) for v in order], 20), y=_short_unique([bu_name(v) for v in rows], 22),
        colorscale=HEAT_SCALE, showscale=False, xgap=2, ygap=2, text=text, texttemplate="%{text}", textfont=dict(size=11), customdata=custom,
        hovertemplate="<b>%{y}</b> · %{x}<br>Net cost: <b>%{customdata[0]}</b><br>%{customdata[1]:.0f}% of this unit's spend<extra></extra>"))
    fig.update_xaxes(side="top", tickangle=-30, showgrid=False)
    fig.update_yaxes(autorange="reversed", showgrid=False, automargin=True)
    finish(fig, f"Business unit × {TITLES[by].lower()}", "Net cost in the selected period · darker = more spend · amounts shown above 1% of total.",
           max(340, 190 + 40 * len(rows)), legend=False)
    fig.update_layout(margin=dict(t=150))
    return fig


def focus_trend(daily, bu, start_day, end_day, color=BASE_COLOR):
    """Daily cost of one business unit vs the previous period (shifted to line up), complete days only."""
    d = complete_days(daily)
    d = d[d[BU] == bu]
    if d.empty:
        return None
    days = (pd.Timestamp(end_day) - pd.Timestamp(start_day)).days + 1
    cur = d[d["period"] == "current"].groupby("usage_date")["net_cost"].sum().sort_index()
    prev = d[d["period"] == "previous"].groupby("usage_date")["net_cost"].sum().sort_index()
    fig = go.Figure()
    if len(prev):
        fig.add_trace(go.Scatter(x=prev.index + pd.Timedelta(days=days), y=prev.values, mode="lines", name="Previous period",
                                 line=dict(color=UNLABELED_COLOR, width=2, dash="dot"), customdata=prev.index.strftime("%d %b"),
                                 hovertemplate="Previous period · %{customdata}<br><b>%{y:,.0f}</b><extra></extra>"))
    fig.add_trace(go.Scatter(x=cur.index, y=cur.values, mode="lines", name="Selected period", line=dict(color=color, width=2.5),
                             fill="tozeroy", fillcolor=_alpha(color, 0.12), hovertemplate="%{x|%d %b %Y}<br><b>%{y:,.0f}</b><extra></extra>"))
    finish(fig, "Daily spend", "Selected period vs the period before it · days with incomplete billing left out.", 360)
    fig.update_xaxes(tickformat="%d %b")
    fig.update_yaxes(tickformat=",.2s", rangemode="tozero")
    return fig


# --------------------------------------------------------------------------- mapping / tagging health
def coverage_bars(df):
    """Share of spend that is mapped to a business unit or carries each label. Click a bar to open the spend missing it.

    Lookup keys are ``(dim, value)`` pairs to open: unmapped spend for the business unit bar, unlabeled spend otherwise."""
    cur = df[df["period"] == "current"]
    total = float(cur["net_cost"].sum())
    if total <= 0:
        return None, []
    rows = []
    for d in COVERAGE_DIMS:
        missing = ~cur[BU].map(is_unit).astype(bool) if d == BU else cur[d] == NOT_LABELED
        pct = min(max(float(cur.loc[~missing, "net_cost"].sum()) / total, 0.0), 1.0)
        rows.append((d, pct, (BU, UNMAPPED) if d == BU else (d, NOT_LABELED)))
    rows.sort(key=lambda r: r[1])

    def verdict(p):
        return ("✓ good", GOOD_COLOR) if p >= 0.9 else ("! fair", WARN_COLOR) if p >= 0.6 else ("✕ poor", BAD_COLOR)
    names = {BU: "Mapped to a BU", FEATURE: "Feature label", CLUSTER: "Feature cluster"}
    fig = go.Figure(go.Bar(
        x=[p * 100 for _, p, _ in rows], y=[names.get(d, TITLES[d]) for d, _, _ in rows], orientation="h",
        marker=dict(color=[verdict(p)[1] for _, p, _ in rows], line=dict(width=0)),
        text=[f"<b>{p:.0%}</b> · {verdict(p)[0]}" for _, p, _ in rows], textposition="outside", cliponaxis=False,
        textfont=dict(size=11, color=TEXT),
        hovertemplate="<b>%{y}</b><br>%{x:.0f}% of spend<br><i>Click to see the spend missing it</i><extra></extra>"))
    fig.update_xaxes(range=[0, 135], showticklabels=False, showgrid=False)
    fig.update_yaxes(title="", automargin=True)
    fig.update_layout(bargap=0.34)
    finish(fig, "Mapping and label coverage", "Share of net cost · good ≥ 90%, fair ≥ 60% · click a bar.", 360, legend=False)
    return fig, [[key for _, _, key in rows]]


def weekly_assignment(daily, end_day):
    """Share of spend mapped to a business unit per 7-day bucket (newest bucket ends on ``end_day``)."""
    cur = daily[daily["period"] == "current"].copy()
    if cur.empty:
        return pd.DataFrame(columns=["label", "assigned", "unassigned"])
    cur["usage_date"] = pd.to_datetime(cur["usage_date"])
    cur["attributed"] = cur[BU].map(is_unit).astype(bool)
    cur["bucket"] = ((pd.Timestamp(end_day) - cur["usage_date"]).dt.days // 7)
    cur["net_cost"] = cur["net_cost"].clip(lower=0)
    g = cur.pivot_table(index="bucket", columns="attributed", values="net_cost", aggfunc="sum", fill_value=0.0)
    for flag in (True, False):
        if flag not in g:
            g[flag] = 0.0
    total = (g[True] + g[False]).replace(0, np.nan)
    out = pd.DataFrame({"assigned": (g[True] / total * 100).fillna(0), "unassigned": (g[False] / total * 100).fillna(0)})
    out["label"] = [(pd.Timestamp(end_day) - pd.Timedelta(days=int(b) * 7 + 6)).strftime("%d %b") for b in out.index]
    return out.sort_index(ascending=False).reset_index(drop=True)


def tagging_trend(daily, end_day):
    """Weekly 100% bars: spend mapped to a business unit vs not."""
    w = weekly_assignment(daily, end_day)
    if w.empty:
        return None
    fig = go.Figure()
    for col, name, color in (("assigned", "Mapped to a business unit", GOOD_COLOR), ("unassigned", "Unmapped / no label", BAD_COLOR)):
        fig.add_trace(go.Bar(x=w["label"], y=w[col], name=name, marker=dict(color=color, line=dict(color="white", width=1)),
                             text=[f"{v:.0f}%" if v >= 8 else "" for v in w[col]], textposition="inside", insidetextanchor="middle",
                             textfont=dict(color="white", size=11, family=FONT),
                             hovertemplate="Week of %{x}<br>" + name + ": <b>%{y:.0f}%</b><extra></extra>"))
    fig.update_layout(barmode="stack", bargap=0.22)
    finish(fig, "Mapping trend", "Share of each week's spend that rolls up to a business unit.", 360)
    fig.update_xaxes(type="category")
    fig.update_yaxes(range=[0, 100], ticksuffix="%", tickvals=[0, 25, 50, 75, 100])
    return fig


# --------------------------------------------------------------------------- executive overview (hero, KPI cards, card panels)
EXEC_CSS = """
<style>
.xhero{position:relative;overflow:hidden;display:flex;justify-content:space-between;align-items:center;gap:24px;
       padding:22px 28px;border-radius:16px;color:#fff;margin:0 0 14px;
       background:linear-gradient(115deg,#0f1f3d 0%,#14365a 52%,#0f7c86 100%)}
.xhero:after{content:"";position:absolute;right:-90px;top:-150px;width:420px;height:420px;border-radius:50%;
             border:70px solid rgba(255,255,255,.05);pointer-events:none}
.xhero-eye{font-size:.7rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:#9fe3e0}
.xhero h1{margin:6px 0 6px;font-size:1.7rem;font-weight:780;letter-spacing:-.02em;color:#fff;line-height:1.2;padding:0}
.xhero p{margin:0;color:#d4e3f2;font-size:.92rem;max-width:720px;line-height:1.45}
.xhero>div:first-child{flex:1;min-width:0}
.xhero-r{flex:none;text-align:right;z-index:1}
.xhero-r b{display:block;font-size:2rem;font-weight:780;letter-spacing:-.02em}
.xhero-r span{font-size:.8rem;color:#9fe3e0}
.xview{font-size:.72rem;font-weight:800;letter-spacing:.1em;color:#64748B;padding-top:9px}
.xnote{font-size:.76rem;color:#94A3B8;text-align:right;padding-top:10px}
.xkpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:4px 0 14px}
.xkpi{background:#fff;border:1px solid #e6eaf2;border-radius:16px;padding:16px 18px 14px;box-shadow:0 2px 10px rgba(15,23,42,.045)}
.xkpi-top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.xkpi-l{font-size:.8rem;color:#475569}
.xkpi-i{display:inline-flex;align-items:center;justify-content:center;min-width:26px;height:26px;border-radius:7px;font-size:.85rem;font-weight:700;
        color:var(--tone);background:color-mix(in srgb,var(--tone) 12%,white)}
.xkpi-v{font-size:1.6rem;font-weight:780;color:#0F172A;letter-spacing:-.02em;margin:10px 0 4px;line-height:1.15}
.xkpi-f{font-size:.76rem;color:#64748B}
.xcard{background:#fff;border:1px solid #e6eaf2;border-radius:16px;padding:16px 18px;box-shadow:0 2px 10px rgba(15,23,42,.045);
       box-sizing:border-box;overflow:auto;margin-bottom:16px}
.xcard h4{margin:0;padding:0;font-size:.98rem;font-weight:720;color:#0F172A}
.xcard .xsub{font-size:.78rem;color:#64748B;margin:3px 0 14px}
.xbar{display:grid;grid-template-columns:minmax(80px,1fr) 1.6fr auto;align-items:center;gap:10px;margin:0 0 9px;font-size:.84rem}
.xbar b{color:#0F172A;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.xbar .track{background:#EEF2F7;border-radius:999px;height:8px;overflow:hidden}
.xbar .fill{height:8px;border-radius:999px}
.xbar .amt{font-weight:700;color:#0F172A;text-align:right;white-space:nowrap}
.xbar .amt small{display:block;font-weight:600;font-size:.72rem}
.xopp{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid #EEF2F7}
.xopp:last-child{border-bottom:none}
.xopp b{display:block;font-size:.86rem;font-weight:620;color:#0F172A}
.xopp span{font-size:.74rem;color:#64748B}
.xopp .save{font-weight:750;color:#0a7d0a;font-size:.92rem;white-space:nowrap}
.xtable{width:100%;border-collapse:collapse;font-size:.84rem}
.xtable th{font-size:.66rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:#64748B;text-align:left;padding:6px 8px;border-bottom:1px solid #E2E8F0}
.xtable td{padding:9px 8px;border-bottom:1px solid #EEF2F7;color:#0F172A;vertical-align:middle}
.xtable td small{display:block;color:#64748B;font-size:.72rem}
.xtable td.num{text-align:right;white-space:nowrap}.xtable th.num{text-align:right}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.7rem;font-weight:700;white-space:nowrap}
.xact{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid #EEF2F7}
.xact:last-child{border-bottom:none}
.xact .n{flex:none;width:24px;height:24px;border-radius:7px;background:#EEF4FF;color:#1D4ED8;font-weight:800;font-size:.78rem;
         display:flex;align-items:center;justify-content:center}
.xact b{display:block;font-size:.88rem;color:#0F172A;font-weight:680}
.xact span{font-size:.76rem;color:#64748B;line-height:1.35}
.xempty{color:#64748B;font-size:.84rem;padding:18px 0;text-align:center}
[data-testid="stPlotlyChart"]{margin-bottom:16px}
</style>
"""
CHART_CARD = 14   # border + padding the app CSS puts around a Plotly chart; HTML cards add it to line up with charts


def exec_hero(total, caption, scope):
    """Dark hero banner of the executive overview."""
    return (f'<div class="xhero"><div><div class="xhero-eye">Executive overview · Google Cloud Platform</div>'
            f'<h1>Cloud spend, utilization &amp; savings — in one view</h1>'
            f'<p>Business-aligned visibility across GCP projects. Every resource is connected to its business unit and product, '
            f'optimization opportunities are sized, and savings are tracked through owner-approved actions. {html.escape(scope)}</p></div>'
            f'<div class="xhero-r"><b>{html.escape(total)}</b><span>{html.escape(caption)}</span></div></div>')


def exec_kpi(label, value, foot, icon, tone=BASE_COLOR, value_color="#0F172A"):
    """One executive KPI card (icon chip top right). ``foot`` is trusted HTML."""
    return (f'<div class="xkpi"><div class="xkpi-top"><span class="xkpi-l">{html.escape(label)}</span>'
            f'<span class="xkpi-i" style="--tone:{tone}">{html.escape(icon)}</span></div>'
            f'<div class="xkpi-v" style="color:{value_color}">{html.escape(str(value))}</div><div class="xkpi-f">{foot}</div></div>')


def exec_kpis(cards):
    return '<div class="xkpis">' + "".join(cards) + "</div>"


def html_card(title, subtitle, body, height):
    """A white card with a title and subtitle around trusted HTML ``body``; ``height`` matches the Plotly chart beside it."""
    return (f'<div class="xcard" style="height:{height + CHART_CARD}px"><h4>{html.escape(title)}</h4>'
            f'<div class="xsub">{html.escape(subtitle)}</div>{body}</div>')


def card_layout(fig, title, subtitle="", height=380, legend=False):
    """Card look for executive charts: compact bold title and a muted subtitle, matching the HTML cards."""
    top = 76 if subtitle else 52
    fig.update_layout(
        title=dict(text=f"<b>{html.escape(title)}</b>", xref="container", x=0, xanchor="left", yref="container", y=1, yanchor="top",
                   pad=dict(t=14, l=16), font=dict(size=15, color=INK, family=FONT)),
        height=height, margin=dict(l=16, r=16, t=top, b=16), paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF",
        font=dict(family=FONT, color=TEXT, size=12), showlegend=legend,
        legend=dict(orientation="h", x=1, xanchor="right", y=1.02, yanchor="bottom", bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
        hoverlabel=dict(bgcolor="white", bordercolor="#CBD5E1", font=dict(family=FONT, size=12, color=INK)))
    if subtitle:
        fig.add_annotation(text=html.escape(subtitle), xref="paper", yref="paper", x=0, y=1, yshift=top - 52, xanchor="left", yanchor="bottom",
                           showarrow=False, font=dict(size=12, color="#64748B", family=FONT))
    fig.update_xaxes(showgrid=False, zeroline=False, linecolor="#E2E8F0", tickcolor="#E2E8F0")
    fig.update_yaxes(gridcolor="#EEF2F7", zeroline=False, linecolor="rgba(0,0,0,0)")
    return fig


def period_trend(daily, start_day, end_day, fmt_money, height=380):
    """Grouped bars: spend per day / week / 30 days of the selected period next to the same slice of the previous period."""
    days = (pd.Timestamp(end_day) - pd.Timestamp(start_day)).days + 1
    step, unit = (1, "Daily") if days <= 14 else (7, "Weekly") if days <= 92 else (30, "30-day")
    d = daily.groupby(["period", "usage_date"], as_index=False)["net_cost"].sum()
    d["usage_date"] = pd.to_datetime(d["usage_date"])
    start, prev_start = pd.Timestamp(start_day), pd.Timestamp(start_day) - pd.Timedelta(days=days)
    n = -(-days // step)
    def buckets(rows, origin):
        b = ((rows["usage_date"] - origin).dt.days // step).clip(lower=0, upper=n - 1)
        return rows.groupby(b)["net_cost"].sum().reindex(range(n), fill_value=0.0)
    cur, prev = buckets(d[d["period"] == "current"], start), buckets(d[d["period"] == "previous"], prev_start)
    labels = [(start + pd.Timedelta(days=i * step)).strftime("%d %b") for i in range(n)]
    prev_labels = [(prev_start + pd.Timedelta(days=i * step)).strftime("%d %b") for i in range(n)]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=labels, y=prev.values, name="Previous", marker=dict(color="#9ec5f4", cornerradius=4),
                         customdata=np.column_stack([prev_labels, prev.map(fmt_money).values]),
                         hovertemplate="Previous period · from %{customdata[0]}<br><b>%{customdata[1]}</b><extra></extra>"))
    fig.add_trace(go.Bar(x=labels, y=cur.values, name="Latest", marker=dict(color=BASE_COLOR, cornerradius=4),
                         customdata=cur.map(fmt_money).values, hovertemplate="From %{x}<br><b>%{customdata}</b><extra></extra>"))
    fig.update_layout(barmode="group", bargap=0.38, bargroupgap=0.08)
    partial = " · last bar is a partial " + ("week" if step == 7 else "period") if days % step else ""
    card_layout(fig, "Cloud spend trend", f"{unit} net cost · selected period vs the one before it{partial}", height, legend=True)
    fig.update_xaxes(type="category")
    fig.update_yaxes(tickformat=",.2s", rangemode="tozero")
    return fig


def bu_bars_html(score, colors, fmt_money):
    """Rows of 'name · bar · amount (change)' per business unit, biggest first."""
    s = score[score["cost"] > 0]
    if s.empty:
        return '<div class="xempty">No spend for this selection.</div>'
    peak = s["cost"].max()
    rows = []
    for _, r in s.iterrows():
        if r["prev"] <= 0:
            change = '<small style="color:#64748B">new</small>'
        elif abs(r["delta"]) < 0.5:
            change = '<small style="color:#64748B">no change</small>'
        else:
            change = f'<small class="{"up" if r["delta"] > 0 else "down"}">{"▲" if r["delta"] > 0 else "▼"} {abs(r["delta_pct"]):.0%}</small>'
        rows.append(f'<div class="xbar"><b title="{html.escape(bu_name(r["value"]), quote=True)}">{html.escape(bu_name(r["value"]))}</b>'
                    f'<div class="track"><div class="fill" style="width:{max(r["cost"] / peak * 100, 1.5):.1f}%;background:{colors.get(r["value"], OTHER_COLOR)}"></div></div>'
                    f'<div class="amt">{html.escape(fmt_money(r["cost"]))}{change}</div></div>')
    return "".join(rows)


def schedule_saving(df, start_day, end_day):
    """Estimated monthly saving if non-production Compute Engine ran 12 hours on weekdays (60 of 168 hours)."""
    days = (pd.Timestamp(end_day) - pd.Timestamp(start_day)).days + 1
    cur = df[(df["period"] == "current") & (df["service"] == "Compute Engine") & (df["environment"] != NOT_LABELED)]
    nonprod = float(cur.loc[~cur["environment"].str.lower().isin(PROD_VALUES), "net_cost"].sum())
    return max(nonprod, 0.0) / days * 30 * (1 - 60 / 168)


def opportunities(recs, df, start_day, end_day):
    """(title, explanation, monthly amount) per savings lever, biggest first. ``recs`` may be None (VM data not loaded)."""
    items = []
    if recs is not None and len(recs):
        for status, title, sub in (("Over-provisioned", "Compute rightsizing", "VMs with sustained low CPU / memory → smaller machine type"),
                                   ("Idle", "Idle VM shutdown", "VMs almost unused in the selected period → stop or delete")):
            amount = float(recs.loc[recs["status"] == status, "monthly_saving"].sum(skipna=True))
            count = int((recs["status"] == status).sum())
            if count:
                items.append((title, f"{sub} · {count} VM{'s' if count != 1 else ''}", amount))
    sched = schedule_saving(df, start_day, end_day)
    if sched > 0:
        items.append(("Non-production schedules", "Dev / test Compute Engine run 12h on weekdays · estimate", sched))
    return sorted(items, key=lambda i: -i[2])


def opportunities_html(items, fmt_money, vm_loaded):
    rows = "".join(f'<div class="xopp"><div><b>{html.escape(t)}</b><span>{html.escape(s)}</span></div><div class="save">{html.escape(fmt_money(a))}</div></div>'
                   for t, s, a in items)
    if not vm_loaded:
        rows += '<div class="xopp"><div><b>VM rightsizing &amp; idle shutdown</b><span>Load VM utilization above to size these</span></div><div class="save" style="color:#94A3B8">—</div></div>'
    return rows or '<div class="xempty">No savings opportunities found for this selection.</div>'


PRIORITY = {   # status: (badge text, background, ink)
    "Idle": ("Shut down?", "#FEE2E2", "#991B1B"),
    "Over-provisioned": ("Resize", "#FEF3C7", "#92400E"),
    "Under-provisioned": ("Upsize", "#DBEAFE", "#1E40AF"),
}


def watchlist_html(recs, fmt_money, n=6):
    """Table of the underutilized VMs with the biggest estimated saving."""
    if recs is None:
        return '<div class="xempty">Load VM utilization above to see underutilized resources.</div>'
    d = recs[recs["status"].isin(["Idle", "Over-provisioned"])].sort_values("monthly_saving", ascending=False, na_position="last").head(n)
    if d.empty:
        return '<div class="xempty">No underutilized VMs in this selection. 🎉</div>'
    rows = []
    for _, r in d.iterrows():
        text, bg, ink = PRIORITY[r["status"]]
        cpu = f"CPU p95 {r['cpu_p95']:.0f}%" if pd.notna(r["cpu_p95"]) else "CPU n/a"
        mem = f" · RAM {r['mem_p95']:.0f}%" if pd.notna(r["mem_p95"]) else ""
        saving = fmt_money(r["monthly_saving"]) + "/mo" if pd.notna(r["monthly_saving"]) else "saving n/a"
        owner = " · ".join(display(c, r[c]) for c in (BU, APP) if r[c] != NOT_LABELED) or "no feature label"
        rows.append(f'<tr><td><b>{html.escape(_short(str(r["instance_name"]), 28))}</b><small>{html.escape(owner)}</small></td>'
                    f'<td>{html.escape(cpu + mem)}<small>{html.escape(saving)}</small></td>'
                    f'<td><span class="badge" style="background:{bg};color:{ink}">{text}</span></td></tr>')
    return '<table class="xtable"><tr><th>Resource</th><th>Signal</th><th>Priority</th></tr>' + "".join(rows) + "</table>"


def product_table_html(df, bu, vm_savings, fmt_money, n=12):
    """Business unit → feature cluster → product (application) cost table for one unit."""
    rows = select_rows(df, BU, bu)
    t = group_table(rows, APP)
    t = t[t["cost"] > 0].head(n)
    if t.empty:
        return '<div class="xempty">No product spend for this business unit.</div>'
    cur = rows[rows["period"] == "current"]
    cluster = (cur.groupby([APP, CLUSTER])["net_cost"].sum().reset_index().sort_values("net_cost", ascending=False)
               .drop_duplicates(APP).set_index(APP)[CLUSTER])
    body = []
    for i, (_, r) in enumerate(t.iterrows()):
        mapped = r["value"] in MAPPING["app_bu"]
        badge = ('<span class="badge" style="background:#D1FAE5;color:#065F46">Mapped</span>' if mapped else
                 '<span class="badge" style="background:#FEF3C7;color:#92400E">Not in plan</span>' if r["value"] != NOT_LABELED else
                 '<span class="badge" style="background:#F1F5F9;color:#475569">No label</span>')
        potential = vm_savings.get(r["value"]) if vm_savings is not None else None
        change = change_span(r["delta"], r["delta_pct"], fmt_money) if r["prev"] > 0 else '<span style="color:#64748B">new</span>'
        body.append(f'<tr><td>{"<b>" + html.escape(bu_name(bu)) + "</b>" if i == 0 else ""}</td>'
                    f'<td>{html.escape(display(CLUSTER, cluster.get(r["value"], NOT_LABELED)))}</td>'
                    f'<td><b>{html.escape(display(APP, r["value"]))}</b></td><td class="num">{html.escape(fmt_money(r["cost"]))}</td>'
                    f'<td class="num">{change}</td>'
                    f'<td class="num">{html.escape(fmt_money(potential)) if potential else "—"}</td><td>{badge}</td></tr>')
    return ('<table class="xtable"><tr><th>Business unit</th><th>Feature cluster</th><th>Product</th><th class="num">Spend</th>'
            '<th class="num">vs previous</th><th class="num">Potential / mo</th><th>Mapping</th></tr>' + "".join(body) + "</table>")


def actions_html(items, n=5):
    """Numbered list of recommended leadership actions (from ``insights``)."""
    if not items:
        return '<div class="xempty">Nothing stands out: spend is mapped, stable and not concentrated.</div>'
    return "".join(f'<div class="xact"><div class="n">{i}</div><div><b>{html.escape(a["title"])}</b><span>{html.escape(a["detail"])}</span></div></div>'
                   for i, a in enumerate(items[:n], start=1))


def unit_sql():
    """BigQuery condition: the row rolls up to a real business unit."""
    return f"{dim_sql(BU)} NOT IN ({_lit(UNMAPPED)},{_lit(NOT_LABELED)})"


def map_feature(feature, cluster_label=NOT_LABELED):
    """(application, business unit, feature cluster) for a feature label value, exactly like ``dim_sql``."""
    f, c = _norm(feature), _norm(cluster_label)
    app = MAPPING["feature_app"].get(f, f)
    bu = MAPPING["app_bu"].get(app) or (NOT_LABELED if f == NOT_LABELED else UNMAPPED)
    cluster = MAPPING["feature_cluster"].get(f) or MAPPING["cluster_alias"].get(c, c)
    return app, bu, cluster


def vm_org(recs):
    """Right-sizing rows with business_unit / application / feature_cluster read from each VM's own labels ('k=v; k=v')."""
    out = recs.copy()
    feature_keys, cluster_keys = label_keys(FEATURE), label_keys(CLUSTER)
    mapped = []
    for text in out["labels"].fillna("").astype(str):
        labels = {}
        for pair in text.split("; "):
            if "=" in pair:
                k, v = pair.split("=", 1)
                labels[k.strip().lower()] = v.strip()
        feature = next((labels[k] for k in feature_keys if labels.get(k)), NOT_LABELED)
        cluster = next((labels[k] for k in cluster_keys if labels.get(k)), NOT_LABELED)
        mapped.append((_norm(feature) if feature != NOT_LABELED else NOT_LABELED, *map_feature(feature, cluster)))
    out[FEATURE], out[APP], out[BU], out[CLUSTER] = zip(*mapped) if mapped else ([], [], [], [])
    return out


def filter_org(frame, org):
    """Keep rows matching the chosen (business units, feature clusters, applications); empty = all."""
    for dim, chosen in zip((BU, CLUSTER, APP), org):
        if chosen:
            frame = frame[frame[dim].isin(chosen)]
    return frame


# --------------------------------------------------------------------------- details pop-up
def drill_trend(daily, value):
    """Daily cost line for one selection (data from the drill-down query)."""
    d = daily.copy()
    d["usage_date"] = pd.to_datetime(d["usage_date"])
    d = d.groupby("usage_date", as_index=False)["net_cost"].sum().sort_values("usage_date")
    fig = go.Figure(go.Scatter(x=d["usage_date"], y=d["net_cost"], mode="lines", fill="tozeroy", line=dict(color=BASE_COLOR, width=2.5),
                               fillcolor=_alpha(BASE_COLOR, 0.12), hovertemplate="%{x|%d %b %Y}<br><b>%{y:,.0f}</b><extra></extra>"))
    finish(fig, "Daily net cost", f"Selected period · {value}", 300, legend=False)
    fig.update_yaxes(tickformat=",.2s")
    return fig


def describe(dim, value):
    """(title, subtitle) for the details pop-up."""
    title = TITLES[dim]
    if dim == BU and value == UNMAPPED:
        return "Spend outside the BU billing plan", "Resources whose feature label is not mapped to a business unit in bu_mapping.json."
    if dim == BU and value == NOT_LABELED:
        return "Spend with no feature label", "Resources with no feature label, so they cannot be mapped to a business unit."
    if value == NOT_LABELED:
        return f"Spend with no {title.lower()} label", "Resources that carry no usable label. Tag them so the cost can be assigned and reviewed."
    return f"{title}: {value}", f"All spend where {title.lower()} is '{value}', for the selected dates."


def resolve_click(points, lookup):
    """Return the key of the first clicked point, or None. Accepts snake_case and camelCase point keys."""
    for p in points or []:
        curve = p.get("curve_number", p.get("curveNumber", 0))
        index = p.get("point_index", p.get("pointIndex", p.get("point_number", p.get("pointNumber"))))
        try:
            return lookup[int(curve)][int(index)]
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return None
