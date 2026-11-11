"""VM right-sizing logic (pure functions, no Streamlit or Google API calls).

Method
------
* Demand is measured on the p95 of hourly *peak* utilization, so short spikes are respected.
* Needed capacity = provisioned capacity x observed p95 / target utilization.
* Candidates are predefined types of the same machine series (e.g. e2, n2) plus, for custom
  VMs, a resized custom shape. The cheapest candidate that satisfies both needs is chosen.
* Price is estimated with a vCPU:GB-RAM weight (GCP list prices for E2/N1/N2 are roughly 7.5:1).
  Estimated saving = the VM's actual billed compute cost x (1 - new_weight / current_weight).
* When RAM is not reported (no Ops Agent) RAM is NOT reduced, only vCPUs, and confidence is lowered.
"""
from dataclasses import dataclass
import math
import re

import numpy as np
import pandas as pd

CPU_WEIGHT = 7.5  # relative price of 1 vCPU versus 1 GB of RAM
SHARED_CORE = {"e2-micro", "e2-small", "e2-medium", "f1-micro", "g1-small"}
GPU_SERIES = {"a2", "a3", "a4", "g2", "g4"}
CUSTOM_RE = re.compile(r"^(?:(?P<series>[a-z0-9]+)-)?custom-(?P<cpu>\d+)-(?P<mem>\d+)(?:-ext)?$")
PREDEFINED_RE = re.compile(r"^(?P<series>[a-z0-9]+)-(?P<shape>standard|highmem|highcpu)-(?P<cpu>\d+)$")

STATUS_ORDER = ["Idle", "Over-provisioned", "Right-sized", "Under-provisioned", "Insufficient data", "Not evaluated"]

# ---- VM labels -------------------------------------------------------------------------------
# Teams spell the same label differently (app_dev_owner / app-dev-owner / application-development-owner ...).
# Keys are compared after lower-casing and dropping every non-alphanumeric character, so all the
# spellings below map to one dashboard column. Aliases are listed in priority order.
NOT_LABELED = "(not labeled)"
LABEL_FIELDS = {
    # column: (title shown in the dashboard, normalized label-key aliases)
    "owner": ("Owner", ["owner"]),
    "client": ("Client", ["client"]),
    "environment": ("Environment", ["environment", "env"]),
    "department": ("Department", ["department", "dept"]),
    "application": ("Application", ["application", "applicationname", "applicationsname", "appname"]),
    "dev_owner": ("App dev owner", ["appdevowner", "applicationdevelopmentowner", "applicationdevowner", "appdevelopmentowner"]),
    "support_owner": ("App support owner", ["appsupportowner", "appsuppportowner", "applicationsupportowner", "applicationsupport", "appsupport"]),
    "in_use_by": ("In use by", ["inuseby", "usedby", "inuse"]),
}
LABEL_COLUMNS = list(LABEL_FIELDS)
_EMPTY_LABEL_VALUES = {"", "none", "na", "n/a", "null", "nil", "-", "unknown"}


def _norm_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def extract_labels(labels):
    """Map a raw GCP label dict to the dashboard label columns (blank/'none' -> NOT_LABELED)."""
    normalized = {}
    for key, value in (labels or {}).items():
        normalized.setdefault(_norm_key(key), str(value).strip())
    out = {}
    for column, (_, aliases) in LABEL_FIELDS.items():
        value = next((normalized[a] for a in aliases
                      if a in normalized and normalized[a].lower() not in _EMPTY_LABEL_VALUES), NOT_LABELED)
        out[column] = value
    return out


def labels_text(labels):
    """All raw labels as 'key=value; key=value' (handy for searching and for the CSV)."""
    return "; ".join(f"{k}={v}" for k, v in sorted((labels or {}).items()))


@dataclass
class Params:
    target_cpu: float = 60.0      # % of CPU we want the p95 peak to land at after resizing
    target_mem: float = 70.0      # % of RAM we want the p95 peak to land at after resizing
    cpu_high: float = 85.0        # p95 CPU at or above this = under-provisioned
    mem_high: float = 90.0        # p95 RAM at or above this = under-provisioned
    min_saving_pct: float = 10.0  # ignore resizes that save less than this
    min_hours: int = 48           # need at least this many hourly points
    idle_cpu_avg: float = 5.0
    idle_cpu_p95: float = 15.0
    idle_mem_p95: float = 30.0


def weight(vcpus, mem_gb):
    return CPU_WEIGHT * vcpus + mem_gb


