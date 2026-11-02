"""Charts, KPI cards and table styling for the CPU & RAM right-sizing view.

Pure plotting / pandas helpers: no Streamlit and no Google API calls, so every function can be
tested (or rendered to an image) on its own.

Colour system
-------------
* One semantic colour per right-sizing status (used by every chart and by the table):
    Idle = violet, Over-provisioned = amber, Right-sized = emerald, Under-provisioned = red,
    Insufficient data = slate, Not evaluated = light slate.
* CPU is always blue and RAM is always cyan.
* "Current / provisioned" is always grey-blue and "Recommended" is always emerald.
"""
import html

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from rightsizing import STATUS_ORDER

# --------------------------------------------------------------------------- palette
FONT = "Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif"
INK, TEXT, MUTED, GRID, CARD = "#0F172A", "#334155", "#64748B", "#EEF2F7", "#FFFFFF"
CPU_COLOR, RAM_COLOR = "#3B82F6", "#06B6D4"
CURRENT_COLOR, RECOMMENDED_COLOR, USED_COLOR = "#94A3B8", "#10B981", "#F59E0B"

STATUS_COLORS = {
    "Idle": "#8B5CF6", "Over-provisioned": "#F59E0B", "Right-sized": "#10B981",
    "Under-provisioned": "#EF4444", "Insufficient data": "#94A3B8", "Not evaluated": "#CBD5E1",
}
STATUS_TINT = {  # soft backgrounds for table cells
    "Idle": "#EDE9FE", "Over-provisioned": "#FEF3C7", "Right-sized": "#D1FAE5",
    "Under-provisioned": "#FEE2E2", "Insufficient data": "#F1F5F9", "Not evaluated": "#F8FAFC",
}
STATUS_INK = {
    "Idle": "#5B21B6", "Over-provisioned": "#92400E", "Right-sized": "#065F46",
    "Under-provisioned": "#991B1B", "Insufficient data": "#475569", "Not evaluated": "#64748B",
}
STATUS_EMOJI = {
    "Idle": "🟣", "Over-provisioned": "🟠", "Right-sized": "🟢",
    "Under-provisioned": "🔴", "Insufficient data": "⚪", "Not evaluated": "⚪",
}
HEAT_SCALE = [[0.0, "#34D399"], [0.5, "#FBBF24"], [1.0, "#EF4444"]]  # low -> high load


def _rgba(hex_color, alpha):
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


# --------------------------------------------------------------------------- shared look
def finish(fig, title, subtitle="", height=380, legend=True):
    """Apply the common look: left-aligned title, one-line explainer, light grid, clean hover."""
    top = 92 if subtitle else 62
    fig.update_layout(
        title=dict(text=f"<b>{title}</b>", x=0.012, xanchor="left", y=0.965, yanchor="top",
                   font=dict(size=17, color=INK, family=FONT)),
        height=height, margin=dict(l=16, r=22, t=top, b=16),
        paper_bgcolor=CARD, plot_bgcolor=CARD,
        font=dict(family=FONT, color=TEXT, size=12),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1.0, bgcolor="rgba(0,0,0,0)",
                    font=dict(size=12)),
        hoverlabel=dict(bgcolor="white", bordercolor="#CBD5E1", font=dict(family=FONT, size=12, color=INK)),
    )
    if subtitle:
        fig.add_annotation(text=subtitle, xref="paper", yref="paper", x=0.0, y=1.0, yshift=34, xanchor="left",
                           yanchor="bottom", showarrow=False, font=dict(size=12, color=MUTED, family=FONT))
    fig.update_xaxes(showgrid=False, zeroline=False, linecolor="#E2E8F0", tickcolor="#E2E8F0")
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor="rgba(0,0,0,0)")
    return fig


# --------------------------------------------------------------------------- KPI cards
def kpi_card(label, value, sub="", accent="#3B82F6", tooltip=""):
    """HTML for one KPI card (styled by the .kpi CSS in the app)."""
    return (f'<div class="kpi" style="--accent:{accent}" title="{html.escape(tooltip, quote=True)}">'
            f'<div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{html.escape(str(value))}</div>'
            f'<div class="kpi-sub">{html.escape(sub) if sub else "&nbsp;"}</div></div>')


