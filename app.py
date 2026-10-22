"""GCP FinOps dashboard: cached summary view and on-demand resource details."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone
import base64
from contextlib import contextmanager
import html
import json
import os
import pathlib
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery, monitoring_v3
from google.oauth2 import service_account

from rightsizing import (LABEL_COLUMNS, LABEL_FIELDS, NOT_LABELED, Params, STATUS_ORDER,
                         build_recommendations, extract_labels, labels_text)

st.set_page_config(
    page_title="GCP FinOps | Cost Intelligence",
    page_icon="💠",
    layout="wide",
    initial_sidebar_state="expanded",
)
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "iks-finops")
TABLE_ID = os.getenv("GCP_BILLING_TABLE", "iks-finops.GCP_Finops_billing.gcp_billing_export_resource_v1_019EFB_4492BC_7B3DE6")
CACHE_TTL = int(os.getenv("FINOPS_CACHE_TTL", "300"))
# CPU/RAM (Cloud Monitoring) uses a separate service account key and a fixed list of projects.
MONITORING_KEY_FILE = os.getenv("GCP_MONITORING_KEY_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "iksgcp.json"))
MONITORING_PROJECTS = [p.strip() for p in os.getenv("GCP_MONITORING_PROJECTS", "iksgcp").split(",") if p.strip()]
# Optional curated table that maps resource (global_name) -> feature. Used when a resource has no "feature" label. Set to "" to disable.
FEATURE_MAP_TABLE = os.getenv("GCP_FEATURE_MAP_TABLE", "iks-finops.Monthly_Reporting.Detailed_2025_Billing")
# Optional CSV (project_id,owner,team) used to assign an owner to spend that has no "owner" label. See owner_map.example.csv.
OWNER_MAP_FILE = os.getenv("FINOPS_OWNER_MAP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "owner_map.csv"))

# ─── Logo (base64-encode once) ────────────────────────────────────────────────
_LOGO_PATH = pathlib.Path(__file__).parent / "iks_logo.png"
if _LOGO_PATH.exists():
    _LOGO_B64 = base64.b64encode(_LOGO_PATH.read_bytes()).decode()
    _LOGO_SRC = f"data:image/png;base64,{_LOGO_B64}"
else:
    _LOGO_SRC = ""

# ─── Design tokens ───────────────────────────────────────────────────────────
INK = "#0F1B3D"
MUTED = "#64748B"
PRIMARY = "#2563EB"
BORDER = "#E6ECF5"
# Donut / ranking palette (matches the reference SaaS theme)
DONUT_PALETTE = ["#3B82F6", "#93C5FD", "#8B5CF6", "#34D399", "#FBBF24", "#F97316"]
OTHERS_COLOR = "#CBD5E1"
UNLABELED_COLOR = "#94A3B8"
CHART_PALETTE = ["#2563EB", "#A9C8F7", "#8B5CF6", "#34D399", "#FBBF24", "#F97316"]
# Uniform size of the two donut cards (Cost by Service / Credits by Service):
# 1.0 = original, 0.8 = a little smaller, 0.7 = compact. Scales chart, text, legend and card padding together.
DONUT_K = 0.8

# ─── Inline SVG icons for KPI cards ──────────────────────────────────────────
def _icon(inner, color):
    return (f'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">{inner}</svg>')


SVG_NET_COST = _icon('<path d="M20 12V8H6a2 2 0 0 1 0-4h12v4"/><path d="M4 6v12a2 2 0 0 0 2 2h14v-4"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/>', "#2563EB")
SVG_GROSS_COST = _icon('<polyline points="3 17 9 11 13 15 21 7"/><polyline points="15 7 21 7 21 13"/>', "#7C3AED")
SVG_CREDITS = _icon('<rect x="3" y="8" width="18" height="13" rx="2"/><path d="M12 8v13"/><path d="M3 12h18"/><path d="M12 8c-2.5 0-4-1-4-2.5S9.5 3 11 3c1.5 0 1 2.5 1 5Zm0 0c2.5 0 4-1 4-2.5S14.5 3 13 3c-1.5 0-1 2.5-1 5Z"/>', "#16A34A")
SVG_ROWS = _icon('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>', "#3B82F6")
SVG_SERVICES = _icon('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>', "#F59E0B")

NAV_ITEMS = ["Overview", "CPU & RAM", "Cost breakdowns", "Resource explorer","Data & export"]

# ─── CSS — Global design system ──────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');


/* ── Hide Streamlit chrome ── */
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"] {display: none !important; visibility: hidden;}
[data-testid="stHeader"] {background: transparent !important;}

/* ── Global type + canvas ── */
.stApp, .stApp :not([data-testid="stIconMaterial"]):not(code):not(pre) {
    font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
}
.stApp {
    background:
        radial-gradient(1200px 500px at 85% -10%, rgba(59,130,246,.08), transparent 60%),
        linear-gradient(180deg, #F6F9FE 0%, #EFF4FB 100%);
    color: #0F1B3D;
}
.block-container {
    padding: 1.6rem 2rem 2.5rem 2rem;
    max-width: 1640px;
}
h1, h2, h3, h4 {color: #0F1B3D; letter-spacing: -0.01em;}

/* ── Sidebar (app navigation) ── */
section[data-testid="stSidebar"] {
    background: #FFFFFF;
    border-right: 1px solid #E6ECF5;
}
section[data-testid="stSidebar"][aria-expanded="true"] {
    min-width: 252px !important;
    width: 252px !important;
    max-width: 252px !important;
}
[data-testid="stSidebarHeader"] {height: 1.2rem; padding: 0;}
[data-testid="stSidebarUserContent"] {padding: 0 1rem 1rem 1rem;}
.side-brand {
    display: flex;
    justify-content: center;   /* centers the logo horizontally */
    padding: 6px 8px 22px 8px;
}
.side-brand-logo {height: 44px; overflow: hidden;}
.side-brand-logo img {width: 96px; height: auto; display: block;}
.side-brand-sep {width: 1px; height: 22px; background: #E2E8F0;}
.side-brand-name {font-size: 15px; font-weight: 600; color: #0F1B3D; letter-spacing: -0.01em;}
.side-section {
    font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
    color: #94A3B8; padding: 0 12px 8px 12px;
}

/* Radio group → nav list */
section[data-testid="stSidebar"] div[role="radiogroup"] {gap: 4px; flex-direction: column;}
section[data-testid="stSidebar"] div[role="radiogroup"] > label {
    display: flex; align-items: center; gap: 12px;
    width: 100%; margin: 0; padding: 11px 14px;
    border-radius: 12px; cursor: pointer;
    transition: background .15s ease, color .15s ease;
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-of-type {display: none !important;}
section[data-testid="stSidebar"] div[role="radiogroup"] > label p {
    font-size: 14px; font-weight: 500; color: #475569; margin: 0;
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label::before {
    content: ""; width: 18px; height: 18px; flex: 0 0 18px;
    background-color: #64748B;
    -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat;
    -webkit-mask-position: center; mask-position: center;
    -webkit-mask-size: contain; mask-size: contain;
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {background: #F3F6FB;}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {background: #EAF1FE;}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) p {color: #2563EB; font-weight: 600;}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked)::before {background-color: #2563EB;}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:nth-of-type(1)::before {
    -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='3' width='7' height='7' rx='1.5'/><rect x='14' y='3' width='7' height='7' rx='1.5'/><rect x='3' y='14' width='7' height='7' rx='1.5'/><rect x='14' y='14' width='7' height='7' rx='1.5'/></svg>");
    mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='3' width='7' height='7' rx='1.5'/><rect x='14' y='3' width='7' height='7' rx='1.5'/><rect x='3' y='14' width='7' height='7' rx='1.5'/><rect x='14' y='14' width='7' height='7' rx='1.5'/></svg>");
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:nth-of-type(2)::before {
    -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='7'/><line x1='21' y1='21' x2='16.5' y2='16.5'/></svg>");
    mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='11' cy='11' r='7'/><line x1='21' y1='21' x2='16.5' y2='16.5'/></svg>");
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:nth-of-type(3)::before {
    -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M21.21 15.89A10 10 0 1 1 8 2.83'/><path d='M22 12A10 10 0 0 0 12 2v10z'/></svg>");
    mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M21.21 15.89A10 10 0 1 1 8 2.83'/><path d='M22 12A10 10 0 0 0 12 2v10z'/></svg>");
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:nth-of-type(4)::before {
    -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><rect x='5' y='5' width='14' height='14' rx='2'/><rect x='9' y='9' width='6' height='6'/><path d='M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3'/></svg>");
    mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><rect x='5' y='5' width='14' height='14' rx='2'/><rect x='9' y='9' width='6' height='6'/><path d='M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3'/></svg>");
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:nth-of-type(5)::before {
    -webkit-mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'/><polyline points='7 10 12 15 17 10'/><line x1='12' y1='15' x2='12' y2='3'/></svg>");
    mask-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'/><polyline points='7 10 12 15 17 10'/><line x1='12' y1='15' x2='12' y2='3'/></svg>");
}

/* Powered-by card pinned to the bottom of the sidebar */
.side-powered {
    position: fixed; bottom: 18px; left: 16px; width: 220px;
    padding: 14px 16px 36px 16px; border-radius: 16px; overflow: hidden;
    background: linear-gradient(180deg, #F3F7FF 0%, #E6EEFD 100%);
    border: 1px solid #E1EAFB;
}
.side-powered .sp-row {display: flex; align-items: center; gap: 10px;}
.side-powered .sp-small {font-size: 11px; color: #64748B; line-height: 1.2;}
.side-powered .sp-big {font-size: 14px; font-weight: 600; color: #0F1B3D; line-height: 1.2;}
.side-powered .sp-wave {position: absolute; left: 0; right: 0; bottom: 0; height: 34px;}

/* ── Page header ── */
.page-title {font-size: 26px; font-weight: 700; color: #0F1B3D; line-height: 1.15; letter-spacing: -0.02em;}
.page-sub {font-size: 10.5px; color: #64748B; margin-top: 4px; font-weight: 500;}
.status-row {
    display: flex; justify-content: flex-end; align-items: center; gap: 12px;
    margin: 2px 2px 14px 2px; font-size: 12px; color: #64748B;
}
.status-dot {width: 7px; height: 7px; border-radius: 50%; background: #22C55E; display: inline-block; margin-right: 6px;
    box-shadow: 0 0 0 3px rgba(34,197,94,.15);}
.period-pill {
    background: #EAF1FE; color: #1D4ED8; font-size: 12px; font-weight: 600;
    padding: 5px 12px; border-radius: 999px;
}

/* ── Cards (bordered containers with key "card_*") ── */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [class*="st-key-card_"]) {
    background: #FFFFFF;
    border: 1px solid #E6ECF5 !important;
    border-radius: 18px !important;
    box-shadow: 0 1px 2px rgba(15,27,61,.04), 0 10px 28px rgba(37,99,235,.05);
    padding: 20px 22px 16px 22px !important;
}
.card-head {display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 6px;}
.card-title {font-size: 15px; font-weight: 600; color: #0F1B3D; letter-spacing: -0.005em;}
.card-desc {font-size: 12.5px; color: #64748B; margin-top: 3px;}
.card-badge {
    font-size: 12px; font-weight: 500; color: #475569; background: #FFFFFF;
    border: 1px solid #E3E9F4; border-radius: 10px; padding: 5px 10px; white-space: nowrap;
}
.legend-inline {display: flex; gap: 14px; font-size: 12px; color: #64748B; align-items: center; white-space: nowrap;}
.legend-inline i {display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px;}

/* ── KPI cards ── */
/* Every size below is "base px × --k", so the whole card (text, icon, svg, padding, height)
   scales uniformly. Change --k only: 1 = original size, 0.7 = compact, 0.6 = very compact. */
.kpi-card {
    --k: 0.7;
    display: flex; flex-direction: column; gap: calc(10px * var(--k));
    background: #FFFFFF; border: 1px solid #E6ECF5; border-radius: calc(18px * var(--k));
    box-shadow: 0 1px 2px rgba(15,27,61,.04), 0 8px 20px rgba(37,99,235,.05);
    padding: calc(18px * var(--k)) calc(18px * var(--k)) calc(14px * var(--k)) calc(18px * var(--k));
    min-height: calc(132px * var(--k)); overflow: hidden;
}
.kpi-top {display: flex; gap: calc(14px * var(--k)); align-items: center;}
.kpi-foot {display: flex; align-items: flex-end; justify-content: space-between; gap: calc(8px * var(--k)); margin-top: auto;}
.kpi-icon {
    flex: 0 0 calc(44px * var(--k)); width: calc(44px * var(--k)); height: calc(44px * var(--k)); border-radius: calc(12px * var(--k));
    display: flex; align-items: center; justify-content: center;
}
.kpi-icon svg {width: calc(20px * var(--k)); height: calc(20px * var(--k));}
.kpi-body {min-width: 0; flex: 1;}
.kpi-label {font-size: calc(12.5px * var(--k)); font-weight: 500; color: #475569; margin-bottom: calc(4px * var(--k)); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.kpi-value {font-size: calc(24px * var(--k)); font-weight: 700; color: #0F1B3D; line-height: 1.15; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; white-space: nowrap;}
.kpi-sub {font-size: calc(12px * var(--k)); color: #64748B; font-weight: 500; min-width: 0; line-height: 1.3;}
.kpi-spark {flex: 0 0 auto; display: block; width: calc(84px * var(--k)); height: auto;}

/* ── Ranked list rows (project / region / SKU) ── */
.rank-list {display: flex; flex-direction: column; gap: 4px; margin-top: 10px;}
.rank-row {
    display: grid; grid-template-columns: minmax(90px, 1.1fr) 1.7fr auto 52px; gap: 14px;
    align-items: center; padding: 7px 0; font-size: 13px;
}
.rank-name {color: #334155; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.rank-bar {height: 10px; background: #EEF3FB; border-radius: 999px; overflow: hidden;}
.rank-bar span {display: block; height: 100%; border-radius: 999px;}
.rank-val {color: #0F1B3D; font-weight: 600; text-align: right; font-variant-numeric: tabular-nums;}
.rank-pct {color: #64748B; text-align: right; font-variant-numeric: tabular-nums;}

/* ── Donut legend ── */
/* --dk (donut scale) is set per donut card below; 1 = original size */
.dl-list {display: flex; flex-direction: column; gap: calc(11px * var(--dk, 1)); padding-top: calc(14px * var(--dk, 1));}
.dl-row {display: flex; align-items: center; gap: calc(10px * var(--dk, 1)); font-size: calc(13px * var(--dk, 1));}
.dl-dot {width: calc(10px * var(--dk, 1)); height: calc(10px * var(--dk, 1)); border-radius: 50%; flex: 0 0 calc(10px * var(--dk, 1));}
.dl-name {flex: 1; color: #334155; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;}
.dl-pct {color: #0F1B3D; font-weight: 600; font-variant-numeric: tabular-nums;}

/* Donut cards ("Cost by Service", "Credits by Service"): everything inside scales uniformly by --dk (see DONUT_K in Python) */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [class*="st-key-card_donut_"]) {
    --dk: __DK__;
    padding: calc(20px * var(--dk)) calc(22px * var(--dk)) calc(16px * var(--dk)) calc(22px * var(--dk)) !important;
    border-radius: calc(18px * var(--dk)) !important;
}
[class*="st-key-card_donut_"] {gap: calc(1rem * var(--dk)) !important;}
[class*="st-key-card_donut_"] [data-testid="stHorizontalBlock"] {gap: calc(1rem * var(--dk)) !important;}
[class*="st-key-card_donut_"] .card-head {gap: calc(12px * var(--dk)); margin-bottom: calc(6px * var(--dk));}
[class*="st-key-card_donut_"] .card-title {font-size: calc(15px * var(--dk));}
[class*="st-key-card_donut_"] .card-desc {font-size: calc(12.5px * var(--dk)); margin-top: calc(3px * var(--dk));}
[class*="st-key-card_donut_"] .card-badge {
    font-size: calc(12px * var(--dk)); padding: calc(5px * var(--dk)) calc(10px * var(--dk)); border-radius: calc(10px * var(--dk));
}

/* ── Inputs / selects / filters ── */
div[data-baseweb="select"] > div, div[data-baseweb="input"], div[data-baseweb="base-input"] {
    border-radius: 12px !important;
}
div[data-baseweb="select"] > div {
    min-height: 42px; background: #FFFFFF !important;
    border: 1px solid #E3E9F4 !important;
    box-shadow: 0 1px 2px rgba(15,27,61,.04);
    font-size: 13px;
}
div[data-baseweb="select"] > div:focus-within, div[data-baseweb="input"]:focus-within {
    border-color: #2563EB !important; box-shadow: 0 0 0 3px rgba(37,99,235,.12) !important;
}
div[data-baseweb="input"], div[data-baseweb="base-input"] {background: #FFFFFF !important;}
div[data-baseweb="input"] {border: 1px solid #E3E9F4 !important;}
.stMultiSelect [data-baseweb="tag"] {background: #EAF1FE !important; border-radius: 8px !important;}
.stMultiSelect [data-baseweb="tag"] span {color: #1D4ED8 !important; font-weight: 500 !important;}
.filter-label {
    font-size: 11px; font-weight: 600; color: #64748B; text-transform: uppercase;
    letter-spacing: .06em; margin-bottom: 4px;
}

/* ── Expander ── */
[data-testid="stExpander"] {
    background: #FFFFFF; border: 1px solid #E6ECF5 !important; border-radius: 14px;
    box-shadow: 0 1px 2px rgba(15,27,61,.03); margin-top: 6px; margin-bottom: 14px;
}
[data-testid="stExpander"] summary {font-size: 13px; font-weight: 600; color: #334155;}
[data-testid="stExpander"] details {border: none !important;}

/* ── Buttons ── */
.stButton > button, .stDownloadButton > button {
    border-radius: 12px !important; font-weight: 600 !important; font-size: 13px !important;
    min-height: 42px; padding: 8px 18px !important; transition: all .15s ease !important;
}
.stButton > button[kind="primary"] {
    background: #2563EB !important; border: 1px solid #2563EB !important; color: #fff !important;
    box-shadow: 0 6px 14px rgba(37,99,235,.25);
}
.stButton > button[kind="primary"]:hover {background: #1D4ED8 !important; border-color: #1D4ED8 !important;}
.stButton > button[kind="secondary"], .stDownloadButton > button {
    background: #FFFFFF !important; border: 1px solid #E3E9F4 !important; color: #0F1B3D !important;
    box-shadow: 0 1px 2px rgba(15,27,61,.04);
}
.stButton > button[kind="secondary"]:hover, .stDownloadButton > button:hover {
    background: #F5F8FE !important; border-color: #BFD3F8 !important; color: #1D4ED8 !important;
}

/* ── Dataframes / alerts ── */
[data-testid="stDataFrame"] {border-radius: 14px; overflow: hidden; border: 1px solid #E6ECF5;}
[data-testid="stAlert"] {border-radius: 14px;}

/* ── st.metric → KPI-style card ── */
/* Every size is "base px x --mk" so the whole card scales uniformly (1 = old size, 0.7 = compact, same as the KPI cards). */
[data-testid="stMetric"] {
    --mk: 0.7;
    background: #FFFFFF; border: 1px solid #E6ECF5; border-radius: calc(18px * var(--mk));
    box-shadow: 0 1px 2px rgba(15,27,61,.04), 0 8px 20px rgba(37,99,235,.05);
    padding: calc(16px * var(--mk)) calc(18px * var(--mk));
    gap: calc(4px * var(--mk));
}
[data-testid="stMetricLabel"] {color: #475569 !important; font-weight: 500 !important;}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {font-size: calc(12.5px * var(--mk)) !important; line-height: 1.3 !important;}
[data-testid="stMetricLabel"] svg {width: calc(16px * var(--mk)) !important; height: calc(16px * var(--mk)) !important;}
[data-testid="stMetricLabel"] *, [data-testid="stMetricDelta"] * {white-space: normal !important; overflow: visible !important; text-overflow: clip !important;}
[data-testid="stMetricValue"] * {white-space: normal !important; overflow: visible !important; text-overflow: clip !important;}
[data-testid="stMetricValue"], [data-testid="stMetricValue"] * {font-size: calc(22px * var(--mk)) !important; line-height: 1.2 !important;}
[data-testid="stMetricValue"] {color: #0F1B3D !important; font-weight: 700 !important; letter-spacing: -0.02em;}
[data-testid="stMetricDelta"], [data-testid="stMetricDelta"] * {font-size: calc(13px * var(--mk)) !important; line-height: 1.3 !important;}
[data-testid="stMetricDelta"] svg {width: calc(16px * var(--mk)) !important; height: calc(16px * var(--mk)) !important;}

/* ── Pill tabs ── */
div[data-testid="stTabs"] [role="tablist"] {
    gap: 4px; background: #EAF0FA; border-radius: 14px; padding: 4px; display: inline-flex; border-bottom: none !important;
}
div[data-testid="stTabs"] [role="tab"] {
    border-radius: 10px; padding: 8px 18px; height: auto; color: #64748B; font-weight: 500;
}
div[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    background: #FFFFFF; color: #2563EB; font-weight: 600; box-shadow: 0 1px 3px rgba(15,27,61,.1);
}
div[data-testid="stTabs"] [data-baseweb="tab-highlight"], div[data-testid="stTabs"] [data-baseweb="tab-border"] {display: none;}

/* ── Key / value items (VM ownership) ── */
.kv-grid {display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px 20px; margin-top: 8px;}
.kv-label {font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: #94A3B8;}
.kv-value {font-size: 14px; font-weight: 600; color: #0F1B3D; margin-top: 3px; word-break: break-word;}
.kv-value.muted-val {color: #94A3B8; font-weight: 500;}

/* ── Full-screen loading overlay (centered) ── */
.load-overlay {
    position: fixed; inset: 0; z-index: 999999; display: flex; align-items: center; justify-content: center;
    background: rgba(240,245,253,.80); backdrop-filter: blur(5px); -webkit-backdrop-filter: blur(5px);
    animation: load-fade .2s ease-out;
}
.load-card {
    display: flex; flex-direction: column; align-items: center; gap: 12px; text-align: center;
    min-width: 320px; max-width: 420px; padding: 28px 40px 26px 40px;
    background: #FFFFFF; border: 1px solid #E6ECF5; border-radius: 22px;
    box-shadow: 0 24px 64px rgba(37,99,235,.18), 0 2px 6px rgba(15,27,61,.05);
}
.load-logo {height: 26px; overflow: hidden;}
.load-logo img {width: 68px; height: auto; display: block;}
.load-ring {
    width: 46px; height: 46px; border-radius: 50%; margin-top: 4px;
    border: 4px solid #E3ECFB; border-top-color: #2563EB; animation: load-spin .85s linear infinite;
}
.load-title {font-size: 15px; font-weight: 600; color: #0F1B3D; letter-spacing: -0.005em;}
.load-sub {font-size: 12.5px; color: #64748B; line-height: 1.4;}
.load-bar {position: relative; width: 190px; height: 4px; margin-top: 4px; background: #E6EEFB; border-radius: 999px; overflow: hidden;}
.load-bar::after {
    content: ""; position: absolute; top: 0; left: -40%; width: 40%; height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, #60A5FA, #2563EB); animation: load-slide 1.2s ease-in-out infinite;
}
@keyframes load-spin {to {transform: rotate(360deg);}}
@keyframes load-slide {0% {left: -40%;} 100% {left: 100%;}}
@keyframes load-fade {from {opacity: 0;} to {opacity: 1;}}

/* ── Footer ── */
.finops-footer {border-top: 1px solid #E2E8F0; padding-top: 16px; margin-top: 22px;}
.finops-footer p {color: #94A3B8; font-size: 12px; margin: 0;}
</style>
""".replace("__DK__", str(DONUT_K)), unsafe_allow_html=True)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def h(markup):
    """Collapse whitespace so Markdown never treats indented HTML as a code block."""
    return " ".join(markup.split())


