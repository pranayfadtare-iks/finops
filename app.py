"""GCP FinOps dashboard: cached summary view and on-demand resource details."""
from datetime import date, timedelta
import base64
import html
import os
import pathlib
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from google.cloud import bigquery

st.set_page_config(
    page_title="GCP FinOps | Cost Intelligence",
    page_icon="💠",
    layout="wide",
    initial_sidebar_state="expanded",
)
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "iks-finops")
TABLE_ID = os.getenv("GCP_BILLING_TABLE", "iks-finops.GCP_Finops_billing.gcp_billing_export_resource_v1_019EFB_4492BC_7B3DE6")
CACHE_TTL = int(os.getenv("FINOPS_CACHE_TTL", "300"))

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
CHART_PALETTE = ["#2563EB", "#A9C8F7", "#8B5CF6", "#34D399", "#FBBF24", "#F97316"]

# ─── Inline SVG icons for KPI cards ──────────────────────────────────────────
def _icon(inner, color):
    return (f'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">{inner}</svg>')


SVG_NET_COST = _icon('<path d="M20 12V8H6a2 2 0 0 1 0-4h12v4"/><path d="M4 6v12a2 2 0 0 0 2 2h14v-4"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/>', "#2563EB")
SVG_GROSS_COST = _icon('<polyline points="3 17 9 11 13 15 21 7"/><polyline points="15 7 21 7 21 13"/>', "#7C3AED")
SVG_CREDITS = _icon('<rect x="3" y="8" width="18" height="13" rx="2"/><path d="M12 8v13"/><path d="M3 12h18"/><path d="M12 8c-2.5 0-4-1-4-2.5S9.5 3 11 3c1.5 0 1 2.5 1 5Zm0 0c2.5 0 4-1 4-2.5S14.5 3 13 3c-1.5 0-1 2.5-1 5Z"/>', "#16A34A")
SVG_ROWS = _icon('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>', "#3B82F6")
SVG_SERVICES = _icon('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>', "#F59E0B")

NAV_ITEMS = ["Overview", "Resource explorer", "Cost breakdowns", "Data & export"]

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
.page-sub {font-size: 12.5px; color: #64748B; margin-top: 4px; font-weight: 500;}
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
.kpi-card {
    display: flex; flex-direction: column; gap: 10px;
    background: #FFFFFF; border: 1px solid #E6ECF5; border-radius: 18px;
    box-shadow: 0 1px 2px rgba(15,27,61,.04), 0 10px 28px rgba(37,99,235,.05);
    padding: 18px 18px 14px 18px; min-height: 132px; overflow: hidden;
}
.kpi-top {display: flex; gap: 14px; align-items: center;}
.kpi-foot {display: flex; align-items: flex-end; justify-content: space-between; gap: 8px; margin-top: auto;}
.kpi-icon {
    flex: 0 0 44px; width: 44px; height: 44px; border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
}
.kpi-body {min-width: 0; flex: 1;}
.kpi-label {font-size: 12.5px; font-weight: 500; color: #475569; margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.kpi-value {font-size: 24px; font-weight: 700; color: #0F1B3D; line-height: 1.15; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; white-space: nowrap;}
.kpi-sub {font-size: 12px; color: #64748B; font-weight: 500; min-width: 0; line-height: 1.3;}
.kpi-spark {flex: 0 0 auto; display: block;}

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
.dl-list {display: flex; flex-direction: column; gap: 11px; padding-top: 14px;}
.dl-row {display: flex; align-items: center; gap: 10px; font-size: 13px;}
.dl-dot {width: 10px; height: 10px; border-radius: 50%; flex: 0 0 10px;}
.dl-name {flex: 1; color: #334155; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;}
.dl-pct {color: #0F1B3D; font-weight: 600; font-variant-numeric: tabular-nums;}

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

/* ── Footer ── */
.finops-footer {border-top: 1px solid #E2E8F0; padding-top: 16px; margin-top: 22px;}
.finops-footer p {color: #94A3B8; font-size: 12px; margin: 0;}
</style>
""", unsafe_allow_html=True)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def h(markup):
    """Collapse whitespace so Markdown never treats indented HTML as a code block."""
    return " ".join(markup.split())


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
def get_client():
    return bigquery.Client(project=PROJECT_ID)


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


# ─── UI building blocks ──────────────────────────────────────────────────────
def style_chart(fig, height=360):
    """Apply the unified SaaS chart design to any Plotly figure."""
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, system-ui, sans-serif", color="#475569", size=12),
        showlegend=False,
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
    colors = [OTHERS_COLOR if name == "Others" else DONUT_PALETTE[i % len(DONUT_PALETTE)] for i, name in enumerate(series.index)]
    fig = go.Figure(go.Pie(
        labels=list(series.index), values=list(series.values), hole=0.72, sort=False, direction="clockwise",
        marker=dict(colors=colors, line=dict(color="#FFFFFF", width=3)),
        textinfo="none", hovertemplate="<b>%{label}</b><br>%{value:,.2f} · %{percent}<extra></extra>",
    ))
    fig.add_annotation(text=f"<b>{center_value}</b>", x=0.5, y=0.54, showarrow=False, font=dict(size=19, color=INK))
    fig.add_annotation(text=center_label, x=0.5, y=0.43, showarrow=False, font=dict(size=12, color=MUTED))
    style_chart(fig, height)
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
            <div><div class="sp-small">Powered by</div><div class="sp-big">Google Cloud</div></div>
          </div>
          <svg class="sp-wave" viewBox="0 0 220 34" preserveAspectRatio="none">
            <path d="M0 18 C 40 4, 80 30, 120 16 S 190 6, 220 14 L220 34 L0 34 Z" fill="#BFD6FB" opacity=".55"/>
            <path d="M0 24 C 50 12, 90 34, 140 22 S 200 14, 220 22 L220 34 L0 34 Z" fill="#93BAF7" opacity=".5"/>
          </svg>
        </div>
    """), unsafe_allow_html=True)


# ─── 1. TOP BAR: title + global filters ──────────────────────────────────────
fc0, fc1, fc3, fc4, fc5, fc6 = st.columns([3, 1.15, 1.3, 1.3, 1.2, 1], vertical_alignment="center")

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
    with st.spinner("Loading filter options from BigQuery…"):
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
        st.session_state.pop("finops_resource_result", None)
        st.session_state.pop("finops_resource_signature", None)
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
    with st.spinner("Loading aggregated billing summary from BigQuery…"):
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

    with right:
        with card("service"):
            card_header("Cost by Service", badge="Top 6 + Others")
            svc = top_with_others(df, "service", "net_cost", 6)
            if svc.empty:
                st.info("No positive net cost for these filters and dates.")
            else:
                render_donut(svc, money(net, cur_label), "Total Cost", height=290)

    c1, c2 = st.columns([1.25, 1])
    with c1:
        with card("project"):
            card_header("Cost by Project", "Top projects ranked by net cost", badge="Top 12")
            render_ranked_list(df, "project", "net_cost", cur_label, 12, "linear-gradient(90deg,#3B82F6,#60A5FA)")
    with c2:
        with card("credits"):
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
                with st.spinner("Loading resource details from BigQuery…"):
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