KPI_CSS = """
<style>
.kpi{background:#fff;border:1px solid #e6eaf2;border-left:6px solid var(--accent);border-radius:14px;padding:14px 16px 12px;
     box-shadow:0 2px 10px rgba(15,23,42,.05);min-height:104px}
.kpi-label{color:#64748B;font-size:.78rem;font-weight:650;letter-spacing:.02em;text-transform:uppercase}
.kpi-value{color:#0F172A;font-size:1.65rem;font-weight:780;line-height:1.25;margin-top:2px;letter-spacing:-.02em}
.kpi-sub{color:#64748B;font-size:.8rem;margin-top:2px}
[data-testid="stPlotlyChart"]{background:#fff;border:1px solid #e6eaf2;border-radius:16px;padding:6px 8px;
     box-shadow:0 2px 10px rgba(15,23,42,.045)}
.stTabs [data-baseweb="tab-list"]{gap:6px;border-bottom:1px solid #e2e8f0}
.stTabs [data-baseweb="tab"]{font-weight:650;font-size:1rem;padding:10px 16px;border-radius:10px 10px 0 0}
.stTabs [aria-selected="true"]{color:#1D4ED8}
.legend-row{display:flex;flex-wrap:wrap;gap:10px;margin:2px 0 12px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:999px;font-size:.8rem;font-weight:650}
.pill i{width:9px;height:9px;border-radius:50%;display:inline-block}
.sec-title{font-size:1.2rem;font-weight:780;color:#0F172A;margin:20px 0 4px;letter-spacing:-.01em}
.sec-sub{color:#64748B;font-size:.88rem;margin:0 0 12px}
</style>
"""


def status_legend_html():
    """Row of coloured pills explaining every status (shown above the table)."""
    meaning = {
        "Idle": "almost unused", "Over-provisioned": "bigger than needed", "Right-sized": "fits the workload",
        "Under-provisioned": "running hot", "Insufficient data": "too little history", "Not evaluated": "GPU / shared-core / unknown",
    }
    pills = "".join(
        f'<span class="pill" style="background:{STATUS_TINT[s]};color:{STATUS_INK[s]}"><i style="background:{STATUS_COLORS[s]}"></i>'
        f'{s} <span style="font-weight:500;opacity:.85">· {meaning[s]}</span></span>' for s in STATUS_ORDER)
    return f'<div class="legend-row">{pills}</div>'