def describe_machine_type(name, catalog):
    """Return dict(series, vcpus, mem_gb, custom, shared) or None when the type is not understood."""
    name = (name or "").split("/")[-1]
    m = CUSTOM_RE.match(name)
    if m:
        return dict(series=m.group("series") or "n1", vcpus=int(m.group("cpu")),
                    mem_gb=int(m.group("mem")) / 1024.0, custom=True, shared=False)
    if name in catalog:
        cpus, mem_gb = catalog[name]
        series = name.split("-")[0]
        return dict(series=series, vcpus=cpus, mem_gb=mem_gb, custom=False, shared=name in SHARED_CORE)
    return None


def _predefined_candidates(series, catalog):
    out = []
    for name, (cpus, mem_gb) in catalog.items():
        m = PREDEFINED_RE.match(name)
        if m and m.group("series") == series:
            out.append(dict(name=name, cpus=cpus, mem=mem_gb))
    return out


def _custom_candidate(series, cpu_min, mem_min_gb):
    """Smallest legal custom shape for the series that covers the requirement."""
    lo, hi = (0.9, 6.5) if series == "n1" else (0.5, 8.0)  # GB per vCPU limits
    step = 1 if series == "n1" else 2
    cpu = max(step, math.ceil(cpu_min))
    if step == 2 and cpu % 2:
        cpu += 1
    mem_mb = 256
    for _ in range(64):
        mem_mb = max(math.ceil(mem_min_gb * 1024 / 256) * 256, math.ceil(cpu * lo * 1024 / 256) * 256)
        if mem_mb <= cpu * hi * 1024:
            break
        cpu += step
    name = f"custom-{cpu}-{mem_mb}" if series == "n1" else f"{series}-custom-{cpu}-{mem_mb}"
    return dict(name=name, cpus=cpu, mem=mem_mb / 1024.0)


def _pick(candidates, req_cpu, req_mem):
    ok = [c for c in candidates if c["cpus"] >= req_cpu - 1e-9 and c["mem"] >= req_mem - 1e-9]
    return min(ok, key=lambda c: (weight(c["cpus"], c["mem"]), c["cpus"])) if ok else None


def recommend(vm, catalog, p):
    """Recommend a machine type for one VM. `vm` needs machine_type, gpus, points, cpu_avg/p95, mem_avg/p95."""
    out = dict(status="Not evaluated", recommended_type=None, rec_vcpus=np.nan, rec_mem_gb=np.nan,
               price_ratio=np.nan, needed_vcpus=np.nan, needed_mem_gb=np.nan, confidence="", reason="",
               vcpus=np.nan, mem_gb=np.nan, series="")
    info = describe_machine_type(vm["machine_type"], catalog)
    if info is None:
        out["reason"] = "Machine type not recognised"
        return out
    out.update(vcpus=info["vcpus"], mem_gb=info["mem_gb"], series=info["series"])
    if info["shared"]:
        out["reason"] = "Shared-core type; CPU % is not comparable"
        return out
    if (vm.get("gpus") or 0) > 0 or info["series"] in GPU_SERIES:
        out["reason"] = "GPU VM; not evaluated"
        return out
    if pd.isna(vm["cpu_p95"]) or vm["points"] < p.min_hours:
        out["status"] = "Insufficient data"
        out["reason"] = f"Only {int(vm['points'])} hourly points (need {p.min_hours})"
        return out

    vcpus, mem_gb = info["vcpus"], info["mem_gb"]
    ram_known = pd.notna(vm["mem_p95"])
    cpu_p95 = float(vm["cpu_p95"])
    mem_p95 = float(vm["mem_p95"]) if ram_known else np.nan
    w_cur = weight(vcpus, mem_gb)

    needed_cpu = vcpus * cpu_p95 / p.target_cpu
    needed_mem = mem_gb * mem_p95 / p.target_mem if ram_known else mem_gb  # RAM unknown -> keep RAM
    out.update(needed_vcpus=needed_cpu, needed_mem_gb=needed_mem)

    days = vm["points"] / 24.0
    out["confidence"] = ("High" if ram_known else "Medium (RAM unknown)") if days >= 7 else "Low (< 7 days of data)"

    cands = _predefined_candidates(info["series"], catalog)
    cpu_pressure = cpu_p95 >= p.cpu_high
    mem_pressure = ram_known and mem_p95 >= p.mem_high

    def with_custom(req_cpu, req_mem):
        extra = [_custom_candidate(info["series"], req_cpu, req_mem)] if info["custom"] else []
        return _pick(cands + extra, req_cpu, req_mem)

    ram_txt = f", RAM p95 {mem_p95:.0f}%" if ram_known else ", RAM not reported"
    base_reason = f"CPU p95 {cpu_p95:.0f}%{ram_txt}"

    if cpu_pressure or mem_pressure:
        req_cpu = max(needed_cpu, vcpus + 1) if cpu_pressure else needed_cpu
        req_mem = max(needed_mem, mem_gb + 0.001) if mem_pressure else needed_mem
        best = with_custom(req_cpu, req_mem)
        out["status"] = "Under-provisioned"
        which = "CPU" if cpu_pressure else "RAM"
        if best is None:
            out["recommended_type"] = "Larger type needed (manual review)"
            out["reason"] = f"{base_reason}; {which} is saturated"
            return out
    else:
        best = with_custom(needed_cpu, needed_mem)
        if best is None or weight(best["cpus"], best["mem"]) > w_cur * (1 - p.min_saving_pct / 100.0):
            out.update(status="Right-sized", recommended_type=vm["machine_type"].split("/")[-1],
                       rec_vcpus=vcpus, rec_mem_gb=mem_gb, price_ratio=1.0,
                       reason=f"{base_reason}; current size fits")
            return out
        idle = (vm["cpu_avg"] < p.idle_cpu_avg and cpu_p95 < p.idle_cpu_p95
                and (not ram_known or mem_p95 < p.idle_mem_p95))
        out["status"] = "Idle" if idle else "Over-provisioned"

    out.update(recommended_type=best["name"], rec_vcpus=best["cpus"], rec_mem_gb=best["mem"],
               price_ratio=weight(best["cpus"], best["mem"]) / w_cur)
    out["reason"] = base_reason + ("; very low usage" if out["status"] == "Idle" else "")
    return out


