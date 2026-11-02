"""Leadership dashboard helpers: headline numbers, clickable charts and drill-down tables.

Pure pandas / plotly: no Streamlit and no Google API calls, so every function can be tested on its own.

Every clickable chart function returns ``(figure, lookup)``. ``lookup[curve][point]`` is the key of that
bar (a status, a client, a VM id ...). When the user clicks a bar, Streamlit reports the clicked
``curve_number`` and ``point_index``; ``resolve_click`` turns that back into the key, which decides
what the details pop-up shows.
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from rightsizing import LABEL_FIELDS, NOT_LABELED, STATUS_ORDER
from vm_charts import (CURRENT_COLOR, GRID, INK, MUTED, RECOMMENDED_COLOR, STATUS_COLORS, TEXT, finish, prepare_table, style_table)

DOWNSIZE = ["Idle", "Over-provisioned"]
EVALUATED = ["Idle", "Over-provisioned", "Right-sized", "Under-provisioned"]
GROUP_FIELDS = ["client", "owner", "environment", "department", "application"]   # fields leaders can slice by
COVERAGE_FIELDS = ["owner", "client", "environment", "department", "application", "dev_owner", "support_owner", "in_use_by"]
SAVING_LOW, SAVING_HIGH = "#FDE68A", "#D97706"
UNLABELED_COLOR = "#94A3B8"


# --------------------------------------------------------------------------- headline numbers
def summary(recs):
    """Numbers for the KPI cards. Savings count Idle + Over-provisioned only; upsizing cost is separate."""
    evaluated = recs[recs["status"].isin(EVALUATED)]
    down = recs[recs["status"].isin(DOWNSIZE)]
    up = recs[recs["status"] == "Under-provisioned"]
    savings = float(down["monthly_saving"].sum(skipna=True))
    base = float(evaluated["monthly_cost"].sum(skipna=True))
    n_eval = len(evaluated)
    return dict(
        vms=len(recs), evaluated=n_eval, down=len(down), idle=int((recs["status"] == "Idle").sum()),
        over=int((recs["status"] == "Over-provisioned").sum()), up=len(up),
        right=int((recs["status"] == "Right-sized").sum()),
        savings=savings, yearly=savings * 12, base_cost=base, savings_pct=(savings / base if base > 0 else 0.0),
        extra=float(-up["monthly_saving"].sum(skipna=True)),
        vcpu_now=float(evaluated["vcpus"].sum()), vcpu_rec=float(evaluated["rec_vcpus"].sum()),
        right_pct=(int((recs["status"] == "Right-sized").sum()) / n_eval if n_eval else 0.0),
        owner_pct=float((recs["owner"] != NOT_LABELED).mean()) if len(recs) else 0.0,
        no_owner=int((recs["owner"] == NOT_LABELED).sum()),
    )


# --------------------------------------------------------------------------- clickable charts
def _unique_labels(names, ids):
    """Bar labels must be unique or Plotly merges the bars. Add the id tail only to duplicated names."""
    names = pd.Series(list(names)).astype(str)
    dup = names.duplicated(keep=False)
    return [f"{n} ({str(i)[-4:]})" if d else n for n, i, d in zip(names, ids, dup)]


def health_bars(recs, fmt_money):
    """How many VMs sit in each right-sizing status. Click a bar to list those VMs."""
    g = (recs.groupby("status").agg(vms=("instance_id", "count"), cost=("monthly_cost", "sum"), saving=("monthly_saving", "sum"))
         .reindex(STATUS_ORDER).dropna(subset=["vms"]).reset_index())
    total = g["vms"].sum()
    fig = go.Figure(go.Bar(
        x=g["vms"], y=g["status"], orientation="h", marker=dict(color=[STATUS_COLORS[s] for s in g["status"]], line=dict(width=0)),
        text=[f"<b>{int(n)}</b> VMs · {n / total:.0%}" for n in g["vms"]], textposition="outside", cliponaxis=False,
        textfont=dict(size=12, color=TEXT),
        customdata=np.column_stack([g["cost"].map(lambda v: fmt_money(v)).values, g["saving"].map(lambda v: fmt_money(v)).values]),
        hovertemplate=("<b>%{y}</b><br>%{x} VMs<br>Monthly compute cost: %{customdata[0]}<br>Est. saving / (extra cost): %{customdata[1]}"
                       "<br><i>Click to see these VMs</i><extra></extra>")))
    fig.update_xaxes(range=[0, g["vms"].max() * 1.45], showticklabels=False, showgrid=False)
    fig.update_yaxes(title="", autorange="reversed", automargin=True)
    fig.update_layout(bargap=0.34)
    finish(fig, "Fleet health", "Running VMs by right-sizing status · click a bar for the VM list.", 360, legend=False)
    return fig, [g["status"].tolist()]


def savings_by_group(recs, dim, fmt_money, top=10):
    """Estimated monthly saving per client / owner / environment... Click a bar for that group's VMs."""
    d = recs[recs["status"].isin(DOWNSIZE)]
    g = d.groupby(dim).agg(saving=("monthly_saving", "sum"), vms=("instance_id", "count"), cost=("monthly_cost", "sum")).reset_index()
    g = g[g["saving"] > 0].nlargest(top, "saving").sort_values("saving")
    if g.empty:
        return None, []
    top_saving = g["saving"].max()
    colors = [UNLABELED_COLOR if v == NOT_LABELED else _blend(SAVING_LOW, SAVING_HIGH, s / top_saving) for v, s in zip(g[dim], g["saving"])]
    fig = go.Figure(go.Bar(
        x=g["saving"], y=g[dim], orientation="h", marker=dict(color=colors, line=dict(width=0)),
        text=[f"<b>{fmt_money(s)}</b> · {int(n)} VM{'s' if n != 1 else ''}" for s, n in zip(g["saving"], g["vms"])],
        textposition="outside", cliponaxis=False, textfont=dict(size=11, color=TEXT),
        customdata=g["cost"].map(lambda v: fmt_money(v)).values,
        hovertemplate="<b>%{y}</b><br>Est. monthly saving: <b>%{text}</b><br>Current compute cost: %{customdata}<br><i>Click for details</i><extra></extra>"))
    fig.update_xaxes(range=[0, top_saving * 1.75], showticklabels=False, showgrid=False)
    fig.update_yaxes(title="", automargin=True)
    fig.update_layout(bargap=0.3)
    title = LABEL_FIELDS[dim][0]
    finish(fig, f"Savings by {title.lower()}", "Estimated monthly saving from downsizing · grey = no label · click a bar.", 400, legend=False)
    return fig, [g[dim].tolist()]