# --------------------------------------------------------------------------- Recommendations tab
def cpu_ram_map(df, target_cpu, target_mem, fmt_money, cpu_high=85, mem_high=90):
    """Bubble map of CPU vs RAM demand with coloured zones. Returns None when no VM reports RAM."""
    d = df.dropna(subset=["cpu_p95", "mem_p95"]).copy()
    if d.empty:
        return None
    cost = d["monthly_cost"].astype(float)
    fill = cost.median() if cost.notna().any() else 1.0
    d["bubble"] = cost.fillna(fill).clip(lower=1.0)
    sizeref = 2.0 * d["bubble"].max() / (36.0 ** 2)
    fig = go.Figure()
    # zones: emerald base, amber oversized corner, red hot strips
    fig.add_shape(type="rect", x0=0, x1=105, y0=0, y1=105, fillcolor=_rgba("#10B981", .07), line_width=0, layer="below")
    fig.add_shape(type="rect", x0=0, x1=target_cpu, y0=0, y1=target_mem, fillcolor=_rgba("#F59E0B", .13), line_width=0, layer="below")
    fig.add_shape(type="rect", x0=cpu_high, x1=105, y0=0, y1=105, fillcolor=_rgba("#EF4444", .10), line_width=0, layer="below")
    fig.add_shape(type="rect", x0=0, x1=105, y0=mem_high, y1=105, fillcolor=_rgba("#EF4444", .10), line_width=0, layer="below")
    fig.add_shape(type="line", x0=target_cpu, x1=target_cpu, y0=0, y1=105, line=dict(color="#94A3B8", width=1, dash="dot"))
    fig.add_shape(type="line", x0=0, x1=105, y0=target_mem, y1=target_mem, line=dict(color="#94A3B8", width=1, dash="dot"))
    for text, x, y, color, xa, ya in (
        ("OVERSIZED · room to shrink", 1.5, target_mem - 1.5, "#B45309", "left", "top"),
        ("RIGHT-SIZED", target_cpu + 1.5, 2.0, "#047857", "left", "bottom"),
        ("RUNNING HOT", 104, 2.0, "#B91C1C", "right", "bottom"),
    ):
        fig.add_annotation(x=x, y=y, text=f"<b>{text}</b>", showarrow=False, xanchor=xa, yanchor=ya, font=dict(size=10.5, color=color))
    cols = ["instance_name", "owner", "client", "machine_type", "recommended_type"]
    for status in STATUS_ORDER:
        sub = d[d["status"] == status]
        if sub.empty:
            continue
        custom = np.column_stack([sub[c].fillna("—").astype(str).values for c in cols] + [sub["monthly_cost"].map(
            lambda v: fmt_money(v) if pd.notna(v) else "n/a").values])
        fig.add_trace(go.Scatter(
            x=sub["cpu_p95"], y=sub["mem_p95"], mode="markers", name=status, customdata=custom,
            marker=dict(size=sub["bubble"], sizemode="area", sizeref=sizeref, sizemin=8, color=STATUS_COLORS[status],
                        opacity=0.9, line=dict(width=1.8, color="white")),
            hovertemplate=("<b>%{customdata[0]}</b><br>%{customdata[3]} → %{customdata[4]}<br>"
                           "CPU p95 <b>%{x:.0f}%</b> · RAM p95 <b>%{y:.0f}%</b><br>"
                           "Owner: %{customdata[1]} · Client: %{customdata[2]}<br>Monthly cost: %{customdata[5]}"
                           "<extra>%{fullData.name}</extra>")))
    fig.update_xaxes(range=[0, 105], title="CPU demand · p95 of hourly peaks", ticksuffix="%", showgrid=False)
    fig.update_yaxes(range=[0, 105], title="RAM demand · p95 of hourly peaks", ticksuffix="%")
    return finish(fig, "Which VMs are oversized?", "Each bubble is a VM. Bubble size = monthly cost. Lower-left = oversized on both CPU and RAM.", 470)


def status_donut(df, fmt_money):
    """Share of VMs per status with the VM count in the centre."""
    g = (df.groupby("status").agg(vms=("instance_id", "count"), cost=("monthly_cost", "sum"))
         .reindex(STATUS_ORDER).dropna(subset=["vms"]).reset_index())
    fig = go.Figure(go.Pie(
        labels=g["status"], values=g["vms"], hole=0.66, sort=False, direction="clockwise",
        marker=dict(colors=[STATUS_COLORS[s] for s in g["status"]], line=dict(color="white", width=3)),
        textinfo="value", textfont=dict(size=13, color="white"), insidetextorientation="horizontal",
        customdata=g["cost"].map(lambda v: fmt_money(v)).values,
        hovertemplate="<b>%{label}</b><br>%{value} VMs (%{percent})<br>Monthly compute cost: %{customdata}<extra></extra>"))
    fig.add_annotation(text=f"<b>{int(g['vms'].sum())}</b>", x=0.5, y=0.54, xref="paper", yref="paper", showarrow=False,
                       font=dict(size=34, color=INK))
    fig.add_annotation(text="running VMs", x=0.5, y=0.43, xref="paper", yref="paper", showarrow=False, font=dict(size=12, color=MUTED))
    finish(fig, "Fleet health", "How the running VMs split by right-sizing status.", 470)
    fig.update_layout(legend=dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.0))
    return fig