@contextmanager
def loading_overlay(title, subtitle="Fetching the latest data. This usually takes a few seconds."):
    """Centered full-screen loading card shown while the body runs, removed when it finishes."""
    slot = st.empty()
    logo = ""
    slot.markdown(h(
        f'<div class="load-overlay"><div class="load-card">{logo}<div class="load-ring"></div>'
        f'<div class="load-title">{html.escape(title)}</div><div class="load-sub">{html.escape(subtitle)}</div>'
        f'<div class="load-bar"></div></div></div>'
    ), unsafe_allow_html=True)
    try:
        yield
    finally:
        slot.empty()


def currency_symbol(currency="INR"):
    return {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(str(currency).upper(), f"{currency} ")


def money(value, currency="INR"):
    """Full-precision money: ₹284,612 (no decimals above 1,000)."""
    try:
        value = float(value or 0)
    except (ValueError, TypeError):
        value = 0.0
    symbol = currency_symbol(currency)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 100_000_000:
        return f"{sign}{symbol}{value / 1_000_000:,.1f}M"
    if value >= 1_000:
        return f"{sign}{symbol}{value:,.0f}"
    return f"{sign}{symbol}{value:,.2f}"


@st.cache_resource
def get_credentials():
    """Return credentials for BigQuery and Cloud Monitoring.

    Credential order:
    1. GCP_SERVICE_ACCOUNT_JSON: the service account key JSON as an env var value.
    2. None, which makes the Google clients fall back to Application Default
       Credentials (e.g. a GOOGLE_APPLICATION_CREDENTIALS file path).
    """
    raw = os.getenv("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except (ValueError, KeyError):
        raise RuntimeError("GCP_SERVICE_ACCOUNT_JSON is set but is not a valid service account key JSON.") from None


@st.cache_resource
def get_client():
    return bigquery.Client(project=PROJECT_ID, credentials=get_credentials())


@st.cache_resource
def get_monitoring_credentials():
    """Service account for Cloud Monitoring + Compute Engine reads (separate from the billing account)."""
    if not os.path.isfile(MONITORING_KEY_FILE):
        raise RuntimeError(f"Monitoring service account key not found: {MONITORING_KEY_FILE}. "
                           "Set GCP_MONITORING_KEY_FILE or place iksgcp.json next to app.py.")
    return service_account.Credentials.from_service_account_file(
        MONITORING_KEY_FILE, scopes=["https://www.googleapis.com/auth/cloud-platform"])


@st.cache_resource
def get_monitoring_client():
    return monitoring_v3.MetricServiceClient(credentials=get_monitoring_credentials())


def run_query(sql, params, label):
    config = bigquery.QueryJobConfig(query_parameters=params, labels={"app": "finops-dashboard", "view": label})
    return get_client().query(sql, job_config=config).to_dataframe(create_bqstorage_client=False)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_filter_options(start_day, end_day):
    sql = f"""
    SELECT
      ARRAY_AGG(DISTINCT service.description IGNORE NULLS ORDER BY service.description LIMIT 300) AS services,
      ARRAY_AGG(DISTINCT project.name IGNORE NULLS ORDER BY project.name LIMIT 300) AS projects,
      ARRAY_AGG(DISTINCT location.region IGNORE NULLS ORDER BY location.region LIMIT 300) AS regions,
      ARRAY_AGG(DISTINCT sku.description IGNORE NULLS ORDER BY sku.description LIMIT 500) AS skus,
      ARRAY_AGG(DISTINCT currency IGNORE NULLS ORDER BY currency LIMIT 30) AS currencies
    FROM `{TABLE_ID}`
    WHERE usage_start_time >= TIMESTAMP(@start_day)
      AND usage_start_time < TIMESTAMP(DATE_ADD(@end_day, INTERVAL 1 DAY))
    """
    params = [bigquery.ScalarQueryParameter("start_day", "DATE", start_day),
              bigquery.ScalarQueryParameter("end_day", "DATE", end_day)]
    result = run_query(sql, params, "filters")
    keys = ["services", "projects", "regions", "skus", "currencies"]
    if result.empty:
        return {key: [] for key in keys}
    row = result.iloc[0]
    options = {}
    for key in keys:
        values = row.get(key)
        if values is None:
            values = []
        options[key] = sorted(str(v) for v in values if v is not None)
    return options


def query_params(start_day, end_day, services, projects, regions, skus, currency, resource_search):
    return [
        bigquery.ScalarQueryParameter("start_day", "DATE", start_day),
        bigquery.ScalarQueryParameter("end_day", "DATE", end_day),
        bigquery.ArrayQueryParameter("services", "STRING", services),
        bigquery.ArrayQueryParameter("projects", "STRING", projects),
        bigquery.ArrayQueryParameter("regions", "STRING", regions),
        bigquery.ArrayQueryParameter("skus", "STRING", skus),
        bigquery.ScalarQueryParameter("currency", "STRING", currency),
        bigquery.ScalarQueryParameter("resource_search", "STRING", resource_search.strip()),
    ]


def filter_sql():
    return """
    WHERE usage_start_time >= TIMESTAMP(@start_day)
      AND usage_start_time < TIMESTAMP(DATE_ADD(@end_day, INTERVAL 1 DAY))
      AND (IFNULL(ARRAY_LENGTH(@services),0)=0 OR service.description IN UNNEST(@services))
      AND (IFNULL(ARRAY_LENGTH(@projects),0)=0 OR COALESCE(project.name,project.id,'Unspecified project') IN UNNEST(@projects))
      AND (IFNULL(ARRAY_LENGTH(@regions),0)=0 OR COALESCE(location.region,'Unspecified region') IN UNNEST(@regions))
      AND (IFNULL(ARRAY_LENGTH(@skus),0)=0 OR COALESCE(sku.description,'Unspecified SKU') IN UNNEST(@skus))
      AND (@currency='ALL' OR currency=@currency)
      AND (@resource_search='' OR STRPOS(LOWER(COALESCE(resource.name,resource.global_name,'')),LOWER(@resource_search))>0)
    """


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_summary_data(start_day, end_day, services, projects, regions, skus, currency, resource_search):
    sql = f"""
    SELECT DATE(usage_start_time) AS usage_date,
      IFNULL(currency,'UNKNOWN') AS currency,
      COALESCE(service.description,'Unspecified service') AS service,
      COALESCE(project.name,project.id,'Unspecified project') AS project,
      COALESCE(location.region,'Unspecified region') AS region,
      COALESCE(sku.description,'Unspecified SKU') AS sku,
      SUM(cost) AS gross_cost,
      SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS credits,
      SUM(cost+IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS net_cost,
      SUM(usage.amount) AS usage_amount,
      STRING_AGG(DISTINCT usage.unit,', ' LIMIT 4) AS usage_units,
      MAX(export_time) AS latest_export_time,
      COUNT(*) AS billing_rows
    FROM `{TABLE_ID}`
    {filter_sql()}
    GROUP BY usage_date,currency,service,project,region,sku
    """
    return run_query(sql, query_params(start_day,end_day,services,projects,regions,skus,currency,resource_search), "summary")


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_resource_data(start_day, end_day, services, projects, regions, skus, currency, resource_search):
    sql = f"""
    SELECT IFNULL(currency,'UNKNOWN') AS currency,
      COALESCE(service.description,'Unspecified service') AS service,
      COALESCE(project.name,project.id,'Unspecified project') AS project,
      COALESCE(location.region,'Unspecified region') AS region,
      COALESCE(sku.description,'Unspecified SKU') AS sku,
      COALESCE(NULLIF(resource.name,''),NULLIF(resource.global_name,''),'Resource not reported') AS resource,
      SUM(cost) AS gross_cost,
      SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS credits,
      SUM(cost+IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS net_cost,
      SUM(usage.amount) AS usage_amount,
      STRING_AGG(DISTINCT usage.unit,', ' LIMIT 4) AS usage_units,
      MAX(DATE(usage_start_time)) AS last_usage_date,
      MAX(export_time) AS latest_export_time,
      COUNT(*) AS billing_rows
    FROM `{TABLE_ID}`
    {filter_sql()}
    GROUP BY currency,service,project,region,sku,resource
    ORDER BY net_cost DESC
    LIMIT 1000
    """
    return run_query(sql, query_params(start_day,end_day,services,projects,regions,skus,currency,resource_search), "resources")


def _label_value_sql(label_array, key):
    """SQL scalar subquery: first non-empty value of a label whose key matches (ignoring case/punctuation)."""
    return (f"(SELECT value FROM UNNEST({label_array}) WHERE REGEXP_REPLACE(LOWER(key), r'[^a-z0-9]', '') = '{key}' "
            f"AND TRIM(IFNULL(value, '')) NOT IN ('', 'none', 'na', 'n/a', 'null', '-') LIMIT 1)")


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_owner_feature_cost(start_day, end_day, services, projects, regions, skus, currency, resource_search, use_map=True):
    """Net cost per (owner, feature, project) in ONE scan of the billing table (labels are the expensive columns).

    Owner   = resource label 'owner',   else project label 'owner',   else '(not labeled)'.
    Feature = resource label 'feature', else project label 'feature', else curated mapping table, else '(not labeled)'.
    """
    use_map = bool(use_map and FEATURE_MAP_TABLE)
    cte = f"""
    WITH fmap AS (
      SELECT global_name, ARRAY_AGG(feature ORDER BY start_date DESC LIMIT 1)[OFFSET(0)] AS mapped_feature
      FROM `{FEATURE_MAP_TABLE}`
      WHERE global_name IS NOT NULL AND TRIM(IFNULL(feature, '')) != ''
      GROUP BY global_name
    )""" if use_map else ""
    join = "LEFT JOIN fmap ON fmap.global_name = resource.global_name" if use_map else ""
    mapped = "fmap.mapped_feature," if use_map else ""
    sql = f"""
    {cte}
    SELECT COALESCE({_label_value_sql('labels', 'owner')}, {_label_value_sql('project.labels', 'owner')}, '{NOT_LABELED}') AS owner_name,
      COALESCE({_label_value_sql('labels', 'feature')}, {_label_value_sql('project.labels', 'feature')}, {mapped} '{NOT_LABELED}') AS feature_name,
      IFNULL(project.id, '') AS project_id,
      IFNULL(currency,'UNKNOWN') AS currency,
      SUM(cost+IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS net_cost
    FROM `{TABLE_ID}` {join}
    {filter_sql()}
    GROUP BY 1, 2, 3, 4
    """
    return run_query(sql, query_params(start_day, end_day, services, projects, regions, skus, currency, resource_search), "owner_feature_cost")


@st.cache_data(show_spinner=False)
def _read_owner_map(path, mtime):
    frame = pd.read_csv(path, dtype=str).fillna("")
    cols = {c.strip().lower(): c for c in frame.columns}
    if "project_id" not in cols or "owner" not in cols:
        return {}
    pairs = zip(frame[cols["project_id"]].str.strip(), frame[cols["owner"]].str.strip())
    return {pid: owner for pid, owner in pairs if pid and owner}


def load_owner_map():
    """project_id -> owner from owner_map.csv (empty when the file is missing or invalid)."""
    try:
        return _read_owner_map(OWNER_MAP_FILE, os.path.getmtime(OWNER_MAP_FILE))
    except (OSError, ValueError, pd.errors.ParserError):
        return {}


# ─── CPU & RAM: VM inventory, Cloud Monitoring metrics and billed VM cost ────
CPU_METRIC = "compute.googleapis.com/instance/cpu/utilization"      # fraction 0-1, no agent needed
MEM_METRIC = "agent.googleapis.com/memory/percent_used"              # percent 0-100, needs Ops Agent
MAX_MONITORING_WORKERS = 8
INSTANCE_ID_CHUNK = 40
HOUR = 3600
_ALIGN = monitoring_v3.Aggregation.Aligner
# column -> (metric, aligner, extra filter, scale to percent)
VM_METRICS = {
    "cpu_pct": (CPU_METRIC, _ALIGN.ALIGN_MEAN, "", 100.0),
    "cpu_max_pct": (CPU_METRIC, _ALIGN.ALIGN_MAX, "", 100.0),
    "mem_pct": (MEM_METRIC, _ALIGN.ALIGN_MEAN, 'metric.labels.state="used"', 1.0),
    "mem_max_pct": (MEM_METRIC, _ALIGN.ALIGN_MAX, 'metric.labels.state="used"', 1.0),
}


def _first_line(exc):
    return (str(exc).splitlines() or [repr(exc)])[0][:200]


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_vm_inventory(projects):
    """Current Compute Engine instances (machine type, status, created, labels) for each project."""
    cols = ["project", "instance_id", "instance_name", "zone", "machine_type", "status", "created", "gpus"] + LABEL_COLUMNS + ["labels"]
    rows, errors = [], []
    session = AuthorizedSession(get_monitoring_credentials())
    for project in projects:
        token = None
        try:
            while True:
                params = {"maxResults": 500, "fields": "items/*/instances(id,name,machineType,status,creationTimestamp,guestAccelerators,labels),nextPageToken"}
                if token:
                    params["pageToken"] = token
                resp = session.get(f"https://compute.googleapis.com/compute/v1/projects/{project}/aggregated/instances", params=params, timeout=60)
                resp.raise_for_status()
                body = resp.json()
                for zone_key, scoped in body.get("items", {}).items():
                    for inst in scoped.get("instances", []):
                        gpus = sum(int(a.get("acceleratorCount", 0)) for a in inst.get("guestAccelerators", []))
                        raw_labels = inst.get("labels", {})
                        found = extract_labels(raw_labels)
                        rows.append((project, str(inst["id"]), inst["name"], zone_key.split("/")[-1],
                                     inst["machineType"].split("/")[-1], inst["status"],
                                     pd.to_datetime(inst.get("creationTimestamp"), utc=True, errors="coerce"), gpus,
                                     *[found[c] for c in LABEL_COLUMNS], labels_text(raw_labels)))
                token = body.get("nextPageToken")
                if not token:
                    break
        except Exception as exc:
            errors.append(f"{project} (inventory): {_first_line(exc)}")
    return pd.DataFrame(rows, columns=cols), errors


@st.cache_data(ttl=3600, show_spinner=False)
def load_machine_type_catalog(project, zones):
    """{zone: {machine_type: (vCPUs, RAM GB)}} so we know total CPU/RAM and the candidate sizes."""
    def fetch(zone):
        catalog, token = {}, None
        session = AuthorizedSession(get_monitoring_credentials())
        try:
            while True:
                params = {"maxResults": 500, "fields": "items(name,guestCpus,memoryMb),nextPageToken"}
                if token:
                    params["pageToken"] = token
                resp = session.get(f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/machineTypes", params=params, timeout=60)
                resp.raise_for_status()
                body = resp.json()
                for item in body.get("items", []):
                    catalog[item["name"]] = (item["guestCpus"], item["memoryMb"] / 1024.0)
                token = body.get("nextPageToken")
                if not token:
                    break
            return zone, catalog, None
        except Exception as exc:
            return zone, catalog, f"{zone} (machine types): {_first_line(exc)}"
    with ThreadPoolExecutor(max_workers=MAX_MONITORING_WORKERS) as pool:
        results = list(pool.map(fetch, zones))
    return {z: c for z, c, _ in results}, [e for _, _, e in results if e]


def _metric_task(client, scope_project, instance_ids, column, start_dt, end_dt):
    metric, aligner, extra, scale = VM_METRICS[column]
    ids = ",".join(f'"{i}"' for i in instance_ids)
    flt = f'metric.type="{metric}" AND resource.type="gce_instance" AND resource.labels.instance_id=one_of({ids})'
    if extra:
        flt += f" AND {extra}"
    request = {
        "name": f"projects/{scope_project}", "filter": flt,
        "interval": monitoring_v3.TimeInterval(start_time=start_dt, end_time=end_dt),
        "aggregation": monitoring_v3.Aggregation(alignment_period={"seconds": HOUR}, per_series_aligner=aligner),
    }
    rows = []
    try:
        for series in client.list_time_series(request=request):
            project = series.resource.labels.get("project_id", scope_project)
            instance_id = series.resource.labels.get("instance_id", "")
            for point in series.points:
                rows.append((project, instance_id, pd.Timestamp(point.interval.end_time), point.value.double_value * scale))
    except Exception as exc:
        return column, rows, f"{scope_project} ({column}): {_first_line(exc)}"
    return column, rows, None


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_vm_metrics(projects, start_day, end_day):
    """Hourly mean and hourly peak CPU/RAM % for the RUNNING VMs only (one row per VM per hour)."""
    inventory, errors = load_vm_inventory(projects)
    running = inventory[inventory["status"] == "RUNNING"]
    start_dt = datetime.combine(start_day, dtime.min, tzinfo=timezone.utc)
    end_dt = min(datetime.combine(end_day + timedelta(days=1), dtime.min, tzinfo=timezone.utc), datetime.now(timezone.utc))
    client = get_monitoring_client()
    tasks = []
    for project in projects:
        ids = running.loc[running["project"] == project, "instance_id"].tolist()
        for i in range(0, len(ids), INSTANCE_ID_CHUNK):
            for column in VM_METRICS:
                tasks.append((client, project, ids[i:i + INSTANCE_ID_CHUNK], column, start_dt, end_dt))
    with ThreadPoolExecutor(max_workers=MAX_MONITORING_WORKERS) as pool:
        results = list(pool.map(lambda t: _metric_task(*t), tasks))
    keys = ["project", "instance_id", "time"]
    frames = {column: [] for column in VM_METRICS}
    for column, rows, error in results:
        frames[column].extend(rows)
        if error:
            errors.append(error)
    util = None
    for column in VM_METRICS:
        part = pd.DataFrame(frames[column], columns=keys + [column])
        util = part if util is None else util.merge(part, on=keys, how="outer")
    return util, errors


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_vm_costs(projects, start_day, end_day, currency):
    """Billed compute cost (cores, RAM, OS licences) per VM, joined by the instance ID in the billing export."""
    sql = rf"""
    SELECT REGEXP_EXTRACT(resource.global_name, r'/instances/(\d+)$') AS instance_id,
      IFNULL(currency,'UNKNOWN') AS currency,
      SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS net_cost
    FROM `{TABLE_ID}`
    WHERE usage_start_time >= TIMESTAMP(@start_day)
      AND usage_start_time < TIMESTAMP(DATE_ADD(@end_day, INTERVAL 1 DAY))
      AND service.description = 'Compute Engine'
      AND project.id IN UNNEST(@projects)
      AND REGEXP_CONTAINS(resource.global_name, r'/instances/\d+$')
      AND REGEXP_CONTAINS(sku.description, r'Instance (Core|Ram)|Licensing Fee')
      AND NOT REGEXP_CONTAINS(sku.description, r'(?i)commit')
      AND (@currency='ALL' OR currency=@currency)
    GROUP BY instance_id, currency
    """
    params = [bigquery.ScalarQueryParameter("start_day", "DATE", start_day),
              bigquery.ScalarQueryParameter("end_day", "DATE", end_day),
              bigquery.ArrayQueryParameter("projects", "STRING", list(projects)),
              bigquery.ScalarQueryParameter("currency", "STRING", currency)]
    df = run_query(sql, params, "vm_costs")
    df["net_cost"] = pd.to_numeric(df["net_cost"], errors="coerce").fillna(0)
    return df.sort_values("net_cost", ascending=False).drop_duplicates("instance_id")


# ─── UI building blocks ──────────────────────────────────────────────────────
def style_chart(fig, height=360, legend=False):
    """Apply the unified SaaS chart design to any Plotly figure."""
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=(34 if legend else 8), b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, system-ui, sans-serif", color="#475569", size=12),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0, title_text="",
                    font=dict(size=12, color="#475569"), bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(
            bgcolor="white", bordercolor="#E2E8F0",
            font=dict(family="Inter, system-ui, sans-serif", color=INK, size=13),
        ),
        title=dict(text=""),  # explicit empty string prevents "undefined"
    )
    fig.update_xaxes(showgrid=False, zeroline=False, showline=False, tickfont=dict(color="#64748B", size=11))
    fig.update_yaxes(gridcolor="#EDF1F7", zeroline=False, showline=False, tickfont=dict(color="#64748B", size=11))
    return fig


def card(key):
    return st.container(border=True, key=f"card_{key}")


def card_header(title, desc="", badge="", right_html=""):
    desc_html = f'<div class="card-desc">{html.escape(desc)}</div>' if desc else ""
    badge_html = f'<div class="card-badge">{html.escape(badge)}</div>' if badge else ""
    st.markdown(h(f'<div class="card-head"><div><div class="card-title">{html.escape(title)}</div>{desc_html}</div>'
                  f'{right_html}{badge_html}</div>'), unsafe_allow_html=True)


def sparkline(values, color, name, bars=False, width=84, height=28):
    vals = [float(v) for v in values if pd.notna(v)]
    if len(vals) < 2:
        return ""
    if bars:
        vals = vals[-16:]
        hi = max(max(vals), 1e-9)
        bw = width / len(vals)
        rects = "".join(
            f'<rect x="{i * bw + 0.8:.1f}" y="{height - max(v / hi * (height - 2), 2):.1f}" '
            f'width="{max(bw - 1.8, 1):.1f}" height="{max(v / hi * (height - 2), 2):.1f}" rx="1.5" fill="{color}" opacity="{0.35 + 0.65 * (i + 1) / len(vals):.2f}"/>'
            for i, v in enumerate(vals)
        )
        return f'<svg class="kpi-spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}">{rects}</svg>'
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = width / (len(vals) - 1)
    pts = [(i * step, (height - 4) - (v - lo) / span * (height - 8)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"0,{height} {line} {width},{height}"
    gid = f"sg_{name}"
    return (f'<svg class="kpi-spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{color}" stop-opacity=".28"/>'
            f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>'
            f'<polygon points="{area}" fill="url(#{gid})"/>'
            f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def render_kpi_card(label, value, sub, svg_icon, icon_bg, spark=""):
    st.markdown(h(
        f'<div class="kpi-card"><div class="kpi-top"><div class="kpi-icon" style="background:{icon_bg};">{svg_icon}</div>'
        f'<div class="kpi-body"><div class="kpi-label">{html.escape(label)}</div><div class="kpi-value">{value}</div></div></div>'
        f'<div class="kpi-foot"><div class="kpi-sub">{html.escape(sub)}</div>{spark}</div></div>'
    ), unsafe_allow_html=True)


def top_with_others(frame, label_col, value_col, top_n=6):
    """Positive totals per label, top N plus a combined 'Others' slice."""
    series = frame.groupby(label_col)[value_col].sum()
    series = series[series > 0].sort_values(ascending=False)
    top = series.head(top_n).copy()
    rest = series.iloc[top_n:].sum()
    if rest > 0:
        top["Others"] = rest
    return top


def render_donut(series, center_value, center_label, height=250):
    """Donut chart (left) with a percentage legend (right), as in modern SaaS dashboards."""
    total = float(series.sum())
    colors = [OTHERS_COLOR if name == "Others" else (UNLABELED_COLOR if name == NOT_LABELED else DONUT_PALETTE[i % len(DONUT_PALETTE)]) for i, name in enumerate(series.index)]
    fig = go.Figure(go.Pie(
        labels=list(series.index), values=list(series.values), hole=0.72, sort=False, direction="clockwise",
        marker=dict(colors=colors, line=dict(color="#FFFFFF", width=3 * DONUT_K)),
        textinfo="none", hovertemplate="<b>%{label}</b><br>%{value:,.2f} · %{percent}<extra></extra>",
    ))
    fig.add_annotation(text=f"<b>{center_value}</b>", x=0.5, y=0.54, showarrow=False, font=dict(size=round(19 * DONUT_K, 1), color=INK))
    fig.add_annotation(text=center_label, x=0.5, y=0.43, showarrow=False, font=dict(size=round(12 * DONUT_K, 1), color=MUTED))
    style_chart(fig, round(height * DONUT_K))
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0))
    legend = "".join(
        f'<div class="dl-row"><span class="dl-dot" style="background:{c};"></span>'
        f'<span class="dl-name" title="{html.escape(str(n))}">{html.escape(str(n))}</span>'
        f'<span class="dl-pct">{(v / total * 100 if total else 0):.1f}%</span></div>'
        for (n, v), c in zip(series.items(), colors)
    )
    chart_col, legend_col = st.columns([1.05, 1], vertical_alignment="center")
    with chart_col:
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    with legend_col:
        st.markdown(f'<div class="dl-list">{legend}</div>', unsafe_allow_html=True)


def render_ranked_list(frame, label_col, value_col, cur, top_n, color):
    """Horizontal progress-bar list: name · bar · value · share of total."""
    grouped = frame.groupby(label_col)[value_col].sum().sort_values(ascending=False)
    total = float(grouped.sum())
    top = grouped.head(top_n)
    peak = max(float(top.max()), 1e-9) if len(top) else 1e-9
    rows = "".join(
        f'<div class="rank-row"><div class="rank-name" title="{html.escape(str(name))}">{html.escape(str(name))}</div>'
        f'<div class="rank-bar"><span style="width:{max(float(v), 0) / peak * 100:.1f}%;background:{color};"></span></div>'
        f'<div class="rank-val">{money(v, cur)}</div>'
        f'<div class="rank-pct">{(float(v) / total * 100 if total > 0 else 0):.1f}%</div></div>'
        for name, v in top.items()
    )
    st.markdown(f'<div class="rank-list">{rows}</div>', unsafe_allow_html=True)


# ─── SIDEBAR: brand + navigation ─────────────────────────────────────────────
_logo_html = (f'<div class="side-brand-logo"><img src="{_LOGO_SRC}" alt="IKS" /></div>'
              if _LOGO_SRC else '<div style="font-size:22px;font-weight:800;color:#C2410C;">IKS</div>')

with st.sidebar:
    st.markdown(h(f'<div class="side-brand">{_logo_html}</div>'), unsafe_allow_html=True)
    st.markdown('<div class="side-section">Menu</div>', unsafe_allow_html=True)
    page = st.radio("Navigation", NAV_ITEMS, key="nav", label_visibility="collapsed")
    st.markdown(h("""
        <div class="side-powered">
          <div class="sp-row">
            <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
              <defs><linearGradient id="gc" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stop-color="#4285F4"/><stop offset=".5" stop-color="#34A853"/><stop offset="1" stop-color="#FBBC05"/>
              </linearGradient></defs>
              <path d="M7 19a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 17.8 9.2 4.9 4.9 0 0 1 17.5 19z" fill="url(#gc)" opacity=".92"/>
              <path d="M9 14.5h6" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
            </svg>
            <div><div class="sp-small">Powered by</div><div class="sp-big">DevOps@IKS</div></div>
          </div>
          <svg class="sp-wave" viewBox="0 0 220 34" preserveAspectRatio="none">
            <path d="M0 18 C 40 4, 80 30, 120 16 S 190 6, 220 14 L220 34 L0 34 Z" fill="#BFD6FB" opacity=".55"/>
            <path d="M0 24 C 50 12, 90 34, 140 22 S 200 14, 220 22 L220 34 L0 34 Z" fill="#93BAF7" opacity=".5"/>
          </svg>
        </div>
    """), unsafe_allow_html=True)


# ─── 1. TOP BAR: title + global filters ──────────────────────────────────────
fc0, fc1, fc3, fc4, fc5, fc6 = st.columns([2.2, 1.3, 1.3, 1.3, 1.2, 1.1], vertical_alignment="center")
with fc0:
    st.markdown(
        '<div class="page-title">GCP FinOps</div>'
        '<div class="page-sub">Cost Intelligence · Spend by service, project, region, SKU and resource</div>',
        unsafe_allow_html=True,
    )
with fc1:
    period = st.selectbox(
        "Quick date range",
        ["Last 7 days", "Last 30 days", "Last 60 days", "Last 90 days", "Custom"],
        index=1,
        label_visibility="collapsed",
        help="Select a preset date range or choose Custom to set exact dates.",
    )

today = date.today()
if period == "Custom":
    cd1, _cd2 = st.columns([1.4, 3])
    with cd1:
        chosen = st.date_input(
            "Billing usage dates",
            value=(today - timedelta(days=29), today),
            max_value=today,
            label_visibility="collapsed",
            help="Pick start and end dates for the billing period.",
        )
    if isinstance(chosen, (tuple, list)) and len(chosen) == 2:
        start_day, end_day = chosen
    else:
        start_day = end_day = chosen[0] if isinstance(chosen, (tuple, list)) and chosen else chosen
else:
    days = {"Last 7 days": 7, "Last 30 days": 30, "Last 60 days": 60, "Last 90 days": 90}[period]
    start_day, end_day = today - timedelta(days=days - 1), today

if start_day > end_day:
    st.error("Start date must be before end date.")
    st.stop()

# Load filter options
try:
    with loading_overlay("Loading filters", "Reading services, projects and regions from BigQuery."):
        options = get_filter_options(start_day, end_day)
except Exception as exc:
    st.error("Could not read billing-export filter options from BigQuery.")
    st.code(str(exc))
    st.info("Check credentials, BigQuery Job User permission, and Data Viewer access.")
    st.stop()

with fc3:
    services = st.multiselect(
        "Service", options["services"],
        placeholder="All services",
        label_visibility="collapsed",
        help="Filter by GCP service (e.g. Compute Engine, BigQuery).",
    )
with fc4:
    projects = st.multiselect(
        "Project", options["projects"],
        placeholder="All projects",
        label_visibility="collapsed",
        help="Filter by GCP project name.",
    )
with fc5:
    regions = st.multiselect(
        "Region", options["regions"],
        placeholder="All regions",
        label_visibility="collapsed",
        help="Filter by cloud region (e.g. us-central1).",
    )
with fc6:
    if st.button("Refresh", icon=":material/refresh:", use_container_width=True, help="Clear cached data and reload from BigQuery."):
        get_filter_options.clear()
        load_summary_data.clear()
        load_resource_data.clear()
        load_owner_feature_cost.clear()
        load_vm_inventory.clear()
        load_vm_metrics.clear()
        load_vm_costs.clear()
        load_machine_type_catalog.clear()
        st.session_state.pop("finops_resource_result", None)
        st.session_state.pop("finops_resource_signature", None)
        st.session_state.pop("finops_vm_data", None)
        st.session_state.pop("finops_vm_signature", None)
        st.rerun()

# More filters (inside expander)
with st.expander("More filters", expanded=False):
    mf1, mf2, mf3 = st.columns(3)
    with mf1:
        st.markdown('<div class="filter-label">SKU / Meter</div>', unsafe_allow_html=True)
        skus = st.multiselect(
            "SKU / meter", options["skus"],
            placeholder="All SKUs",
            label_visibility="collapsed",
            help="Filter by specific billing SKU or meter.",
        )
    with mf2:
        st.markdown('<div class="filter-label">Billing currency</div>', unsafe_allow_html=True)
        currency_options = ["ALL"] + options["currencies"]
        currency = st.selectbox(
            "Billing currency", currency_options,
            index=(currency_options.index("INR") if "INR" in currency_options else 0),
            label_visibility="collapsed",
            help="Filter to a single billing currency, or ALL to show every currency (totals won't be converted).",
        )
    with mf3:
        st.markdown('<div class="filter-label">Resource name contains</div>', unsafe_allow_html=True)
        resource_search = st.text_input(
            "Resource name contains",
            placeholder="e.g. instance, bucket, database",
            label_visibility="collapsed",
            help="Text search within resource names (case-insensitive).",
        )


# ─── 2. LOAD DATA ───────────────────────────────────────────────────────────
try:
    with loading_overlay("Loading billing data", "Aggregating your cloud spend from BigQuery."):
        df = load_summary_data(start_day, end_day, services, projects, regions, skus, currency, resource_search)
except Exception as exc:
    st.error("Unable to load the billing summary.")
    st.code(str(exc))
    st.info("Check service-account credentials and BigQuery read permissions.")
    st.stop()

if df.empty:
    st.warning("No billing rows matched these filters and dates. Try a wider date range or clear some filters.")
    st.stop()

for col in ["gross_cost", "credits", "net_cost", "usage_amount", "billing_rows"]:
    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
df["usage_date"] = pd.to_datetime(df["usage_date"], errors="coerce")
latest_export = pd.to_datetime(df["latest_export_time"], errors="coerce", utc=True).max()
selected_currency = df["currency"].dropna().unique().tolist()
if len(selected_currency) > 1:
    st.warning("Multiple currencies are included. Totals are not converted; select one currency for a meaningful total.")
cur_label = selected_currency[0] if len(selected_currency) == 1 else "Mixed currency"
cur_symbol = currency_symbol(cur_label) if len(selected_currency) == 1 else ""
period_label = period if period != "Custom" else "Custom range"

# ─── Status row: period pill + export timestamp ──────────────────────────────
_export_ts_str = latest_export.strftime('%b %d, %Y · %H:%M UTC') if pd.notna(latest_export) else 'N/A'
st.markdown(h(
    f'<div class="status-row"><span class="period-pill">{start_day:%d %b %Y} — {end_day:%d %b %Y}</span>'
    f'<span><span class="status-dot"></span>Last export: {_export_ts_str} (Cached: {CACHE_TTL // 60} min)</span></div>'
), unsafe_allow_html=True)


# ─── 3. KPI ROW ─────────────────────────────────────────────────────────────
gross, credits_total, net = df["gross_cost"].sum(), df["credits"].sum(), df["net_cost"].sum()
row_count, services_count = int(df["billing_rows"].sum()), df["service"].nunique()
projects_count = df["project"].nunique()

daily_all = (df.groupby("usage_date", as_index=False)
               .agg(gross_cost=("gross_cost", "sum"), credits=("credits", "sum"), net_cost=("net_cost", "sum"),
                    billing_rows=("billing_rows", "sum"), services=("service", "nunique"))
               .sort_values("usage_date"))
n_days = max(int(daily_all["usage_date"].nunique()), 1)
credit_share = abs(credits_total) / gross * 100 if gross else 0
net_share = net / gross * 100 if gross else 0

k1, k2, k3, k4, k5 = st.columns(5)
with k1:
    render_kpi_card("Net Cost After Credits", money(net, cur_label), f"{net_share:.1f}% of gross cost",
                    SVG_NET_COST, "#E8F0FE", sparkline(daily_all["net_cost"], "#2563EB", "net"))
with k2:
    render_kpi_card("Gross Cost", money(gross, cur_label), f"{period_label}",
                    SVG_GROSS_COST, "#F1EAFE", sparkline(daily_all["gross_cost"], "#8B5CF6", "gross"))
with k3:
    render_kpi_card("Credits / Adjustments", money(credits_total, cur_label), f"{credit_share:.1f}% of gross cost",
                    SVG_CREDITS, "#E3F8EC", sparkline(daily_all["credits"], "#22C55E", "credits"))
with k4:
    render_kpi_card("Billing Rows", f"{row_count:,}", f"Avg {row_count / n_days:,.0f} rows / day",
                    SVG_ROWS, "#E6F0FF", sparkline(daily_all["billing_rows"], "#3B82F6", "rows"))
with k5:
    render_kpi_card("Services", f"{services_count:,}", f"across {projects_count:,} projects",
                    SVG_SERVICES, "#FEF1DC", sparkline(daily_all["services"], "#F59E0B", "svc", bars=True))

st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)


# ─── PAGE: Overview ─────────────────────────────────────────────────────────
if page == "Overview":
    # Owner + feature labels: one cached BigQuery scan feeds both donuts below.
    label_df, label_error = None, ""
    try:
        try:
            label_df = load_owner_feature_cost(start_day, end_day, services, projects, regions, skus, currency, resource_search, True)
        except Exception:
            # curated feature-mapping table not readable -> fall back to labels only
            label_df = load_owner_feature_cost(start_day, end_day, services, projects, regions, skus, currency, resource_search, False)
        label_df = label_df.copy()
        label_df["net_cost"] = pd.to_numeric(label_df["net_cost"], errors="coerce").fillna(0)
        owner_map = load_owner_map()
        if owner_map:  # spend without an owner label falls back to the project -> owner mapping
            unmapped = label_df["owner_name"] == NOT_LABELED
            label_df.loc[unmapped, "owner_name"] = label_df.loc[unmapped, "project_id"].map(owner_map).fillna(NOT_LABELED)
    except Exception as exc:
        label_df = None
        label_error = str(exc).splitlines()[0][:200]

    left, right = st.columns([1.7, 1])
    with left:
        with card("trend"):
            card_header(
                "Daily Cost Trend",
                right_html=('<div class="legend-inline"><span><i style="background:#A9C8F7"></i>Gross Cost</span>'
                            '<span><i style="background:#2563EB"></i>Net Cost</span></div>'),
            )
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=daily_all["usage_date"], y=daily_all["gross_cost"], name="Gross cost", mode="lines",
                line=dict(color="#A9C8F7", width=2, shape="spline", smoothing=0.6),
                fill="tozeroy", fillcolor="rgba(169,200,247,.18)",
                hovertemplate=f"Gross: {cur_symbol}%{{y:,.2f}}<extra></extra>",
            ))
            fig.add_trace(go.Scatter(
                x=daily_all["usage_date"], y=daily_all["net_cost"], name="Net after credits",
                mode="lines+markers" if len(daily_all) <= 45 else "lines",
                line=dict(color="#2563EB", width=2.5, shape="spline", smoothing=0.6),
                marker=dict(size=6, color="#FFFFFF", line=dict(color="#2563EB", width=2)),
                fill="tozeroy", fillcolor="rgba(37,99,235,.10)",
                hovertemplate=f"Net: {cur_symbol}%{{y:,.2f}}<extra></extra>",
            ))
            style_chart(fig, 352)
            fig.update_layout(hovermode="x unified")
            fig.update_xaxes(tickformat="%b %d")
            fig.update_yaxes(tickprefix=cur_symbol, tickformat="~s", rangemode="tozero")
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    with left:
        with card("donut_owner"):
            if label_df is None:
                card_header("Cost by Owner", badge="Top 6 + Others")
                st.info("Could not load cost by owner.")
                st.caption(label_error)
            else:
                ow_total = float(label_df.loc[label_df["net_cost"] > 0, "net_cost"].sum())
                ow_unlabeled = float(label_df.loc[(label_df["owner_name"] == NOT_LABELED) & (label_df["net_cost"] > 0), "net_cost"].sum())
                ow_desc = f"{ow_unlabeled / ow_total * 100:.0f}% of cost has no owner label" if ow_total > 0 and ow_unlabeled > 0 else ""
                card_header("Cost by Owner", ow_desc, badge="Top 6 + Others")
                ow = top_with_others(label_df, "owner_name", "net_cost", 6)
                if ow.empty:
                    st.info("No positive net cost for these filters and dates.")
                else:
                    render_donut(ow, money(ow_total, cur_label), "Total Cost", height=290)
        with card("project"):
            card_header("Cost by Project", "Top projects ranked by net cost", badge="Top 12")
            render_ranked_list(df, "project", "net_cost", cur_label, 12, "linear-gradient(90deg,#3B82F6,#60A5FA)")

    with right:
        with card("donut_service"):
            card_header("Cost by Service", badge="Top 6 + Others")
            svc = top_with_others(df, "service", "net_cost", 6)
            if svc.empty:
                st.info("No positive net cost for these filters and dates.")
            else:
                render_donut(svc, money(net, cur_label), "Total Cost", height=290)

        with card("donut_feature"):
            if label_df is None:
                card_header("Cost by Feature", badge="Top 6 + Others")
                st.info("Could not load cost by feature.")
                st.caption(label_error)
            else:
                fe_total = float(label_df.loc[label_df["net_cost"] > 0, "net_cost"].sum())
                fe_unlabeled = float(label_df.loc[(label_df["feature_name"] == NOT_LABELED) & (label_df["net_cost"] > 0), "net_cost"].sum())
                fe_desc = f"{fe_unlabeled / fe_total * 100:.0f}% of cost has no feature label" if fe_total > 0 and fe_unlabeled > 0 else ""
                card_header("Cost by Feature", fe_desc, badge="Top 6 + Others")
                fe = top_with_others(label_df, "feature_name", "net_cost", 6)
                if fe.empty:
                    st.info("No positive net cost for these filters and dates.")
                else:
                    render_donut(fe, money(fe_total, cur_label), "Total Cost", height=290)
        with card("donut_credits"):
            card_header("Credits by Service", "Largest credit / adjustment amounts by service")
            credit_df = df.assign(credit_abs=df["credits"].abs())
            credit_df = credit_df[credit_df["credits"] != 0]
            if credit_df.empty:
                st.info("No credits were recorded for these filters and dates.")
            else:
                cred = top_with_others(credit_df, "service", "credit_abs", 6)
                render_donut(cred, money(abs(credits_total), cur_label), "Total Credits", height=230)