def top_vm_savings(recs, fmt_money, n=10, clickable=True):
    """The VMs with the biggest saving. Click a bar to open that VM's utilization story."""
    d = recs[recs["status"].isin(DOWNSIZE)].dropna(subset=["monthly_saving"]).nlargest(n, "monthly_saving").sort_values("monthly_saving")
    if d.empty:
        return None, []
    labels = _unique_labels(d["instance_name"], d["instance_id"])
    move = d["machine_type"].astype(str) + " → " + d["recommended_type"].astype(str)
    fig = go.Figure(go.Bar(
        x=d["monthly_saving"], y=labels, orientation="h", marker=dict(color=[STATUS_COLORS[s] for s in d["status"]], line=dict(width=0)),
        text=[f"<b>{fmt_money(v)}</b>  {m}" for v, m in zip(d["monthly_saving"], move)], textposition="outside", cliponaxis=False,
        textfont=dict(size=11, color=TEXT),
        customdata=np.column_stack([d["status"].values, d["owner"].astype(str).values, d["client"].astype(str).values]),
        hovertemplate=("<b>%{y}</b> · %{customdata[0]}<br>Saving: <b>%{x:,.0f}</b><br>Owner: %{customdata[1]} · Client: %{customdata[2]}"
                       + ("<br><i>Click to open this VM</i>" if clickable else "") + "<extra></extra>")))
    fig.update_xaxes(range=[0, d["monthly_saving"].max() * 2.1], showticklabels=False, showgrid=False)
    fig.update_yaxes(title="", automargin=True)
    fig.update_layout(bargap=0.3)
    finish(fig, f"Top {len(d)} VMs to resize", "Largest estimated monthly savings · purple = idle, amber = over-provisioned.", 440, legend=False)
    return fig, [d["instance_id"].tolist()]