def top_savings_bar(df, fmt_money):
    """Top 15 downsize savings, one bar per VM, coloured by status, labelled with the proposed change."""
    top = df.dropna(subset=["monthly_saving"]).nlargest(15, "monthly_saving").sort_values("monthly_saving")
    if top.empty:
        return None
    fig = go.Figure()
    xmax = top["monthly_saving"].max()
    for status in ("Idle", "Over-provisioned"):
        sub = top[top["status"] == status]
        if sub.empty:
            continue
        move = sub["machine_type"] + " → " + sub["recommended_type"].astype(str)
        fig.add_trace(go.Bar(
            x=sub["monthly_saving"], y=sub["instance_name"], orientation="h", name=status,
            marker=dict(color=STATUS_COLORS[status], line=dict(width=0)),
            text=[f"<b>{fmt_money(v)}</b>  {m}" for v, m in zip(sub["monthly_saving"], move)], textposition="outside",
            cliponaxis=False, textfont=dict(size=11, color=TEXT),
            customdata=np.column_stack([move.values, sub["monthly_cost"].map(lambda v: fmt_money(v) if pd.notna(v) else "n/a").values,
                                        sub["owner"].astype(str).values]),
            hovertemplate=("<b>%{y}</b><br>%{customdata[0]}<br>Saving: <b>%{text}</b><br>Current monthly cost: %{customdata[1]}<br>"
                           "Owner: %{customdata[2]}<extra>" + status + "</extra>")))
    fig.update_xaxes(range=[0, xmax * 1.9], showticklabels=False, showgrid=False)
    fig.update_yaxes(title="", automargin=True)
    fig.update_layout(barmode="relative", bargap=0.32)
    return finish(fig, "Where the biggest savings are", "Top 15 VMs by estimated monthly saving, with the proposed size change.", 520)


def capacity_compare(evaluated):
    """Current vs recommended vCPUs and RAM by machine series."""
    cap = evaluated.groupby("series", as_index=False).agg(
        vcpu_now=("vcpus", "sum"), vcpu_rec=("rec_vcpus", "sum"), ram_now=("mem_gb", "sum"), ram_rec=("rec_mem_gb", "sum"))
    if cap.empty:
        return None
    v0, v1, r0, r1 = cap["vcpu_now"].sum(), cap["vcpu_rec"].sum(), cap["ram_now"].sum(), cap["ram_rec"].sum()
    pct = lambda a, b: f"{(b - a) / a:+.0%}" if a else "n/a"
    fig = make_subplots(rows=1, cols=2, subplot_titles=("vCPUs", "RAM (GB)"), horizontal_spacing=0.1)
    for col, (now, rec) in enumerate((("vcpu_now", "vcpu_rec"), ("ram_now", "ram_rec")), start=1):
        fig.add_bar(x=cap["series"], y=cap[now], name="Current", marker_color=CURRENT_COLOR, legendgroup="now", showlegend=col == 1,
                    text=cap[now].map("{:,.0f}".format), textposition="outside", cliponaxis=False, row=1, col=col)
        fig.add_bar(x=cap["series"], y=cap[rec], name="Recommended", marker_color=RECOMMENDED_COLOR, legendgroup="rec", showlegend=col == 1,
                    text=cap[rec].map("{:,.0f}".format), textposition="outside", cliponaxis=False, row=1, col=col)
    fig.update_layout(barmode="group", bargap=0.28, bargroupgap=0.06)
    fig.update_annotations(font=dict(size=13, color=MUTED))
    finish(fig, "Capacity: current vs recommended",
           f"vCPUs {v0:,.0f} → {v1:,.0f} ({pct(v0, v1)}) · RAM {r0:,.0f} → {r1:,.0f} GB ({pct(r0, r1)})", 520)
    fig.update_yaxes(range=[0, None])
    return fig


def savings_by_group(grouped, dim, dim_title, fmt_money):
    """Estimated saving per owner/client/... as horizontal bars shaded by size."""
    g = grouped.nlargest(15, "saving").sort_values("saving")
    if g.empty or g["saving"].max() <= 0:
        return None
    labels = [f"<b>{fmt_money(s)}</b> · {int(n)} VM{'s' if n != 1 else ''}" for s, n in zip(g["saving"], g["VMs"])]
    fig = go.Figure(go.Bar(
        x=g["saving"], y=g[dim], orientation="h", text=labels, textposition="outside", cliponaxis=False,
        marker=dict(color=g["saving"], colorscale=[[0, "#FDE68A"], [1, "#D97706"]], line=dict(width=0)),
        customdata=np.column_stack([g["VMs"].values, g["extra"].map(lambda v: fmt_money(v)).values, g["cost"].map(lambda v: fmt_money(v)).values]),
        hovertemplate=("<b>%{y}</b><br>Estimated saving: <b>%{text}</b><br>Extra cost to fix under-provisioned: %{customdata[1]}<br>"
                       "Current compute cost: %{customdata[2]}<extra></extra>")))
    fig.update_xaxes(range=[0, g["saving"].max() * 1.6], showticklabels=False)
    fig.update_yaxes(title="", automargin=True)
    fig.update_layout(bargap=0.3)
    return finish(fig, f"Estimated monthly savings by {dim_title.lower()}", "Downsizing Idle and Over-provisioned VMs.", 440, legend=False)


