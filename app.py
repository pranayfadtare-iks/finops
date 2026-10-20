"""GCP FinOps dashboard: cost, owner mapping, and utilization signals."""
from datetime import date, datetime, timedelta, timezone
import os
import re

import pandas as pd
import plotly.express as px
import streamlit as st
from google.cloud import bigquery
from google.cloud import monitoring_v3
from google.cloud.monitoring_v3.types import Aggregation, ListTimeSeriesRequest, TimeInterval

st.set_page_config(page_title="GCP FinOps | Cost Intelligence", page_icon="💠", layout="wide")
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "iks-finops")
TABLE_ID = os.getenv("GCP_BILLING_TABLE", "iks-finops.GCP_Finops_billing.gcp_billing_export_resource_v1_019EFB_4492BC_7B3DE6")
MONITORING_PROJECT = os.getenv("MONITORING_PROJECT_ID", "iksgcp")
CACHE_TTL = int(os.getenv("FINOPS_CACHE_TTL", "300"))

st.markdown("""
<style>
.stApp{background:#f5f7fb}
[data-testid="stHeader"]{background:rgba(245,247,251,.92)}
.block-container{padding-top:1.2rem;padding-bottom:2rem;max-width:1700px}
[data-testid="stMetric"]{background:#fff;border:1px solid #e6eaf2;padding:16px 18px;border-radius:14px;box-shadow:0 2px 8px rgba(18,38,63,.035)}
[data-testid="stMetricLabel"]{color:#65718a}
[data-testid="stMetricValue"]{color:#152238}
.hero{padding:24px 26px;border-radius:18px;color:white;background:linear-gradient(115deg,#101d3b 0%,#183d68 55%,#187c91 100%);margin-bottom:18px}
.hero h1{margin:0;font-size:2rem;letter-spacing:-.03em}
.hero p{margin:7px 0 0;color:#d8e8f7;font-size:.98rem}
.muted{color:#68758b;font-size:.86rem}
.section-head{font-size:1.12rem;font-weight:750;color:#17243b;margin:5px 0 10px}
</style>
""", unsafe_allow_html=True)


def money(value, currency="INR"):
    try:
        value = float(value or 0)
    except (ValueError, TypeError):
        value = 0.0
    symbol = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(str(currency).upper(), f"{currency} ")
    if abs(value) >= 1_000_000:
        return f"{symbol}{value / 1_000_000:,.2f}M"
    if abs(value) >= 10_000:
        return f"{symbol}{value / 1_000:,.1f}K"
    return f"{symbol}{value:,.2f}"


@st.cache_resource
def get_client():
    return bigquery.Client(project=PROJECT_ID)