def label_coverage(recs):
    """Share of VMs that carry each label. Click a bar to list the VMs missing it."""
    rows = [(col, LABEL_FIELDS[col][0], float((recs[col] != NOT_LABELED).mean()), int((recs[col] == NOT_LABELED).sum())) for col in COVERAGE_FIELDS]
    rows.sort(key=lambda r: r[2])
    colors = [("#10B981" if pct >= .9 else "#F59E0B" if pct >= .6 else "#EF4444") for _, _, pct, _ in rows]
    fig = go.Figure(go.Bar(
        x=[r[2] * 100 for r in rows], y=[r[1] for r in rows], orientation="h", marker=dict(color=colors, line=dict(width=0)),
        text=[f"<b>{r[2]:.0%}</b> · {r[3]} missing" for r in rows], textposition="outside", cliponaxis=False, textfont=dict(size=11, color=TEXT),
        hovertemplate="<b>%{y}</b><br>%{x:.0f}% of VMs labeled<br><i>Click to list VMs without it</i><extra></extra>"))
    fig.update_xaxes(range=[0, 135], showticklabels=False, showgrid=False)
    fig.update_yaxes(title="", automargin=True)
    fig.update_layout(bargap=0.34)
    finish(fig, "Who can we ask? Label coverage", "Share of VMs with each ownership label · red < 60%, amber < 90% · click a bar.", 400, legend=False)
    return fig, [[r[0] for r in rows]]


def _blend(c0, c1, t):
    """Linear blend between two hex colours (t in 0..1)."""
    t = float(min(max(t, 0.0), 1.0))
    a, b = [int(c0.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)], [int(c1.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


# --------------------------------------------------------------------------- click handling
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


# --------------------------------------------------------------------------- details
def select_rows(recs, kind, value):
    """Rows behind a click. kind: 'status', 'vm', 'unlabeled' or a label column such as 'client'."""
    if kind == "status":
        return recs[recs["status"] == value]
    if kind == "vm":
        return recs[recs["instance_id"] == value]
    if kind == "unlabeled":
        return recs[recs[value] == NOT_LABELED]
    return recs[recs[kind] == value]


def describe(kind, value, recs):
    """(title, subtitle) for the details pop-up."""
    if kind == "status":
        return f"{value} VMs", "Every running VM currently in this right-sizing status."
    if kind == "unlabeled":
        return f"VMs without a '{LABEL_FIELDS[value][0]}' label", "Add the label in Google Cloud so costs and recommendations can be routed to the right team."
    if kind == "vm":
        row = recs[recs["instance_id"] == value]
        return (str(row["instance_name"].iloc[0]) if len(row) else "VM"), "Utilization, size and recommendation for this VM."
    return f"{LABEL_FIELDS[kind][0]}: {value}", f"All running VMs where {LABEL_FIELDS[kind][0].lower()} is '{value}'."


def detail_table(df):
    """Styled, human-friendly table for the details pop-up. Returns (styler, column_config_labels)."""
    t = prepare_table(df)
    cols = ["instance_name", "status_label", "owner", "client", "environment", "change", "action", "cpu_p95", "mem_p95",
            "monthly_cost", "monthly_saving", "confidence"]
    t = t[cols].sort_values("monthly_saving", ascending=False, na_position="last").reset_index(drop=True)
    labels = {"instance_name": "VM", "status_label": "Status", "owner": "Owner", "client": "Client", "environment": "Environment",
              "change": "Current → recommended", "action": "Action", "cpu_p95": "CPU p95 %", "mem_p95": "RAM p95 %",
              "monthly_cost": "Monthly cost", "monthly_saving": "Est. monthly saving", "confidence": "Confidence"}
    return style_table(t), labels