def status_mix(mix, dim, dim_title):
    """Stacked bars: how many VMs of each status per owner/client/..."""
    order = mix.groupby(dim)["VMs"].sum().sort_values().index.tolist()
    fig = go.Figure()
    for status in STATUS_ORDER:
        sub = mix[mix["status"] == status]
        if sub.empty:
            continue
        fig.add_trace(go.Bar(x=sub["VMs"], y=sub[dim], orientation="h", name=status, marker=dict(color=STATUS_COLORS[status], line=dict(color="white", width=1)),
                             hovertemplate="<b>%{y}</b><br>" + status + ": %{x} VMs<extra></extra>"))
    fig.update_layout(barmode="stack", bargap=0.3)
    fig.update_yaxes(categoryorder="array", categoryarray=order, title="", automargin=True)
    fig.update_xaxes(title="VMs")
    return finish(fig, f"VM status mix by {dim_title.lower()}", "Spot who owns the idle, oversized and hot VMs.", 440)


# --------------------------------------------------------------------------- Fleet tab
def fleet_trend(trend, hot_cpu=85):
    """Average CPU/RAM across the fleet over time. `trend` has columns time, cpu_pct, mem_pct."""
    fig = go.Figure()
    fig.add_hrect(y0=hot_cpu, y1=100, fillcolor=_rgba("#EF4444", .07), line_width=0,
                  annotation_text="hot zone", annotation_position="top left", annotation_font=dict(size=10, color="#B91C1C"))
    fig.add_trace(go.Scatter(x=trend["time"], y=trend["cpu_pct"], name="CPU", mode="lines", line=dict(color=CPU_COLOR, width=2.4),
                             fill="tozeroy", fillcolor=_rgba(CPU_COLOR, .10), hovertemplate="CPU %{y:.1f}%<extra></extra>"))
    if trend["mem_pct"].notna().any():
        fig.add_trace(go.Scatter(x=trend["time"], y=trend["mem_pct"], name="RAM (VMs with Ops Agent)", mode="lines",
                                 line=dict(color=RAM_COLOR, width=2.4), hovertemplate="RAM %{y:.1f}%<extra></extra>"))
    fig.update_yaxes(range=[0, 100], ticksuffix="%")
    fig.update_layout(hovermode="x unified")
    return finish(fig, "Fleet utilization over time", "Average across all running VMs, hour by hour.", 400)


def cpu_histogram(df, target_cpu, cpu_high=85):
    """How many VMs sit in each CPU band, stacked by status."""
    d = df.dropna(subset=["cpu_p95"])
    fig = go.Figure()
    for status in STATUS_ORDER:
        sub = d[d["status"] == status]
        if sub.empty:
            continue
        fig.add_trace(go.Histogram(x=sub["cpu_p95"], name=status, xbins=dict(start=0, end=100, size=5),
                                   marker=dict(color=STATUS_COLORS[status], line=dict(color="white", width=1)),
                                   hovertemplate="CPU p95 %{x}%<br>%{y} VMs<extra>" + status + "</extra>"))
    fig.add_vline(x=target_cpu, line_dash="dot", line_color="#94A3B8", annotation_text="target", annotation_font=dict(size=10, color=MUTED))
    fig.add_vline(x=cpu_high, line_dash="dot", line_color="#EF4444", annotation_text="hot", annotation_font=dict(size=10, color="#B91C1C"))
    fig.update_layout(barmode="stack", bargap=0.04)
    fig.update_xaxes(title="CPU p95 of hourly peaks", range=[0, 100], ticksuffix="%")
    fig.update_yaxes(title="VMs")
    return finish(fig, "How busy are the VMs?", "Most VMs on the far left are candidates for a smaller size.", 400)