@st.cache_resource
def get_monitoring_client():
    return monitoring_v3.MetricServiceClient()


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
      COALESCE(MAX((SELECT lbl.value FROM UNNEST(labels) lbl WHERE LOWER(lbl.key) IN ('owner','owned_by','team_owner','resource_owner') LIMIT 1)), 'Unassigned') AS owner,
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
      COALESCE(project.id,'unknown-project-id') AS project_id,
      COALESCE(location.region,'Unspecified region') AS region,
      COALESCE(location.zone,'') AS zone,
      COALESCE(sku.description,'Unspecified SKU') AS sku,
      COALESCE(NULLIF(resource.name,''),NULLIF(resource.global_name,''),'Resource not reported') AS resource,
      COALESCE(MAX((SELECT lbl.value FROM UNNEST(labels) lbl WHERE LOWER(lbl.key) IN ('owner','owned_by','team_owner','resource_owner') LIMIT 1)), 'Unassigned') AS owner,
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
    GROUP BY currency,service,project,project_id,region,zone,sku,resource
    ORDER BY net_cost DESC
    LIMIT 2000
    """
    return run_query(sql, query_params(start_day,end_day,services,projects,regions,skus,currency,resource_search), "resources")


def style_chart(fig, height=360):
    fig.update_layout(height=height, margin=dict(l=10,r=10,t=48,b=10), paper_bgcolor="white",
                      plot_bgcolor="white", font=dict(family="Arial",color="#34435b"),
                      legend=dict(orientation="h",yanchor="bottom",y=1.02,xanchor="left",x=0),
                      hoverlabel=dict(bgcolor="white"))
    fig.update_xaxes(showgrid=False,zeroline=False)
    fig.update_yaxes(gridcolor="#edf1f6",zeroline=False)
    return fig


def extract_instance_details(resource_text):
    text = str(resource_text or "")
    instance_segment = re.search(r"instances/([A-Za-z0-9_.-]+)", text)
    instance_id_match = re.search(r"instances/(\d+)", text)
    zone_match = re.search(r"/zones/([A-Za-z0-9-]+)", text)
    instance_name = instance_segment.group(1) if instance_segment else ""
    instance_id = instance_id_match.group(1) if instance_id_match else ""
    zone = zone_match.group(1) if zone_match else ""
    if instance_name.isdigit() and not instance_id:
        instance_id = instance_name
        instance_name = ""
    return pd.Series([instance_id, instance_name, zone], index=["instance_id", "instance_name", "zone_from_resource"])


def monitoring_window(start_day, end_day):
    start_ts = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
    end_ts = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return start_ts, end_ts


def build_time_interval(start_ts, end_ts):
    return TimeInterval(
        start_time={"seconds": int(start_ts.timestamp())},
        end_time={"seconds": int(end_ts.timestamp())},
    )


def metric_value_to_float(point):
    value = point.value
    if value.double_value is not None:
        return float(value.double_value)
    if value.int64_value is not None:
        return float(value.int64_value)
    return None


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_gce_utilization(project_id, start_day, end_day):
    client = get_monitoring_client()
    start_ts, end_ts = monitoring_window(start_day, end_day)
    interval = build_time_interval(start_ts, end_ts)
    project_name = f"projects/{project_id}"

    def fetch_metric(metric_type):
        request = ListTimeSeriesRequest(
            name=project_name,
            filter=f'metric.type = "{metric_type}" AND resource.type = "gce_instance"',
            interval=interval,
            view=ListTimeSeriesRequest.TimeSeriesView.FULL,
            aggregation=Aggregation(
                alignment_period={"seconds": 3600},
                per_series_aligner=Aggregation.Aligner.ALIGN_MEAN,
            ),
        )
        rows = []
        series_iter = client.list_time_series(request=request)
        for series in series_iter:
            labels = dict(series.resource.labels)
            metric_labels = dict(series.metric.labels)
            vals = [metric_value_to_float(p) for p in series.points]
            vals = [v for v in vals if v is not None]
            if not vals:
                continue
            rows.append(
                {
                    "project_id": labels.get("project_id", ""),
                    "instance_id": labels.get("instance_id", ""),
                    "zone": labels.get("zone", ""),
                    "instance_name": metric_labels.get("instance_name", ""),
                    "value_avg": float(sum(vals) / len(vals)),
                }
            )
        return pd.DataFrame(rows)

    cpu_df = fetch_metric("compute.googleapis.com/instance/cpu/utilization")
    if not cpu_df.empty:
        cpu_df["cpu_utilization_pct"] = cpu_df["value_avg"] * 100.0
        cpu_df = cpu_df.drop(columns=["value_avg"])
    mem_df = fetch_metric("agent.googleapis.com/memory/percent_used")
    if not mem_df.empty:
        mem_df["memory_utilization_pct"] = mem_df["value_avg"]
        mem_df = mem_df.drop(columns=["value_avg"])

    if cpu_df.empty and mem_df.empty:
        return pd.DataFrame(columns=["project_id", "instance_id", "zone", "instance_name", "cpu_utilization_pct", "memory_utilization_pct"])

    if cpu_df.empty:
        merged = mem_df
        merged["cpu_utilization_pct"] = pd.NA
    elif mem_df.empty:
        merged = cpu_df
        merged["memory_utilization_pct"] = pd.NA
    else:
        merged = pd.merge(cpu_df, mem_df, on=["project_id", "instance_id", "zone", "instance_name"], how="outer")
    return merged


def merge_cost_and_utilization(resource_df, util_df):
    enriched = resource_df.copy()
    parsed = enriched["resource"].apply(extract_instance_details)
    enriched = pd.concat([enriched, parsed], axis=1)
    enriched["cpu_utilization_pct"] = pd.NA
    enriched["memory_utilization_pct"] = pd.NA
    enriched["util_source"] = "Not mapped"

    if util_df.empty:
        return enriched

    util = util_df.copy()
    util["instance_id"] = util["instance_id"].astype(str)
    util["instance_name"] = util["instance_name"].astype(str)

    joined = pd.merge(enriched, util, on=["project_id", "instance_id"], how="left", suffixes=("", "_util"))
    id_match = joined["cpu_utilization_pct_util"].notna() | joined["memory_utilization_pct_util"].notna()
    joined.loc[id_match, "cpu_utilization_pct"] = joined.loc[id_match, "cpu_utilization_pct_util"]
    joined.loc[id_match, "memory_utilization_pct"] = joined.loc[id_match, "memory_utilization_pct_util"]
    joined.loc[id_match, "util_source"] = "gce_instance:instance_id"

    if "cpu_utilization_pct_util" in joined.columns:
        joined = joined.drop(columns=["cpu_utilization_pct_util", "memory_utilization_pct_util", "zone_util"], errors="ignore")
    return joined


def apply_underutilization_flags(df, cpu_threshold, memory_threshold, high_cost_percentile):
    flagged = df.copy()
    flagged["net_cost"] = pd.to_numeric(flagged["net_cost"], errors="coerce").fillna(0.0)
    flagged["cpu_utilization_pct"] = pd.to_numeric(flagged["cpu_utilization_pct"], errors="coerce")
    flagged["memory_utilization_pct"] = pd.to_numeric(flagged["memory_utilization_pct"], errors="coerce")
    high_cost_cutoff = flagged["net_cost"].quantile(high_cost_percentile) if not flagged.empty else 0.0
    flagged["is_high_cost"] = flagged["net_cost"] >= high_cost_cutoff
    flagged["is_cpu_low"] = flagged["cpu_utilization_pct"].notna() & (flagged["cpu_utilization_pct"] < cpu_threshold)
    flagged["is_mem_low"] = flagged["memory_utilization_pct"].notna() & (flagged["memory_utilization_pct"] < memory_threshold)
    flagged["util_coverage"] = flagged["cpu_utilization_pct"].notna() | flagged["memory_utilization_pct"].notna()
    flagged["underutilized"] = flagged["is_high_cost"] & (flagged["is_cpu_low"] | flagged["is_mem_low"])
    flagged["severity"] = "Info"
    flagged.loc[flagged["underutilized"], "severity"] = "Medium waste risk"
    flagged.loc[flagged["is_high_cost"] & flagged["is_cpu_low"] & flagged["is_mem_low"], "severity"] = "High waste risk"
    flagged.loc[~flagged["util_coverage"], "severity"] = "No util data"
    return flagged


st.markdown("""<div class="hero"><h1>💠 GCP FinOps · Cost Intelligence</h1>
<p>Understand cloud spend by service, project, region, SKU, owner and resource — with utilization context for rightsizing.</p></div>""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("## 🎛️ Dashboard controls")
    st.caption("Filters apply to all views.")
    period = st.selectbox("Quick date range", ["Last 7 days","Last 30 days","Last 60 days","Last 90 days","Custom"], index=1)
    today = date.today()
    if period == "Custom":
        chosen = st.date_input("Billing usage dates", value=(today-timedelta(days=29),today), max_value=today)
        if isinstance(chosen,(tuple,list)) and len(chosen)==2:
            start_day,end_day = chosen
        else:
            start_day=end_day=chosen
    else:
        days = {"Last 7 days":7,"Last 30 days":30,"Last 60 days":60,"Last 90 days":90}[period]
        start_day,end_day = today-timedelta(days=days-1),today
    if start_day>end_day:
        st.error("Start date must be before end date.")
        st.stop()
    st.markdown("---")
    if st.button("↻ Refresh billing + utilization cache", use_container_width=True):
        get_filter_options.clear()
        load_summary_data.clear()
        load_resource_data.clear()
        load_gce_utilization.clear()
        st.session_state.pop("finops_resource_result",None)
        st.session_state.pop("finops_resource_signature",None)
        st.rerun()