# ─── PAGE: Resource explorer ────────────────────────────────────────────────
elif page == "Resource explorer":
    with card("resources"):
        card_header("Resource-level cost explorer",
                    "Drill into cost by individual resource. Query runs on demand and returns up to 1,000 grouped rows.")
        signature = (str(start_day), str(end_day), tuple(services), tuple(projects), tuple(regions), tuple(skus), currency, resource_search.strip())
        if st.button("Load resource details (up to 1,000 rows)", type="primary"):
            try:
                with loading_overlay("Loading resource details", "Querying resource-level cost from BigQuery."):
                    result = load_resource_data(start_day, end_day, services, projects, regions, skus, currency, resource_search)
                st.session_state["finops_resource_result"] = result
                st.session_state["finops_resource_signature"] = signature
            except Exception as exc:
                st.error("Unable to load resource details.")
                st.code(str(exc))
        if st.session_state.get("finops_resource_signature") == signature:
            resource_df = st.session_state["finops_resource_result"].copy()
            if resource_df.empty:
                st.info("No resource combinations matched the current filters.")
            else:
                for col in ["gross_cost", "credits", "net_cost", "usage_amount", "billing_rows"]:
                    resource_df[col] = pd.to_numeric(resource_df[col], errors="coerce").fillna(0)
                top_n = st.select_slider("Rows to display", options=[25, 50, 100, 250, 500, 1000], value=100)
                st.dataframe(resource_df.head(top_n), use_container_width=True, hide_index=True, column_config={
                    "gross_cost": st.column_config.NumberColumn("Gross cost", format="%.2f"),
                    "credits": st.column_config.NumberColumn("Credits", format="%.2f"),
                    "net_cost": st.column_config.NumberColumn("Net cost", format="%.2f"),
                    "usage_amount": st.column_config.NumberColumn("Usage quantity", format="%.4f"),
                    "billing_rows": st.column_config.NumberColumn("Billing rows", format="%d")})
                st.caption(f"Showing {min(top_n, len(resource_df)):,} of {len(resource_df):,} returned combinations.")
                dl1, dl2, _ = st.columns([1, 1, 2])
                with dl1:
                    st.download_button("Download displayed rows (CSV)", data=resource_df.head(top_n).to_csv(index=False).encode("utf-8-sig"), file_name=f"gcp_finops_resources_{start_day}_{end_day}.csv", mime="text/csv", use_container_width=True)
                with dl2:
                    st.download_button("Download all rows (CSV)", data=resource_df.to_csv(index=False).encode("utf-8-sig"), file_name=f"gcp_finops_resources_all_{start_day}_{end_day}.csv", mime="text/csv", use_container_width=True)
        else:
            st.info("Click **Load resource details** to run the resource-level query for the current filters.")