def top_heat_bar(df, col, avg_col, title, subtitle, n=15):
    """Busiest VMs: bar = p95 of hourly peaks (green to red by load), diamond = average."""
    d = df.dropna(subset=[col]).nlargest(n, col).sort_values(col)
    if d.empty:
        return None
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=d[col], y=d["instance_name"], orientation="h", name="p95 of hourly peaks",
        marker=dict(color=d[col], colorscale=HEAT_SCALE, cmin=0, cmax=100, line=dict(width=0)),
        text=d[col].map("{:.0f}%".format), textposition="outside", cliponaxis=False, textfont=dict(size=11, color=TEXT),
        customdata=np.column_stack([d["machine_type"].astype(str).values, d[avg_col].fillna(np.nan).values]),
        hovertemplate="<b>%{y}</b><br>%{customdata[0]}<br>p95 peak: <b>%{x:.0f}%</b><br>Average: %{customdata[1]:.1f}%<extra></extra>"))
    fig.add_trace(go.Scatter(x=d[avg_col], y=d["instance_name"], mode="markers", name="Average",
                             marker=dict(symbol="diamond", size=9, color="white", line=dict(color=INK, width=1.6)), hoverinfo="skip"))
    fig.update_xaxes(range=[0, 118], ticksuffix="%", showgrid=True, gridcolor=GRID)
    fig.update_yaxes(title="", automargin=True, showgrid=False)
    fig.update_layout(bargap=0.3)
    return finish(fig, title, subtitle, 500)


def series_bar(by_series):
    """Average CPU and RAM per machine series (grouped bars)."""
    fig = go.Figure()
    for name, col, color in (("CPU", "CPU", CPU_COLOR), ("RAM", "RAM", RAM_COLOR)):
        sub = by_series.dropna(subset=[col])
        if sub.empty:
            continue
        fig.add_trace(go.Bar(x=sub["series"], y=sub[col], name=name, marker_color=color, text=sub[col].map("{:.0f}%".format),
                             textposition="outside", cliponaxis=False,
                             customdata=sub["VMs"].values, hovertemplate="<b>%{x}</b><br>" + name + " average: %{y:.1f}%<br>%{customdata} VMs<extra></extra>"))
    fig.update_layout(barmode="group", bargap=0.3)
    fig.update_yaxes(range=[0, 100], ticksuffix="%")
    fig.update_xaxes(title="Machine series")
    return finish(fig, "Average utilization by machine series", "Which VM families are busiest and which are mostly idle.", 400)


# --------------------------------------------------------------------------- VM drill-down
def gauge(value, title, target, high, color):
    """Gauge of a p95 peak against the target and 'hot' limits. Returns None when value is missing."""
    if value is None or pd.isna(value):
        return None
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=float(value), number=dict(suffix="%", font=dict(size=36, color=INK, family=FONT), valueformat=".0f"),
        title=dict(text=title, font=dict(size=14, color=MUTED, family=FONT)),
        gauge=dict(axis=dict(range=[0, 100], tickwidth=1, tickcolor="#CBD5E1", ticksuffix="%"), bar=dict(color=color, thickness=0.3),
                   bgcolor="white", borderwidth=0,
                   steps=[dict(range=[0, target], color="#E0F2FE"), dict(range=[target, high], color="#D1FAE5"), dict(range=[high, 100], color="#FEE2E2")],
                   threshold=dict(line=dict(color=INK, width=3), thickness=0.85, value=target))))
    fig.update_layout(height=270, margin=dict(l=24, r=24, t=60, b=10), paper_bgcolor=CARD, font=dict(family=FONT))
    return fig


def vm_capacity(vm):
    """Provisioned vs used (at p95 peak) vs recommended, for vCPUs and RAM."""
    cats = ["Provisioned", "Used at p95 peak", "Recommended"]
    colors = [CURRENT_COLOR, USED_COLOR, RECOMMENDED_COLOR]
    used_cpu = vm["vcpus"] * vm["cpu_p95"] / 100 if pd.notna(vm["cpu_p95"]) else None
    used_mem = vm["mem_gb"] * vm["mem_p95"] / 100 if pd.notna(vm["mem_p95"]) else None
    fig = make_subplots(rows=1, cols=2, subplot_titles=("vCPUs", "RAM (GB)"), horizontal_spacing=0.12)
    for col, vals, fmt in ((1, [vm["vcpus"], used_cpu, vm["rec_vcpus"]], "{:.1f}"), (2, [vm["mem_gb"], used_mem, vm["rec_mem_gb"]], "{:.1f}")):
        fig.add_bar(x=cats, y=vals, marker_color=colors, showlegend=False,
                    text=[fmt.format(v) if v is not None and pd.notna(v) else "n/a" for v in vals], textposition="outside", cliponaxis=False,
                    row=1, col=col)
    fig.update_layout(bargap=0.35)
    fig.update_annotations(font=dict(size=13, color=MUTED))
    finish(fig, "Capacity: provisioned vs used vs recommended", "Grey = what the VM has, amber = what it really needs at peak, green = proposed size.", 320, legend=False)
    fig.update_yaxes(range=[0, None])
    return fig