try:
    with st.spinner("Loading filter options from BigQuery…"):
        options = get_filter_options(start_day,end_day)
except Exception as exc:
    st.error("Could not read billing-export filter options from BigQuery.")
    st.code(str(exc))
    st.info("Check credentials, BigQuery Job User permission, and Data Viewer access.")
    st.stop()

with st.sidebar:
    services = st.multiselect("Service",options["services"],placeholder="All services")
    projects = st.multiselect("Project",options["projects"],placeholder="All projects")
    regions = st.multiselect("Region",options["regions"],placeholder="All regions")
    skus = st.multiselect("SKU / meter",options["skus"],placeholder="All SKUs")
    currency_options = ["ALL"]+options["currencies"]
    currency = st.selectbox("Billing currency",currency_options,index=(currency_options.index("INR") if "INR" in currency_options else 0))
    resource_search = st.text_input("Resource name contains",placeholder="e.g. instance, bucket, database")
    st.caption(f"Source table: `{TABLE_ID}`")
    st.caption(f"Monitoring project: `{MONITORING_PROJECT}`")

try:
    with st.spinner("Loading aggregated billing summary from BigQuery…"):
        df = load_summary_data(start_day,end_day,services,projects,regions,skus,currency,resource_search)
except Exception as exc:
    st.error("Unable to load the billing summary.")
    st.code(str(exc))
    st.info("Check service-account credentials and BigQuery read permissions.")
    st.stop()