# ─── PAGE: Cost breakdowns ──────────────────────────────────────────────────
elif page == "Cost breakdowns":
    b1, b2 = st.columns(2)
    with b1:
        with card("region"):
            card_header("Region Cost Distribution", "Net cost broken down by cloud region", badge="Top 20")
            render_ranked_list(df, "region", "net_cost", cur_label, 20, "linear-gradient(90deg,#14B8A6,#5EEAD4)")
    with b2:
        with card("sku"):
            card_header("SKU / Billing Meter", "Top SKUs by net cost", badge="Top 20")
            render_ranked_list(df, "sku", "net_cost", cur_label, 20, "linear-gradient(90deg,#8B5CF6,#C4B5FD)")

    st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)
    with card("matrix"):
        card_header("Service × Project Matrix", "Heatmap showing net cost at the intersection of top services and projects")
        heat = df.groupby(["service", "project"], as_index=False)["net_cost"].sum()
        top_services = df.groupby("service")["net_cost"].sum().nlargest(12).index
        top_projects = df.groupby("project")["net_cost"].sum().nlargest(12).index
        heat = heat[heat["service"].isin(top_services) & heat["project"].isin(top_projects)]
        if not heat.empty:
            pivot = heat.pivot(index="service", columns="project", values="net_cost").fillna(0)
            blue_scale = [
                [0.0, "#F4F8FF"], [0.2, "#DCE9FD"], [0.4, "#B6D0FB"],
                [0.6, "#7FAEF7"], [0.8, "#3B82F6"], [1.0, "#1D4ED8"],
            ]
            fig = px.imshow(pivot, aspect="auto", color_continuous_scale=blue_scale,
                            labels=dict(x="Project", y="Service", color="Net cost"))
            style_chart(fig, 480)
            fig.update_layout(coloraxis_colorbar=dict(thickness=10, len=0.8, outlinewidth=0, tickfont=dict(size=11, color=MUTED)))
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("Not enough data to build the service × project matrix for the current filters.")