def build_recommendations(inventory, util, costs, catalogs, window_start, window_end, params):
    """One row per RUNNING VM with utilization stats, recommendation, cost and estimated saving.

    inventory: project, instance_id, instance_name, zone, machine_type, status, created, gpus
    util:      project, instance_id, time, cpu_pct, cpu_max_pct, mem_pct, mem_max_pct (hourly, 0-100)
    costs:     instance_id, net_cost  (billed compute cost over the window)
    catalogs:  {zone: {machine_type_name: (vcpus, mem_gb)}}
    """
    running = inventory[inventory["status"] == "RUNNING"].rename(columns={"status": "vm_state"}).copy()
    if running.empty:
        return pd.DataFrame()
    g = util.groupby(["project", "instance_id"])
    stats = pd.DataFrame({
        "points": g["cpu_pct"].count(),
        "cpu_avg": g["cpu_pct"].mean(), "cpu_p95": g["cpu_max_pct"].quantile(0.95), "cpu_peak": g["cpu_max_pct"].max(),
        "mem_avg": g["mem_pct"].mean(), "mem_p95": g["mem_max_pct"].quantile(0.95), "mem_peak": g["mem_max_pct"].max(),
    }).reset_index()
    df = running.merge(stats, on=["project", "instance_id"], how="left")
    df["points"] = df["points"].fillna(0)

    recs = [recommend(row, catalogs.get(row["zone"], {}), params) for row in df.to_dict("records")]
    df = pd.concat([df.reset_index(drop=True), pd.DataFrame(recs)], axis=1)

    # Cost: billed compute cost over the days the VM actually existed in the window, scaled to 30 days.
    window_days = max((window_end - window_start).total_seconds() / 86400.0, 1.0)
    start_ts = pd.Timestamp(window_start)
    created = pd.to_datetime(df["created"], utc=True, errors="coerce")
    existed = (pd.Timestamp(window_end) - created.where(created > start_ts, start_ts)).dt.total_seconds() / 86400.0
    df["eff_days"] = existed.clip(lower=1.0, upper=window_days).fillna(window_days)
    if costs is not None and not costs.empty:
        df = df.merge(costs[["instance_id", "net_cost"]].drop_duplicates("instance_id"), on="instance_id", how="left")
    else:
        df["net_cost"] = np.nan
    df["monthly_cost"] = df["net_cost"] / df["eff_days"] * 30.0
    df["monthly_saving"] = np.where(df["status"].isin(["Idle", "Over-provisioned", "Under-provisioned"]),
                                    df["monthly_cost"] * (1 - df["price_ratio"]), 0.0)
    df.loc[df["monthly_cost"].isna() & df["status"].isin(["Idle", "Over-provisioned", "Under-provisioned"]), "monthly_saving"] = np.nan
    return df