if df.empty:
    st.warning("No billing rows matched these filters and dates. Try a wider date range or clear some filters.")
    st.stop()

for col in ["gross_cost","credits","net_cost","usage_amount","billing_rows"]:
    df[col] = pd.to_numeric(df[col],errors="coerce").fillna(0)
df["usage_date"] = pd.to_datetime(df["usage_date"],errors="coerce")
latest_export = pd.to_datetime(df["latest_export_time"],errors="coerce",utc=True).max()
selected_currency = df["currency"].dropna().unique().tolist()
if len(selected_currency)>1:
    st.warning("Multiple currencies are included. Totals are not converted; select one currency for a meaningful total.")
cur_label = selected_currency[0] if len(selected_currency)==1 else "Mixed currency"

st.markdown(f"<div class='muted'>Usage period: <b>{start_day:%d %b %Y}</b> to <b>{end_day:%d %b %Y}</b> · Latest available billing export, not live metering</div>",unsafe_allow_html=True)
if pd.notna(latest_export):
    st.caption(f"Latest export timestamp: {latest_export.strftime('%d %b %Y, %H:%M UTC')} · Cached for {CACHE_TTL//60} minute(s).")

gross,credits_total,net = df["gross_cost"].sum(),df["credits"].sum(),df["net_cost"].sum()
row_count,services_count = int(df["billing_rows"].sum()),df["service"].nunique()
k1,k2,k3,k4,k5 = st.columns(5)
k1.metric("Net cost after credits",money(net,cur_label))
k2.metric("Gross cost",money(gross,cur_label))
k3.metric("Credits / adjustments",money(credits_total,cur_label))
k4.metric("Billing rows represented",f"{row_count:,}")
k5.metric("Services",f"{services_count:,}")

view = st.radio("Dashboard view",["📊 Overview","🧱 Resource explorer","⚙️ Utilization & underutilization","🧭 Cost breakdowns","⬇️ Data & export"],horizontal=True,label_visibility="collapsed")