# ─── PAGE: CPU & RAM (VM utilization, right-sizing, owner/client labels) ─────
elif page == "CPU & RAM":
    STATUS_COLORS = {"Idle": "#94A3B8", "Over-provisioned": "#F59E0B", "Right-sized": "#10B981", "Under-provisioned": "#EF4444",
                     "Insufficient data": "#CBD5E1", "Not evaluated": "#E2E8F0"}
    C_NOW, C_REC, C_WARN, C_PURPLE = "#3B82F6", "#34D399", "#F59E0B", "#8B5CF6"

    def show_chart(fig, height, legend=False, subplots=False):
        style_chart(fig, height, legend=legend)
        if subplots:  # leave room for the subplot titles (and put the legend above them)
            fig.update_layout(margin=dict(t=(70 if legend else 34)), legend=dict(y=1.14))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    def sub_title(text):
        st.markdown(f'<div class="card-title" style="font-size:13px;margin:6px 0 2px;">{html.escape(text)}</div>', unsafe_allow_html=True)

    projects_t = tuple(MONITORING_PROJECTS)
    vm_signature = (str(start_day), str(end_day), projects_t, currency)

    with card("vm_intro"):
        card_header(
            "VM Utilization & Right-sizing",
            f"Running Compute Engine VMs in {', '.join(MONITORING_PROJECTS)} · CPU/RAM from Cloud Monitoring, machine sizes from "
            f"Compute Engine, cost from the billing export · authenticated with {os.path.basename(MONITORING_KEY_FILE)}. "
            "Uses the selected date range (metrics are kept ~6 weeks).",
        )
        with st.expander("Right-sizing assumptions (change anytime, no reload needed)", expanded=False):
            a1, a2, a3 = st.columns(3)
            target_cpu = a1.slider("Target CPU at peak (%)", 30, 85, 60, help="After resizing, the p95 hourly peak CPU should land near this value.")
            target_mem = a2.slider("Target RAM at peak (%)", 40, 90, 70, help="After resizing, the p95 hourly peak RAM should land near this value.")
            min_saving = a3.slider("Minimum saving to recommend (%)", 0, 50, 10)
            st.caption("Demand = p95 of hourly peak utilization over the date range. Needed size = current size × p95 ÷ target. "
                       "The cheapest same-series machine type (or a resized custom type for custom VMs) that fits both CPU and RAM is recommended. "
                       "VMs without RAM data (no Ops Agent) keep their RAM and only shrink vCPUs. Saving = the VM's billed compute cost × estimated price reduction "
                       "(vCPU ≈ 7.5× the price of 1 GB RAM). Treat savings as estimates; committed-use discounts may reduce what you actually save.")
        if st.button("Load VM utilization & right-sizing", type="primary"):
            try:
                with loading_overlay("Loading VM utilization", "Reading VMs, CPU/RAM metrics and billed cost."):
                    inventory, inv_errors = load_vm_inventory(projects_t)
                    util, util_errors = load_vm_metrics(projects_t, start_day, end_day)
                    zones = tuple(sorted(inventory.loc[inventory["status"] == "RUNNING", "zone"].unique()))
                    catalogs, cat_errors = load_machine_type_catalog(projects_t[0], zones) if zones else ({}, [])
                    cost_errors = []
                    try:
                        vm_costs = load_vm_costs(projects_t, start_day, end_day, currency)
                    except Exception as exc:
                        vm_costs = pd.DataFrame(columns=["instance_id", "currency", "net_cost"])
                        cost_errors.append(f"billing cost lookup failed: {str(exc).splitlines()[0][:200]}")
                st.session_state["finops_vm_data"] = dict(inventory=inventory, util=util, catalogs=catalogs, costs=vm_costs,
                                                          errors=list(dict.fromkeys(inv_errors + util_errors + cat_errors + cost_errors)))
                st.session_state["finops_vm_signature"] = vm_signature
            except Exception as exc:
                st.error("Unable to load VM data.")
                st.code(str(exc))

    if st.session_state.get("finops_vm_signature") != vm_signature:
        st.info("Click **Load VM utilization & right-sizing**. It reads the VM list, hourly CPU/RAM metrics and billed cost for the selected dates.")
    else:
        vm_data = st.session_state["finops_vm_data"]
        if vm_data["errors"]:
            with st.expander(f"{len(vm_data['errors'])} request(s) failed", expanded=False):
                st.caption("Typically a missing Compute Viewer / Monitoring Viewer role, or an API that is disabled in the project.")
                for message in vm_data["errors"]:
                    st.code(message)
        window_start = datetime.combine(start_day, dtime.min, tzinfo=timezone.utc)
        window_end = min(datetime.combine(end_day + timedelta(days=1), dtime.min, tzinfo=timezone.utc), datetime.now(timezone.utc))
        all_recs = build_recommendations(vm_data["inventory"], vm_data["util"], vm_data["costs"], vm_data["catalogs"], window_start, window_end,
                                         Params(target_cpu=target_cpu, target_mem=target_mem, min_saving_pct=min_saving))
        filtered = all_recs
        recs = all_recs

        # ── Owner / client / environment / application label filters ──
        if not all_recs.empty:
            filter_keys = {col: f"finops_vm_f_{col}" for col in LABEL_COLUMNS}
            filter_keys.update(series="finops_vm_f_series", confidence="finops_vm_f_conf", name="finops_vm_f_name")

            def reset_vm_filters():
                for key in filter_keys.values():
                    st.session_state[key] = "" if key.endswith("_name") else []

            with st.expander("Filters: owner, client, environment, application…", expanded=True):
                st.caption("Leave a filter empty to include everything. Values come from the VM labels in Google Cloud; "
                           f"VMs without a usable label show as **{NOT_LABELED}**.")
                label_cols = list(LABEL_FIELDS.items())
                for row_start in range(0, len(label_cols), 4):
                    holders = st.columns(4)
                    for holder, (col, (title, _)) in zip(holders, label_cols[row_start:row_start + 4]):
                        label_opts = sorted(all_recs[col].dropna().unique(), key=lambda v: (v == NOT_LABELED, str(v).lower()))
                        holder.multiselect(title, label_opts, key=filter_keys[col], placeholder=f"All ({len(label_opts)})")
                x1, x2, x3, x4 = st.columns(4)
                x1.multiselect("Machine series", sorted(all_recs["series"].dropna().unique()), key=filter_keys["series"], placeholder="All")
                conf_options = sorted([c for c in all_recs["confidence"].dropna().unique() if c])
                x2.multiselect("Confidence", conf_options, key=filter_keys["confidence"], placeholder="All")
                x3.text_input("VM name contains", key=filter_keys["name"], placeholder="e.g. iksgcp-1")
                with x4:
                    st.write("")
                    st.button("Clear filters", on_click=reset_vm_filters, use_container_width=True)
                apply_everywhere = st.checkbox("Also apply filters to the KPIs and charts (otherwise only the recommendations table is filtered)",
                                               value=True, key="finops_vm_f_all")
            mask = pd.Series(True, index=all_recs.index)
            for col in LABEL_COLUMNS:
                chosen_values = st.session_state.get(filter_keys[col]) or []
                if chosen_values:
                    mask &= all_recs[col].isin(chosen_values)
            if st.session_state.get(filter_keys["series"]):
                mask &= all_recs["series"].isin(st.session_state[filter_keys["series"]])
            if st.session_state.get(filter_keys["confidence"]):
                mask &= all_recs["confidence"].isin(st.session_state[filter_keys["confidence"]])
            name_query = (st.session_state.get(filter_keys["name"]) or "").strip()
            if name_query:
                mask &= all_recs["instance_name"].str.contains(name_query, case=False, regex=False, na=False)
            filtered = all_recs[mask]
            recs = filtered if apply_everywhere else all_recs
            st.caption(f"Showing **{len(filtered):,}** of {len(all_recs):,} running VMs after filters.")

        if all_recs.empty:
            st.warning("No running VMs were found for the configured project(s).")
        elif filtered.empty:
            st.warning("No VMs match the selected filters. Clear or loosen a filter.")
        else:
            vm_cur = vm_data["costs"]["currency"].iloc[0] if not vm_data["costs"].empty else cur_label
            evaluated = recs[recs["status"].isin(["Idle", "Over-provisioned", "Right-sized", "Under-provisioned"])]
            down = recs[recs["status"].isin(["Idle", "Over-provisioned"])]
            up = recs[recs["status"] == "Under-provisioned"]
            savings = float(down["monthly_saving"].sum(skipna=True))
            extra_cost = float(-up["monthly_saving"].sum(skipna=True))
            base_cost = float(evaluated["monthly_cost"].sum(skipna=True))
            terminated = int((vm_data["inventory"]["status"] != "RUNNING").sum())

            v1, v2, v3, v4, v5, v6 = st.columns(6)
            v1.metric("Running VMs", f"{len(recs):,}", help=f"{terminated} stopped/terminated VMs are excluded.")
            v2.metric("Downsize candidates", f"{len(down):,}")
            v3.metric("Under-provisioned", f"{len(up):,}")
            v4.metric("Est. monthly savings", money(savings, vm_cur),
                      delta=(f"{savings / base_cost:.0%} of VM compute cost" if base_cost > 0 else None), delta_color="off")
            v5.metric("Extra cost to fix under-provisioned", money(extra_cost, vm_cur))
            v6.metric("vCPUs now → recommended", f"{int(evaluated['vcpus'].sum()):,} → {int(evaluated['rec_vcpus'].sum()):,}")
            st.markdown('<div style="height:10px"></div>', unsafe_allow_html=True)

            tab_rec, tab_fleet, tab_vm = st.tabs(["Recommendations", "Fleet utilization", "VM drill-down"])

            # ───────── Recommendations ─────────
            with tab_rec:
                r1, r2 = st.columns([1.5, 1])
                with r1:
                    with card("vm_scatter"):
                        card_header("CPU vs RAM Demand", "Bubble size = monthly cost. Bottom-left of the dashed lines = oversized in both CPU and RAM.")
                        scatter = recs[recs["cpu_p95"].notna() & recs["mem_p95"].notna()].copy()
                        if scatter.empty:
                            st.info("No VM reports RAM (Ops Agent not installed), so the CPU-vs-RAM map cannot be drawn.")
                        else:
                            fallback = scatter["monthly_cost"].median()
                            scatter["bubble"] = scatter["monthly_cost"].fillna(fallback if pd.notna(fallback) else 1).clip(lower=1)
                            fig = px.scatter(scatter, x="cpu_p95", y="mem_p95", size="bubble", color="status", color_discrete_map=STATUS_COLORS, size_max=38,
                                             hover_name="instance_name",
                                             hover_data={"owner": True, "client": True, "machine_type": True, "recommended_type": True,
                                                         "cpu_p95": ":.0f", "mem_p95": ":.0f", "bubble": False},
                                             labels={"cpu_p95": "CPU p95 of hourly peaks (%)", "mem_p95": "RAM p95 of hourly peaks (%)"})
                            fig.add_vline(x=target_cpu, line_dash="dash", line_color="#9AA6BB")
                            fig.add_hline(y=target_mem, line_dash="dash", line_color="#9AA6BB")
                            fig.update_xaxes(range=[0, 105])
                            fig.update_yaxes(range=[0, 105])
                            show_chart(fig, 400, legend=True)
                            st.caption(f"Only {len(scatter)} of {len(recs)} VMs report RAM.")
                with r2:
                    with card("vm_status"):
                        card_header("VMs by Right-sizing Status")
                        by_status = recs.groupby("status").agg(vms=("instance_id", "count"), cost=("monthly_cost", "sum")).reindex(STATUS_ORDER).dropna(subset=["vms"]).reset_index()
                        fig = px.bar(by_status, x="status", y="vms", color="status", color_discrete_map=STATUS_COLORS, text="vms",
                                     hover_data={"cost": ":,.0f"})
                        fig.update_layout(xaxis_title="", yaxis_title="VMs")
                        show_chart(fig, 400)

                s1, s2 = st.columns(2)
                with s1:
                    with card("vm_topsave"):
                        card_header("Top 15 Monthly Savings by VM", badge="Downsize candidates")
                        top = down.dropna(subset=["monthly_saving"]).nlargest(15, "monthly_saving").sort_values("monthly_saving")
                        if top.empty:
                            st.info("No downsize savings to show (no candidates, or no billing cost matched these VMs).")
                        else:
                            top = top.assign(move=top["machine_type"] + " → " + top["recommended_type"].astype(str),
                                             label=top["monthly_saving"].map(lambda v: money(v, vm_cur)))
                            fig = px.bar(top, x="monthly_saving", y="instance_name", orientation="h", color="status", color_discrete_map=STATUS_COLORS, text="label",
                                         hover_data={"move": True, "monthly_cost": ":,.0f", "monthly_saving": ":,.0f", "label": False})
                            fig.update_layout(yaxis_title="", xaxis_title=f"Estimated monthly saving ({vm_cur})")
                            show_chart(fig, 440, legend=True)
                with s2:
                    with card("vm_capacity"):
                        card_header("Capacity: Current vs Recommended", "vCPUs and RAM by machine series")
                        cap = evaluated.groupby("series", as_index=False).agg(vcpu_now=("vcpus", "sum"), vcpu_rec=("rec_vcpus", "sum"),
                                                                              ram_now=("mem_gb", "sum"), ram_rec=("rec_mem_gb", "sum"))
                        fig = make_subplots(rows=1, cols=2, subplot_titles=("vCPUs", "RAM (GB)"))
                        fig.add_bar(x=cap["series"], y=cap["vcpu_now"], name="Current", marker_color=C_NOW, legendgroup="now", row=1, col=1)
                        fig.add_bar(x=cap["series"], y=cap["vcpu_rec"], name="Recommended", marker_color=C_REC, legendgroup="rec", row=1, col=1)
                        fig.add_bar(x=cap["series"], y=cap["ram_now"], name="Current", marker_color=C_NOW, legendgroup="now", showlegend=False, row=1, col=2)
                        fig.add_bar(x=cap["series"], y=cap["ram_rec"], name="Recommended", marker_color=C_REC, legendgroup="rec", showlegend=False, row=1, col=2)
                        fig.update_layout(barmode="group")
                        show_chart(fig, 440, legend=True, subplots=True)

                with card("vm_owners"):
                    card_header("Who Owns the Savings?", "Estimated monthly savings and VM status mix grouped by a VM label")
                    dim_titles = {col: title for col, (title, _) in LABEL_FIELDS.items()}
                    dim = st.selectbox("Group by", list(dim_titles), format_func=lambda c: dim_titles[c], key="finops_vm_group_dim")
                    grouped = recs.assign(
                        saving=recs["monthly_saving"].where(recs["status"].isin(["Idle", "Over-provisioned"]), 0.0),
                        extra=(-recs["monthly_saving"]).where(recs["status"] == "Under-provisioned", 0.0)).groupby(dim, as_index=False).agg(
                        VMs=("instance_id", "count"), saving=("saving", "sum"), extra=("extra", "sum"), cost=("monthly_cost", "sum"))
                    g_left, g_right = st.columns(2)
                    with g_left:
                        sub_title(f"Estimated monthly savings by {dim_titles[dim].lower()}")
                        g_top = grouped.nlargest(15, "saving").sort_values("saving")
                        g_top = g_top.assign(label=g_top["saving"].map(lambda v: money(v, vm_cur)))
                        fig = px.bar(g_top, x="saving", y=dim, orientation="h", text="label", color_discrete_sequence=[C_WARN],
                                     hover_data={"VMs": True, "extra": ":,.0f", "cost": ":,.0f", "saving": ":,.0f", "label": False})
                        fig.update_layout(yaxis_title="", xaxis_title=f"Est. monthly saving ({vm_cur})")
                        show_chart(fig, 400)
                    with g_right:
                        sub_title(f"VM status mix by {dim_titles[dim].lower()}")
                        top_groups = grouped.nlargest(12, "VMs")[dim]
                        mix = recs[recs[dim].isin(top_groups)].groupby([dim, "status"], as_index=False).size().rename(columns={"size": "VMs"})
                        fig = px.bar(mix, x="VMs", y=dim, color="status", orientation="h", color_discrete_map=STATUS_COLORS, barmode="stack",
                                     category_orders={"status": STATUS_ORDER})
                        fig.update_layout(yaxis_title="", xaxis_title="VMs")
                        show_chart(fig, 400, legend=True)

                with card("vm_table"):
                    card_header("Right-sizing Recommendations", "Negative savings on under-provisioned VMs are the extra monthly cost of resizing them up.")
                    chosen = st.multiselect("Status", STATUS_ORDER, default=[s for s in STATUS_ORDER if s in set(filtered["status"])], key="finops_vm_status_filter")
                    table = filtered[filtered["status"].isin(chosen)].sort_values("monthly_saving", ascending=False, na_position="last")
                    table = table[["instance_name"] + LABEL_COLUMNS + ["zone", "status", "machine_type", "vcpus", "mem_gb", "cpu_avg", "cpu_p95", "mem_avg", "mem_p95",
                                   "recommended_type", "rec_vcpus", "rec_mem_gb", "monthly_cost", "monthly_saving", "confidence", "reason", "instance_id", "labels"]]
                    label_titles = {col: title for col, (title, _) in LABEL_FIELDS.items()}
                    st.dataframe(table, use_container_width=True, hide_index=True, column_config={
                        **label_titles, "labels": "All labels",
                        "instance_name": "VM", "machine_type": "Current type", "vcpus": st.column_config.NumberColumn("vCPUs", format="%d"),
                        "mem_gb": st.column_config.NumberColumn("RAM (GB)", format="%.1f"),
                        "cpu_avg": st.column_config.NumberColumn("CPU avg %", format="%.1f"), "cpu_p95": st.column_config.NumberColumn("CPU p95 %", format="%.1f"),
                        "mem_avg": st.column_config.NumberColumn("RAM avg %", format="%.1f"), "mem_p95": st.column_config.NumberColumn("RAM p95 %", format="%.1f"),
                        "recommended_type": "Recommended type", "rec_vcpus": st.column_config.NumberColumn("Rec. vCPUs", format="%d"),
                        "rec_mem_gb": st.column_config.NumberColumn("Rec. RAM (GB)", format="%.1f"),
                        "monthly_cost": st.column_config.NumberColumn(f"Monthly cost ({vm_cur})", format="%.0f"),
                        "monthly_saving": st.column_config.NumberColumn(f"Est. monthly saving ({vm_cur})", format="%.0f"),
                        "confidence": "Confidence", "reason": "Why", "instance_id": "Instance ID"})
                    st.caption("Verify with the application owner before changing a VM.")
                    st.download_button("Download right-sizing recommendations (CSV)", data=table.to_csv(index=False).encode("utf-8-sig"),
                                       file_name=f"gcp_finops_rightsizing_{start_day}_{end_day}.csv", mime="text/csv")

            # ───────── Fleet utilization ─────────
            with tab_fleet:
                util_fleet = vm_data["util"]
                util_fleet = util_fleet[util_fleet["instance_id"].isin(recs["instance_id"])]
                f1, f2 = st.columns([1.6, 1])
                with f1:
                    with card("vm_fleet_trend"):
                        card_header("Average Utilization Across Running VMs", "Hourly mean CPU and RAM")
                        trend = util_fleet.groupby("time", as_index=False)[["cpu_pct", "mem_pct"]].mean().sort_values("time")
                        trend = trend.melt(id_vars="time", value_vars=["cpu_pct", "mem_pct"], var_name="Metric", value_name="Utilization %").dropna()
                        trend["Metric"] = trend["Metric"].map({"cpu_pct": "CPU", "mem_pct": "RAM (VMs with agent)"})
                        fig = px.line(trend, x="time", y="Utilization %", color="Metric", color_discrete_sequence=[C_NOW, C_WARN])
                        fig.update_yaxes(range=[0, 100])
                        fig.update_xaxes(title_text="")
                        show_chart(fig, 360, legend=True)
                with f2:
                    with card("vm_fleet_hist"):
                        card_header("How Busy Are the VMs?", "Distribution of CPU p95")
                        fig = px.histogram(recs.dropna(subset=["cpu_p95"]), x="cpu_p95", color="status", color_discrete_map=STATUS_COLORS, nbins=20,
                                           labels={"cpu_p95": "CPU p95 of hourly peaks (%)"})
                        fig.update_layout(yaxis_title="VMs")
                        show_chart(fig, 360, legend=True)
                g1, g2 = st.columns(2)
                with g1:
                    with card("vm_fleet_cpu"):
                        card_header("Top 15 VMs by CPU", "p95 of hourly peaks")
                        top_cpu = recs.dropna(subset=["cpu_p95"]).nlargest(15, "cpu_p95").sort_values("cpu_p95")
                        fig = px.bar(top_cpu, x="cpu_p95", y="instance_name", orientation="h", color="status", color_discrete_map=STATUS_COLORS,
                                     hover_data={"machine_type": True, "cpu_avg": ":.1f"})
                        fig.update_layout(yaxis_title="", xaxis_title="CPU p95 %")
                        show_chart(fig, 420, legend=True)
                with g2:
                    with card("vm_fleet_ram"):
                        card_header("Top 15 VMs by RAM", "p95 of hourly peaks")
                        top_ram = recs.dropna(subset=["mem_p95"]).nlargest(15, "mem_p95").sort_values("mem_p95")
                        if top_ram.empty:
                            st.info("No VM reports RAM. Install the Ops Agent to see RAM utilization.")
                        else:
                            fig = px.bar(top_ram, x="mem_p95", y="instance_name", orientation="h", color="status", color_discrete_map=STATUS_COLORS,
                                         hover_data={"machine_type": True, "mem_avg": ":.1f"})
                            fig.update_layout(yaxis_title="", xaxis_title="RAM p95 %")
                            show_chart(fig, 420, legend=True)
                with card("vm_fleet_series"):
                    card_header("Average Utilization by Machine Series")
                    by_series = recs.groupby("series", as_index=False).agg(CPU=("cpu_avg", "mean"), RAM=("mem_avg", "mean"), VMs=("instance_id", "count"))
                    by_series = by_series.melt(id_vars=["series", "VMs"], value_vars=["CPU", "RAM"], var_name="Metric", value_name="Avg utilization %").dropna()
                    fig = px.bar(by_series, x="series", y="Avg utilization %", color="Metric", barmode="group", hover_data={"VMs": True},
                                 color_discrete_sequence=[C_NOW, C_WARN])
                    fig.update_layout(xaxis_title="Machine series")
                    show_chart(fig, 340, legend=True)

            # ───────── VM drill-down ─────────
            with tab_vm:
                ordered = recs.sort_values(["monthly_saving", "instance_name"], ascending=[False, True], na_position="last")
                vm_labels = {row["instance_id"]: f"{row['instance_name']} · {row['machine_type']} · {row['status']}" for _, row in ordered.iterrows()}
                picked = st.selectbox("Choose a VM", list(vm_labels.keys()), format_func=lambda i: vm_labels[i])
                vm = recs[recs["instance_id"] == picked].iloc[0]
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Current type", vm["machine_type"])
                m2.metric("Provisioned", f"{vm['vcpus']:.0f} vCPU · {vm['mem_gb']:.1f} GB" if pd.notna(vm["vcpus"]) else "n/a")
                m3.metric("Status", vm["status"])
                m4.metric("Recommended", str(vm["recommended_type"]) if vm["recommended_type"] else "n/a")
                m5.metric("Est. monthly saving", money(vm["monthly_saving"], vm_cur) if pd.notna(vm["monthly_saving"]) else "n/a")
                st.caption(f"{vm['reason']} · confidence: {vm['confidence'] or 'n/a'} · hourly data points: {int(vm['points'])}")

                with card("vm_ownership"):
                    card_header("Ownership", "From the VM labels in Google Cloud")
                    kv_items = "".join(
                        f'<div><div class="kv-label">{html.escape(title)}</div>'
                        f'<div class="kv-value{" muted-val" if vm[col] == NOT_LABELED else ""}">{html.escape(str(vm[col]))}</div></div>'
                        for col, (title, _) in LABEL_FIELDS.items())
                    st.markdown(f'<div class="kv-grid">{kv_items}</div>', unsafe_allow_html=True)
                    if vm["labels"]:
                        with st.expander("All labels on this VM"):
                            st.code(vm["labels"].replace("; ", "\n"))

                vm_util = vm_data["util"]
                vm_util = vm_util[vm_util["instance_id"] == picked].sort_values("time")
                d1, d2 = st.columns(2)
                for holder, mean_col, max_col, title, p95, target in ((d1, "cpu_pct", "cpu_max_pct", "CPU %", vm["cpu_p95"], target_cpu),
                                                                      (d2, "mem_pct", "mem_max_pct", "RAM %", vm["mem_p95"], target_mem)):
                    with holder:
                        with card(f"vm_ts_{mean_col}"):
                            card_header(f"{title} Over Time", "Hourly average and peak")
                            if vm_util[mean_col].notna().any():
                                fig = go.Figure()
                                fig.add_trace(go.Scatter(x=vm_util["time"], y=vm_util[max_col], name="Hourly peak", line=dict(color="#EF4444", width=1)))
                                fig.add_trace(go.Scatter(x=vm_util["time"], y=vm_util[mean_col], name="Hourly average", line=dict(color=C_NOW, width=2)))
                                fig.add_hline(y=target, line_dash="dash", line_color="#9AA6BB", annotation_text=f"target {target:.0f}%")
                                if pd.notna(p95):
                                    fig.add_hline(y=p95, line_dash="dot", line_color=C_WARN, annotation_text=f"p95 {p95:.0f}%", annotation_position="bottom right")
                                fig.update_layout(yaxis_range=[0, 105])
                                show_chart(fig, 330, legend=True)
                            else:
                                st.info(f"No {title.split()[0]} data for this VM." + (" Install the Ops Agent to report RAM." if title.startswith("RAM") else ""))
                if pd.notna(vm["vcpus"]):
                    with card("vm_capacity_one"):
                        card_header("Capacity: Provisioned vs Used vs Recommended")
                        cap_fig = make_subplots(rows=1, cols=2, subplot_titles=("vCPUs", "RAM (GB)"))
                        used_cpu = vm["vcpus"] * vm["cpu_p95"] / 100 if pd.notna(vm["cpu_p95"]) else None
                        used_mem = vm["mem_gb"] * vm["mem_p95"] / 100 if pd.notna(vm["mem_p95"]) else None
                        bar_x = ["Provisioned", "Used at p95 peak", "Recommended"]
                        bar_colors = [C_NOW, C_WARN, C_REC]
                        cap_fig.add_bar(x=bar_x, y=[vm["vcpus"], used_cpu, vm["rec_vcpus"]], marker_color=bar_colors, showlegend=False, row=1, col=1)
                        cap_fig.add_bar(x=bar_x, y=[vm["mem_gb"], used_mem, vm["rec_mem_gb"]], marker_color=bar_colors, showlegend=False, row=1, col=2)
                        show_chart(cap_fig, 320, subplots=True)