def vm_timeseries(vm_util, mean_col, max_col, title, color, p95, target, high):
    """Hourly average (line) and hourly peak (shaded band above it), with target and p95 markers."""
    fig = go.Figure()
    fig.add_hrect(y0=high, y1=105, fillcolor=_rgba("#EF4444", .07), line_width=0)
    fig.add_trace(go.Scatter(x=vm_util["time"], y=vm_util[mean_col], name="Hourly average", mode="lines", line=dict(color=color, width=2.2),
                             hovertemplate="avg %{y:.1f}%<extra></extra>"))
    fig.add_trace(go.Scatter(x=vm_util["time"], y=vm_util[max_col], name="Hourly peak", mode="lines", line=dict(color=_rgba(color, .45), width=1),
                             fill="tonexty", fillcolor=_rgba(color, .16), hovertemplate="peak %{y:.1f}%<extra></extra>"))
    fig.add_hline(y=target, line_dash="dash", line_color="#94A3B8", annotation_text=f"target {target:.0f}%",
                  annotation_position="top left", annotation_font=dict(size=10, color=MUTED))
    if p95 is not None and pd.notna(p95):
        fig.add_hline(y=p95, line_dash="dot", line_color=USED_COLOR, line_width=2, annotation_text=f"p95 {p95:.0f}%",
                      annotation_position="bottom right", annotation_font=dict(size=10, color="#B45309"))
    fig.update_yaxes(range=[0, 105], ticksuffix="%")
    fig.update_layout(hovermode="x unified")
    return finish(fig, f"{title} over time", "Line = hourly average. Shaded band reaches up to the hourly peak.", 360)


# --------------------------------------------------------------------------- recommendations table
ACTIONS = {
    "Idle": "⬇ Downsize", "Over-provisioned": "⬇ Downsize", "Under-provisioned": "⬆ Upsize",
    "Right-sized": "✔ Keep as is", "Insufficient data": "⏳ Wait for data", "Not evaluated": "— Skip",
}


def prepare_table(df):
    """Add the friendly columns used by the table: coloured status, action and 'current → recommended'."""
    t = df.copy()
    t["status_label"] = t["status"].map(lambda s: f"{STATUS_EMOJI.get(s, '⚪')} {s}")
    t["action"] = t["status"].map(ACTIONS)
    manual = t["recommended_type"].astype(str).str.startswith("Larger type")
    t.loc[(t["status"] == "Under-provisioned") & manual, "action"] = "🔎 Manual review"
    resize = t["status"].isin(["Idle", "Over-provisioned", "Under-provisioned"]) & t["recommended_type"].notna() & ~manual
    t["change"] = np.where(resize, t["machine_type"].astype(str) + "  →  " + t["recommended_type"].astype(str), t["machine_type"].astype(str))
    return t


def style_table(t):
    """Pandas Styler: tinted status cells and green/red savings."""
    def status_style(v):
        for status in STATUS_ORDER:
            if str(v).endswith(status):
                return f"background-color:{STATUS_TINT[status]};color:{STATUS_INK[status]};font-weight:650"
        return ""

    def saving_style(v):
        if pd.isna(v) or v == 0:
            return "color:#94A3B8"
        return "color:#047857;font-weight:700" if v > 0 else "color:#B91C1C;font-weight:700"

    def action_style(v):
        if str(v).startswith("⬇"):
            return "color:#B45309;font-weight:650"
        if str(v).startswith("⬆"):
            return "color:#B91C1C;font-weight:650"
        if str(v).startswith("✔"):
            return "color:#047857;font-weight:650"
        return "color:#64748B"

    return (t.style.map(status_style, subset=["status_label"]).map(saving_style, subset=["monthly_saving"])
            .map(action_style, subset=["action"]))