if view=="📊 Overview":
    left,right = st.columns([1.65,1])
    daily = df.groupby(["usage_date","currency"],as_index=False)[["gross_cost","credits","net_cost"]].sum().sort_values("usage_date")
    with left:
        st.markdown("<div class='section-head'>Daily cost trend</div>",unsafe_allow_html=True)
        trend = daily.melt(id_vars=["usage_date","currency"],value_vars=["gross_cost","net_cost"],var_name="Cost type",value_name="Cost")
        trend["Cost type"] = trend["Cost type"].map({"gross_cost":"Gross cost","net_cost":"Net after credits"})
        fig = px.area(trend,x="usage_date",y="Cost",color="Cost type",title="Gross vs net spend",color_discrete_sequence=["#3678d4","#11a7a1"])
        st.plotly_chart(style_chart(fig,390),use_container_width=True)
    with right:
        st.markdown("<div class='section-head'>Spend by service</div>",unsafe_allow_html=True)
        by_service = df.groupby("service",as_index=False)["net_cost"].sum().sort_values("net_cost",ascending=False).head(10)
        fig = px.bar(by_service.sort_values("net_cost"),x="net_cost",y="service",orientation="h",title="Top 10 services",color="net_cost",color_continuous_scale=["#b7d8f4","#1457a6"])
        fig.update_layout(coloraxis_showscale=False,yaxis_title="")
        st.plotly_chart(style_chart(fig,390),use_container_width=True)
    c1,c2 = st.columns(2)
    with c1:
        st.markdown("<div class='section-head'>Cost by project</div>",unsafe_allow_html=True)
        projects_cost = df.groupby("project",as_index=False)["net_cost"].sum().sort_values("net_cost",ascending=False).head(12)
        fig = px.bar(projects_cost.sort_values("net_cost"),x="net_cost",y="project",orientation="h",title="Top projects",color_discrete_sequence=["#6c63d9"])
        fig.update_layout(yaxis_title="")
        st.plotly_chart(style_chart(fig,360),use_container_width=True)
    with c2:
        st.markdown("<div class='section-head'>Owner-wise cost</div>",unsafe_allow_html=True)
        owner_cost = df.groupby("owner",as_index=False)["net_cost"].sum().sort_values("net_cost",ascending=False).head(12)
        if owner_cost.empty:
            st.info("No owner labels were found in billing rows.")
        else:
            fig = px.bar(owner_cost.sort_values("net_cost"),x="net_cost",y="owner",orientation="h",title="Top owners by net cost",color_discrete_sequence=["#4d65c4"])
            fig.update_layout(yaxis_title="")
            st.plotly_chart(style_chart(fig,360),use_container_width=True)

elif view=="🧱 Resource explorer":
    st.markdown("<div class='section-head'>Resource-level cost explorer</div>",unsafe_allow_html=True)
    st.caption("Resource detail is queried only when requested. The BigQuery result is capped at 2,000 grouped combinations.")
    signature = (str(start_day),str(end_day),tuple(services),tuple(projects),tuple(regions),tuple(skus),currency,resource_search.strip())
    if st.button("Load resource details (up to 2,000 rows)",type="primary"):
        try:
            with st.spinner("Loading resource details from BigQuery…"):
                result = load_resource_data(start_day,end_day,services,projects,regions,skus,currency,resource_search)
            st.session_state["finops_resource_result"] = result
            st.session_state["finops_resource_signature"] = signature
        except Exception as exc:
            st.error("Unable to load resource details.")
            st.code(str(exc))
    if st.session_state.get("finops_resource_signature")==signature:
        resource_df = st.session_state["finops_resource_result"].copy()
        if resource_df.empty:
            st.info("No resource combinations matched the current filters.")
        else:
            for col in ["gross_cost","credits","net_cost","usage_amount","billing_rows"]:
                resource_df[col] = pd.to_numeric(resource_df[col],errors="coerce").fillna(0)
            top_n = st.select_slider("Rows to display",options=[25,50,100,250,500,1000,2000],value=100)
            st.dataframe(resource_df.head(top_n),use_container_width=True,hide_index=True,column_config={
                "gross_cost":st.column_config.NumberColumn("Gross cost",format="%.2f"),
                "credits":st.column_config.NumberColumn("Credits",format="%.2f"),
                "net_cost":st.column_config.NumberColumn("Net cost",format="%.2f"),
                "usage_amount":st.column_config.NumberColumn("Usage quantity",format="%.4f"),
                "billing_rows":st.column_config.NumberColumn("Billing rows",format="%d")})
            st.caption(f"Showing {min(top_n,len(resource_df)):,} of {len(resource_df):,} returned combinations.")
            st.download_button("⬇️ Download displayed resource report (CSV)",data=resource_df.head(top_n).to_csv(index=False).encode("utf-8-sig"),file_name=f"gcp_finops_resources_{start_day}_{end_day}.csv",mime="text/csv")
            st.download_button("⬇️ Download all returned resource rows (CSV)",data=resource_df.to_csv(index=False).encode("utf-8-sig"),file_name=f"gcp_finops_resources_all_{start_day}_{end_day}.csv",mime="text/csv")
    else:
        st.info("Click **Load resource details** to run the resource-level query for the current filters.")