# ─── PAGE: Data & export ────────────────────────────────────────────────────
elif page == "Data & export":
    with card("data"):
        card_header("Aggregated billing data",
                    "Summary grouped by usage date, service, project, region and SKU. Amounts remain in the source currency.")
        st.dataframe(df.sort_values("net_cost", ascending=False).head(20000), use_container_width=True, hide_index=True)
        st.caption("The table displays at most 20,000 summary rows.")
        dl_a, dl_b, _ = st.columns([1, 1, 2])
        with dl_a:
            st.download_button("Download summary data (CSV)", data=df.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"gcp_finops_summary_{start_day}_{end_day}.csv", mime="text/csv", use_container_width=True)
        with dl_b:
            daily_export = df.groupby(["usage_date", "currency"], as_index=False)[["gross_cost", "credits", "net_cost"]].sum().sort_values("usage_date")
            st.download_button("Download daily trend (CSV)", data=daily_export.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"gcp_finops_daily_cost_{start_day}_{end_day}.csv", mime="text/csv", use_container_width=True)


# ─── FOOTER ─────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="finops-footer">
    <p>FinOps dashboard · Read-only billing analytics · Costs reflect the latest rows available in the configured Cloud Billing export.
    Export delays, credits and adjustments can change historical totals.</p>
    <p style="margin-top:4px;">Source table: <code>{TABLE_ID}</code></p>
</div>
""", unsafe_allow_html=True)