elif view=="⚙️ Utilization & underutilization":
    st.markdown("<div class='section-head'>Utilization-aware rightsizing (iksgcp)</div>",unsafe_allow_html=True)
    st.caption("CPU and Memory are fetched from Cloud Monitoring. Memory requires Ops Agent on VMs.")
    cpu_threshold = st.slider("CPU underutilized threshold (%)", min_value=5, max_value=50, value=20, step=1)
    memory_threshold = st.slider("Memory underutilized threshold (%)", min_value=5, max_value=60, value=30, step=1)
    high_cost_percentile = st.slider("High-cost percentile cutoff", min_value=0.50, max_value=0.99, value=0.70, step=0.01)

    try:
        with st.spinner("Loading resource costs from BigQuery…"):
            resource_df = load_resource_data(start_day,end_day,services,projects,regions,skus,currency,resource_search)
        with st.spinner(f"Loading CPU/Memory metrics from Cloud Monitoring for `{MONITORING_PROJECT}`…"):
            util_df = load_gce_utilization(MONITORING_PROJECT, start_day, end_day)
    except Exception as exc:
        st.error("Could not load utilization data.")
        st.code(str(exc))
        st.info("Check service account monitoring.viewer access and metric availability in iksgcp.")
        st.stop()

    if resource_df.empty:
        st.info("No resource costs found for current filters.")
        st.stop()

    merged = merge_cost_and_utilization(resource_df, util_df)
    flagged = apply_underutilization_flags(merged, cpu_threshold, memory_threshold, high_cost_percentile)
    coverage = flagged["util_coverage"].mean() * 100 if not flagged.empty else 0.0
    under_cost = flagged.loc[flagged["underutilized"], "net_cost"].sum()
    total_cost = flagged["net_cost"].sum()
    under_rows = int(flagged["underutilized"].sum())

    m1,m2,m3,m4 = st.columns(4)
    m1.metric("Resources with util mapping",f"{coverage:.1f}%")
    m2.metric("Underutilized resources",f"{under_rows:,}")
    m3.metric("Underutilized net cost",money(under_cost,cur_label))
    m4.metric("Cost coverage in view",money(total_cost,cur_label))

    st.markdown("<div class='section-head'>High-cost underutilized resources</div>",unsafe_allow_html=True)
    focus = flagged[flagged["underutilized"]].sort_values("net_cost",ascending=False)
    if focus.empty:
        st.success("No high-cost underutilized resources found for selected thresholds.")
    else:
        show_cols = ["severity","owner","project","service","region","resource","net_cost","cpu_utilization_pct","memory_utilization_pct","util_source"]
        st.dataframe(focus[show_cols].head(500),use_container_width=True,hide_index=True,column_config={
            "net_cost":st.column_config.NumberColumn("Net cost",format="%.2f"),
            "cpu_utilization_pct":st.column_config.NumberColumn("CPU %",format="%.2f"),
            "memory_utilization_pct":st.column_config.NumberColumn("Memory %",format="%.2f"),
        })

    c1,c2 = st.columns(2)
    with c1:
        waste_owner = flagged[flagged["underutilized"]].groupby("owner",as_index=False)["net_cost"].sum().sort_values("net_cost",ascending=False).head(12)
        if waste_owner.empty:
            st.info("No owner-level underutilized cost to display.")
        else:
            fig = px.bar(waste_owner.sort_values("net_cost"),x="net_cost",y="owner",orientation="h",title="Underutilized cost by owner",color_discrete_sequence=["#cd5d5d"])
            fig.update_layout(yaxis_title="")
            st.plotly_chart(style_chart(fig,360),use_container_width=True)
    with c2:
        chart_df = flagged[flagged["util_coverage"]].copy()
        if chart_df.empty:
            st.info("No mapped utilization data for cost-vs-utilization chart.")
        else:
            chart_df["util_score"] = chart_df[["cpu_utilization_pct","memory_utilization_pct"]].mean(axis=1,skipna=True)
            fig = px.scatter(chart_df.head(400),x="util_score",y="net_cost",color="severity",
                             hover_data=["owner","project","resource","cpu_utilization_pct","memory_utilization_pct"],
                             title="Cost vs utilization",
                             color_discrete_map={"High waste risk":"#d94848","Medium waste risk":"#ec9f3c","Info":"#4b79d8","No util data":"#8b95a8"})
            fig.update_xaxes(title="Average utilization (%)")
            fig.update_yaxes(title="Net cost")
            st.plotly_chart(style_chart(fig,360),use_container_width=True)

    export_cols = ["owner","project","project_id","service","region","resource","net_cost",
                   "cpu_utilization_pct","memory_utilization_pct","severity","underutilized","util_source"]
    st.download_button("⬇️ Download utilization-mapped report (CSV)",
                       data=flagged[export_cols].to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"gcp_finops_utilization_{start_day}_{end_day}.csv",mime="text/csv")

elif view=="🧭 Cost breakdowns":
    b1,b2 = st.columns(2)
    with b1:
        st.markdown("<div class='section-head'>Region cost distribution</div>",unsafe_allow_html=True)
        region_cost = df.groupby("region",as_index=False)["net_cost"].sum().nlargest(20,"net_cost")
        fig = px.bar(region_cost.sort_values("net_cost"),x="net_cost",y="region",orientation="h",title="Net cost by region",color_discrete_sequence=["#168c86"])
        fig.update_layout(yaxis_title="")
        st.plotly_chart(style_chart(fig,420),use_container_width=True)
    with b2:
        st.markdown("<div class='section-head'>SKU / billing meter</div>",unsafe_allow_html=True)
        sku_cost = df.groupby("sku",as_index=False)["net_cost"].sum().nlargest(20,"net_cost")
        fig = px.bar(sku_cost.sort_values("net_cost"),x="net_cost",y="sku",orientation="h",title="Top 20 SKUs",color_discrete_sequence=["#6c63d9"])
        fig.update_layout(yaxis_title="")
        st.plotly_chart(style_chart(fig,420),use_container_width=True)
    st.markdown("<div class='section-head'>Service × project matrix</div>",unsafe_allow_html=True)
    heat = df.groupby(["service","project"],as_index=False)["net_cost"].sum()
    top_services = df.groupby("service")["net_cost"].sum().nlargest(12).index
    top_projects = df.groupby("project")["net_cost"].sum().nlargest(12).index
    heat = heat[heat["service"].isin(top_services)&heat["project"].isin(top_projects)]
    if not heat.empty:
        pivot = heat.pivot(index="service",columns="project",values="net_cost").fillna(0)
        fig = px.imshow(pivot,aspect="auto",color_continuous_scale="Blues",labels=dict(x="Project",y="Service",color="Net cost"),title="Top services and projects")
        st.plotly_chart(style_chart(fig,480),use_container_width=True)

else:
    st.markdown("<div class='section-head'>Aggregated billing data</div>",unsafe_allow_html=True)
    st.caption("Summary is grouped by usage date, service, project, region and SKU. Amounts remain in the source currency.")
    st.dataframe(df.sort_values("net_cost",ascending=False).head(20000),use_container_width=True,hide_index=True)
    st.caption("The table displays at most 20,000 summary rows.")
    st.download_button("⬇️ Download summary data (CSV)",data=df.to_csv(index=False).encode("utf-8-sig"),file_name=f"gcp_finops_summary_{start_day}_{end_day}.csv",mime="text/csv")
    daily_export = df.groupby(["usage_date","currency"],as_index=False)[["gross_cost","credits","net_cost"]].sum().sort_values("usage_date")
    st.download_button("⬇️ Download daily trend (CSV)",data=daily_export.to_csv(index=False).encode("utf-8-sig"),file_name=f"gcp_finops_daily_cost_{start_day}_{end_day}.csv",mime="text/csv")

st.markdown("---")
st.markdown("<div class='muted'>FinOps dashboard · Read-only billing analytics · Costs reflect the latest rows available in the configured Cloud Billing export. Export delays, credits and adjustments can change historical totals.</div>",unsafe_allow_html=True)
