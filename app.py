"""GCP FinOps dashboard: cached summary view and on-demand resource details."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone
import json
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery, monitoring_v3
from google.oauth2 import service_account

from rightsizing import Params, STATUS_ORDER, build_recommendations

st.set_page_config(page_title="GCP FinOps | Cost Intelligence", page_icon="💠", layout="wide")
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "iks-finops")
TABLE_ID = os.getenv("GCP_BILLING_TABLE", "iks-finops.GCP_Finops_billing.gcp_billing_export_resource_v1_019EFB_4492BC_7B3DE6")
CACHE_TTL = int(os.getenv("FINOPS_CACHE_TTL", "300"))
# CPU/RAM (Cloud Monitoring) uses a separate service account key and a fixed list of projects.
MONITORING_KEY_FILE = os.getenv("GCP_MONITORING_KEY_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "iksgcp.json"))
MONITORING_PROJECTS = [p.strip() for p in os.getenv("GCP_MONITORING_PROJECTS", "iksgcp").split(",") if p.strip()]

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
    """Current Compute Engine instances (machine type, status, created) for each project."""
    cols = ["project", "instance_id", "instance_name", "zone", "machine_type", "status", "created", "gpus"]
    rows, errors = [], []
    session = AuthorizedSession(get_monitoring_credentials())
    for project in projects:
        token = None
        try:
            while True:
                params = {"maxResults": 500, "fields": "items/*/instances(id,name,machineType,status,creationTimestamp,guestAccelerators),nextPageToken"}
                if token:
                    params["pageToken"] = token
                resp = session.get(f"https://compute.googleapis.com/compute/v1/projects/{project}/aggregated/instances", params=params, timeout=60)
                resp.raise_for_status()
                body = resp.json()
                for zone_key, scoped in body.get("items", {}).items():
                    for inst in scoped.get("instances", []):
                        gpus = sum(int(a.get("acceleratorCount", 0)) for a in inst.get("guestAccelerators", []))
                        rows.append((project, str(inst["id"]), inst["name"], zone_key.split("/")[-1],
                                     inst["machineType"].split("/")[-1], inst["status"],
                                     pd.to_datetime(inst.get("creationTimestamp"), utc=True, errors="coerce"), gpus))
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


def style_chart(fig, height=360):
    fig.update_layout(height=height, margin=dict(l=10,r=10,t=48,b=10), paper_bgcolor="white",
                      plot_bgcolor="white", font=dict(family="Arial",color="#34435b"),
                      legend=dict(orientation="h",yanchor="bottom",y=1.02,xanchor="left",x=0),
                      hoverlabel=dict(bgcolor="white"))
    fig.update_xaxes(showgrid=False,zeroline=False)
    fig.update_yaxes(gridcolor="#edf1f6",zeroline=False)
    return fig


st.markdown("""<div class="hero"><h1>💠 GCP FinOps · Cost Intelligence</h1>
<p>Understand cloud spend by service, project, region, SKU and resource — using the latest available billing export.</p></div>""", unsafe_allow_html=True)

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
    if st.button("↻ Refresh billing data", use_container_width=True):
        get_filter_options.clear()
        load_summary_data.clear()
        load_resource_data.clear()
        load_vm_inventory.clear()
        load_vm_metrics.clear()
        load_vm_costs.clear()
        load_machine_type_catalog.clear()
        st.session_state.pop("finops_resource_result",None)
        st.session_state.pop("finops_resource_signature",None)
        st.session_state.pop("finops_vm_data",None)
        st.session_state.pop("finops_vm_signature",None)
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

view = st.radio("Dashboard view",["📊 Overview","🧱 Resource explorer","🧭 Cost breakdowns","📈 CPU & RAM","⬇️ Data & export"],horizontal=True,label_visibility="collapsed")

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
        st.markdown("<div class='section-head'>Credits by service</div>",unsafe_allow_html=True)
        credit_service = df.groupby("service",as_index=False)["credits"].sum()
        credit_service = credit_service[credit_service["credits"]!=0].sort_values("credits").head(12)
        if credit_service.empty:
            st.info("No credits were recorded for these filters and dates.")
        else:
            fig = px.bar(credit_service,x="credits",y="service",orientation="h",title="Largest credit amounts",color_discrete_sequence=["#e09a37"])
            fig.update_layout(yaxis_title="")
            st.plotly_chart(style_chart(fig,360),use_container_width=True)

elif view=="🧱 Resource explorer":
    st.markdown("<div class='section-head'>Resource-level cost explorer</div>",unsafe_allow_html=True)
    st.caption("Resource detail is queried only when requested. The BigQuery result is capped at 1,000 grouped combinations.")
    signature = (str(start_day),str(end_day),tuple(services),tuple(projects),tuple(regions),tuple(skus),currency,resource_search.strip())
    if st.button("Load resource details (up to 1,000 rows)",type="primary"):
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
            top_n = st.select_slider("Rows to display",options=[25,50,100,250,500,1000],value=100)
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

elif view=="📈 CPU & RAM":
    STATUS_COLORS = {"Idle":"#7f8fa9","Over-provisioned":"#e09a37","Right-sized":"#168c86","Under-provisioned":"#d64545",
                     "Insufficient data":"#b7bfce","Not evaluated":"#d5dbe6"}
    st.markdown("<div class='section-head'>VM utilization & right-sizing</div>",unsafe_allow_html=True)
    st.caption(f"Running Compute Engine VMs in **{', '.join(MONITORING_PROJECTS)}** · CPU/RAM from Cloud Monitoring, machine sizes from Compute Engine, "
               f"cost from the billing export · authenticated with `{os.path.basename(MONITORING_KEY_FILE)}`. Uses the sidebar date range (metrics are kept ~6 weeks).")
    with st.expander("Right-sizing assumptions (change anytime, no reload needed)",expanded=False):
        a1,a2,a3 = st.columns(3)
        target_cpu = a1.slider("Target CPU at peak (%)",30,85,60,help="After resizing, the p95 hourly peak CPU should land near this value.")
        target_mem = a2.slider("Target RAM at peak (%)",40,90,70,help="After resizing, the p95 hourly peak RAM should land near this value.")
        min_saving = a3.slider("Minimum saving to recommend (%)",0,50,10)
        st.caption("Demand = p95 of hourly peak utilization over the date range. Needed size = current size × p95 ÷ target. "
                   "The cheapest same-series machine type (or a resized custom type for custom VMs) that fits both CPU and RAM is recommended. "
                   "VMs without RAM data (no Ops Agent) keep their RAM and only shrink vCPUs. Saving = the VM's billed compute cost × estimated price reduction "
                   "(vCPU ≈ 7.5× the price of 1 GB RAM). Treat savings as estimates; committed-use discounts may reduce what you actually save.")
    projects_t = tuple(MONITORING_PROJECTS)
    vm_signature = (str(start_day),str(end_day),projects_t,currency)
    if st.button("Load VM utilization & right-sizing",type="primary"):
        try:
            with st.spinner("Loading VM inventory, utilization and billing cost…"):
                inventory,inv_errors = load_vm_inventory(projects_t)
                util,util_errors = load_vm_metrics(projects_t,start_day,end_day)
                zones = tuple(sorted(inventory.loc[inventory["status"]=="RUNNING","zone"].unique()))
                catalogs,cat_errors = load_machine_type_catalog(projects_t[0],zones) if zones else ({},[])
                cost_errors = []
                try:
                    vm_costs = load_vm_costs(projects_t,start_day,end_day,currency)
                except Exception as exc:
                    vm_costs = pd.DataFrame(columns=["instance_id","currency","net_cost"])
                    cost_errors.append(f"billing cost lookup failed: {str(exc).splitlines()[0][:200]}")
            st.session_state["finops_vm_data"] = dict(inventory=inventory,util=util,catalogs=catalogs,costs=vm_costs,
                                                      errors=list(dict.fromkeys(inv_errors+util_errors+cat_errors+cost_errors)))
            st.session_state["finops_vm_signature"] = vm_signature
        except Exception as exc:
            st.error("Unable to load VM data.")
            st.code(str(exc))
    if st.session_state.get("finops_vm_signature")!=vm_signature:
        st.info("Click **Load VM utilization & right-sizing**. It reads the VM list, hourly CPU/RAM metrics and billed cost for the selected dates.")
    else:
        vm_data = st.session_state["finops_vm_data"]
        if vm_data["errors"]:
            with st.expander(f"⚠️ {len(vm_data['errors'])} request(s) failed",expanded=False):
                st.caption("Typically a missing Compute Viewer / Monitoring Viewer role, or an API that is disabled in the project.")
                for message in vm_data["errors"]:
                    st.code(message)
        window_start = datetime.combine(start_day,dtime.min,tzinfo=timezone.utc)
        window_end = min(datetime.combine(end_day+timedelta(days=1),dtime.min,tzinfo=timezone.utc),datetime.now(timezone.utc))
        recs = build_recommendations(vm_data["inventory"],vm_data["util"],vm_data["costs"],vm_data["catalogs"],window_start,window_end,
                                     Params(target_cpu=target_cpu,target_mem=target_mem,min_saving_pct=min_saving))
        if recs.empty:
            st.warning("No running VMs were found for the configured project(s).")
        else:
            vm_cur = vm_data["costs"]["currency"].iloc[0] if not vm_data["costs"].empty else cur_label
            evaluated = recs[recs["status"].isin(["Idle","Over-provisioned","Right-sized","Under-provisioned"])]
            down = recs[recs["status"].isin(["Idle","Over-provisioned"])]
            up = recs[recs["status"]=="Under-provisioned"]
            savings = float(down["monthly_saving"].sum(skipna=True))
            extra_cost = float(-up["monthly_saving"].sum(skipna=True))
            base_cost = float(evaluated["monthly_cost"].sum(skipna=True))
            terminated = int((vm_data["inventory"]["status"]!="RUNNING").sum())

            k1,k2,k3,k4,k5,k6 = st.columns(6)
            k1.metric("Running VMs",f"{len(recs):,}",help=f"{terminated} stopped/terminated VMs are excluded.")
            k2.metric("Downsize candidates",f"{len(down):,}")
            k3.metric("Under-provisioned",f"{len(up):,}")
            k4.metric("Est. monthly savings",money(savings,vm_cur),delta=(f"{savings/base_cost:.0%} of VM compute cost" if base_cost>0 else None),delta_color="off")
            k5.metric("Extra cost to fix under-provisioned",money(extra_cost,vm_cur))
            k6.metric("vCPUs now → recommended",f"{int(evaluated['vcpus'].sum()):,} → {int(evaluated['rec_vcpus'].sum()):,}")

            tab_rec,tab_fleet,tab_vm = st.tabs(["🎯 Recommendations","📊 Fleet utilization","🔍 VM drill-down"])

            with tab_rec:
                r1,r2 = st.columns([1.5,1])
                with r1:
                    scatter = recs[recs["cpu_p95"].notna() & recs["mem_p95"].notna()].copy()
                    if scatter.empty:
                        st.info("No VM reports RAM (Ops Agent not installed), so the CPU-vs-RAM map cannot be drawn.")
                    else:
                        fallback = scatter["monthly_cost"].median()
                        scatter["bubble"] = scatter["monthly_cost"].fillna(fallback if pd.notna(fallback) else 1).clip(lower=1)
                        fig = px.scatter(scatter,x="cpu_p95",y="mem_p95",size="bubble",color="status",color_discrete_map=STATUS_COLORS,size_max=38,
                                         hover_name="instance_name",hover_data={"machine_type":True,"recommended_type":True,"cpu_p95":":.0f","mem_p95":":.0f","bubble":False},
                                         labels={"cpu_p95":"CPU p95 of hourly peaks (%)","mem_p95":"RAM p95 of hourly peaks (%)"},
                                         title="CPU vs RAM demand (bubble size = monthly cost)")
                        fig.add_vline(x=target_cpu,line_dash="dash",line_color="#9aa6bb")
                        fig.add_hline(y=target_mem,line_dash="dash",line_color="#9aa6bb")
                        fig.update_xaxes(range=[0,105]); fig.update_yaxes(range=[0,105])
                        st.plotly_chart(style_chart(fig,430),use_container_width=True)
                        st.caption(f"Bottom-left of the dashed lines = oversized in both CPU and RAM. Only {len(scatter)} of {len(recs)} VMs report RAM.")
                with r2:
                    by_status = recs.groupby("status").agg(vms=("instance_id","count"),cost=("monthly_cost","sum")).reindex(STATUS_ORDER).dropna(subset=["vms"]).reset_index()
                    fig = px.bar(by_status,x="status",y="vms",color="status",color_discrete_map=STATUS_COLORS,text="vms",title="VMs by right-sizing status",
                                 hover_data={"cost":":,.0f"})
                    fig.update_layout(showlegend=False,xaxis_title="",yaxis_title="VMs")
                    st.plotly_chart(style_chart(fig,430),use_container_width=True)

                s1,s2 = st.columns(2)
                with s1:
                    top = down.dropna(subset=["monthly_saving"]).nlargest(15,"monthly_saving").sort_values("monthly_saving")
                    if top.empty:
                        st.info("No downsize savings to show (no candidates, or no billing cost matched these VMs).")
                    else:
                        top = top.assign(move=top["machine_type"]+" → "+top["recommended_type"].astype(str),label=top["monthly_saving"].map(lambda v: money(v,vm_cur)))
                        fig = px.bar(top,x="monthly_saving",y="instance_name",orientation="h",color="status",color_discrete_map=STATUS_COLORS,text="label",
                                     hover_data={"move":True,"monthly_cost":":,.0f","monthly_saving":":,.0f","label":False},title="Top 15 monthly savings by VM")
                        fig.update_layout(yaxis_title="",xaxis_title=f"Estimated monthly saving ({vm_cur})")
                        st.plotly_chart(style_chart(fig,470),use_container_width=True)
                with s2:
                    cap = evaluated.groupby("series",as_index=False).agg(vcpu_now=("vcpus","sum"),vcpu_rec=("rec_vcpus","sum"),ram_now=("mem_gb","sum"),ram_rec=("rec_mem_gb","sum"))
                    fig = make_subplots(rows=1,cols=2,subplot_titles=("vCPUs: current vs recommended","RAM (GB): current vs recommended"))
                    fig.add_bar(x=cap["series"],y=cap["vcpu_now"],name="Current",marker_color="#3678d4",legendgroup="now",row=1,col=1)
                    fig.add_bar(x=cap["series"],y=cap["vcpu_rec"],name="Recommended",marker_color="#11a7a1",legendgroup="rec",row=1,col=1)
                    fig.add_bar(x=cap["series"],y=cap["ram_now"],name="Current",marker_color="#3678d4",legendgroup="now",showlegend=False,row=1,col=2)
                    fig.add_bar(x=cap["series"],y=cap["ram_rec"],name="Recommended",marker_color="#11a7a1",legendgroup="rec",showlegend=False,row=1,col=2)
                    fig.update_layout(barmode="group")
                    st.plotly_chart(style_chart(fig,470),use_container_width=True)

                st.markdown("<div class='section-head'>Right-sizing recommendations</div>",unsafe_allow_html=True)
                chosen = st.multiselect("Status",STATUS_ORDER,default=[s for s in STATUS_ORDER if s in set(recs["status"])],key="finops_vm_status_filter")
                table = recs[recs["status"].isin(chosen)].sort_values("monthly_saving",ascending=False,na_position="last")
                table = table[["instance_name","zone","status","machine_type","vcpus","mem_gb","cpu_avg","cpu_p95","mem_avg","mem_p95",
                               "recommended_type","rec_vcpus","rec_mem_gb","monthly_cost","monthly_saving","confidence","reason","instance_id"]]
                st.dataframe(table,use_container_width=True,hide_index=True,column_config={
                    "instance_name":"VM","machine_type":"Current type","vcpus":st.column_config.NumberColumn("vCPUs",format="%d"),
                    "mem_gb":st.column_config.NumberColumn("RAM (GB)",format="%.1f"),
                    "cpu_avg":st.column_config.NumberColumn("CPU avg %",format="%.1f"),"cpu_p95":st.column_config.NumberColumn("CPU p95 %",format="%.1f"),
                    "mem_avg":st.column_config.NumberColumn("RAM avg %",format="%.1f"),"mem_p95":st.column_config.NumberColumn("RAM p95 %",format="%.1f"),
                    "recommended_type":"Recommended type","rec_vcpus":st.column_config.NumberColumn("Rec. vCPUs",format="%d"),
                    "rec_mem_gb":st.column_config.NumberColumn("Rec. RAM (GB)",format="%.1f"),
                    "monthly_cost":st.column_config.NumberColumn(f"Monthly cost ({vm_cur})",format="%.0f"),
                    "monthly_saving":st.column_config.NumberColumn(f"Est. monthly saving ({vm_cur})",format="%.0f"),
                    "confidence":"Confidence","reason":"Why","instance_id":"Instance ID"})
                st.caption("Negative savings on Under-provisioned VMs are the extra monthly cost of resizing them up. Verify with the application owner before changing a VM.")
                st.download_button("⬇️ Download right-sizing recommendations (CSV)",data=table.to_csv(index=False).encode("utf-8-sig"),file_name=f"gcp_finops_rightsizing_{start_day}_{end_day}.csv",mime="text/csv")

            with tab_fleet:
                util_fleet = vm_data["util"]
                util_fleet = util_fleet[util_fleet["instance_id"].isin(recs["instance_id"])]
                f1,f2 = st.columns([1.6,1])
                with f1:
                    trend = util_fleet.groupby("time",as_index=False)[["cpu_pct","mem_pct"]].mean().sort_values("time")
                    trend = trend.melt(id_vars="time",value_vars=["cpu_pct","mem_pct"],var_name="Metric",value_name="Utilization %").dropna()
                    trend["Metric"] = trend["Metric"].map({"cpu_pct":"CPU","mem_pct":"RAM (VMs with agent)"})
                    fig = px.line(trend,x="time",y="Utilization %",color="Metric",title="Average utilization across running VMs (hourly)",color_discrete_sequence=["#3678d4","#e09a37"])
                    fig.update_yaxes(range=[0,100])
                    st.plotly_chart(style_chart(fig,380),use_container_width=True)
                with f2:
                    fig = px.histogram(recs.dropna(subset=["cpu_p95"]),x="cpu_p95",color="status",color_discrete_map=STATUS_COLORS,nbins=20,
                                       title="How busy are the VMs? (CPU p95)",labels={"cpu_p95":"CPU p95 of hourly peaks (%)"})
                    fig.update_layout(yaxis_title="VMs")
                    st.plotly_chart(style_chart(fig,380),use_container_width=True)
                g1,g2 = st.columns(2)
                with g1:
                    top_cpu = recs.dropna(subset=["cpu_p95"]).nlargest(15,"cpu_p95").sort_values("cpu_p95")
                    fig = px.bar(top_cpu,x="cpu_p95",y="instance_name",orientation="h",color="status",color_discrete_map=STATUS_COLORS,
                                 title="Top 15 VMs by CPU (p95 of hourly peaks)",hover_data={"machine_type":True,"cpu_avg":":.1f"})
                    fig.update_layout(yaxis_title="",xaxis_title="CPU p95 %")
                    st.plotly_chart(style_chart(fig,440),use_container_width=True)
                with g2:
                    top_ram = recs.dropna(subset=["mem_p95"]).nlargest(15,"mem_p95").sort_values("mem_p95")
                    if top_ram.empty:
                        st.info("No VM reports RAM. Install the Ops Agent to see RAM utilization.")
                    else:
                        fig = px.bar(top_ram,x="mem_p95",y="instance_name",orientation="h",color="status",color_discrete_map=STATUS_COLORS,
                                     title="Top 15 VMs by RAM (p95 of hourly peaks)",hover_data={"machine_type":True,"mem_avg":":.1f"})
                        fig.update_layout(yaxis_title="",xaxis_title="RAM p95 %")
                        st.plotly_chart(style_chart(fig,440),use_container_width=True)
                by_series = recs.groupby("series",as_index=False).agg(CPU=("cpu_avg","mean"),RAM=("mem_avg","mean"),VMs=("instance_id","count"))
                by_series = by_series.melt(id_vars=["series","VMs"],value_vars=["CPU","RAM"],var_name="Metric",value_name="Avg utilization %").dropna()
                fig = px.bar(by_series,x="series",y="Avg utilization %",color="Metric",barmode="group",hover_data={"VMs":True},
                             title="Average utilization by machine series",color_discrete_sequence=["#3678d4","#e09a37"])
                fig.update_layout(xaxis_title="Machine series")
                st.plotly_chart(style_chart(fig,360),use_container_width=True)

            with tab_vm:
                ordered = recs.sort_values(["monthly_saving","instance_name"],ascending=[False,True],na_position="last")
                labels = {row["instance_id"]:f"{row['instance_name']} · {row['machine_type']} · {row['status']}" for _,row in ordered.iterrows()}
                picked = st.selectbox("Choose a VM",list(labels.keys()),format_func=lambda i: labels[i])
                vm = recs[recs["instance_id"]==picked].iloc[0]
                m1,m2,m3,m4,m5 = st.columns(5)
                m1.metric("Current type",vm["machine_type"])
                m2.metric("Provisioned",f"{vm['vcpus']:.0f} vCPU · {vm['mem_gb']:.1f} GB" if pd.notna(vm["vcpus"]) else "n/a")
                m3.metric("Status",vm["status"])
                m4.metric("Recommended",str(vm["recommended_type"]) if vm["recommended_type"] else "n/a")
                m5.metric("Est. monthly saving",money(vm["monthly_saving"],vm_cur) if pd.notna(vm["monthly_saving"]) else "n/a")
                st.caption(f"{vm['reason']} · confidence: {vm['confidence'] or 'n/a'} · hourly data points: {int(vm['points'])}")
                vm_util = vm_data["util"]
                vm_util = vm_util[vm_util["instance_id"]==picked].sort_values("time")
                d1,d2 = st.columns(2)
                for holder,mean_col,max_col,title,p95,target in ((d1,"cpu_pct","cpu_max_pct","CPU %",vm["cpu_p95"],target_cpu),(d2,"mem_pct","mem_max_pct","RAM %",vm["mem_p95"],target_mem)):
                    with holder:
                        if vm_util[mean_col].notna().any():
                            fig = go.Figure()
                            fig.add_trace(go.Scatter(x=vm_util["time"],y=vm_util[max_col],name="Hourly peak",line=dict(color="#d64545",width=1)))
                            fig.add_trace(go.Scatter(x=vm_util["time"],y=vm_util[mean_col],name="Hourly average",line=dict(color="#3678d4",width=2)))
                            fig.add_hline(y=target,line_dash="dash",line_color="#9aa6bb",annotation_text=f"target {target:.0f}%")
                            if pd.notna(p95):
                                fig.add_hline(y=p95,line_dash="dot",line_color="#e09a37",annotation_text=f"p95 {p95:.0f}%",annotation_position="bottom right")
                            fig.update_layout(title=f"{title} over time",yaxis_range=[0,105])
                            st.plotly_chart(style_chart(fig,360),use_container_width=True)
                        else:
                            st.info(f"No {title.split()[0]} data for this VM." + (" Install the Ops Agent to report RAM." if title.startswith("RAM") else ""))
                if pd.notna(vm["vcpus"]):
                    cap_fig = make_subplots(rows=1,cols=2,subplot_titles=("vCPUs","RAM (GB)"))
                    used_cpu = vm["vcpus"]*vm["cpu_p95"]/100 if pd.notna(vm["cpu_p95"]) else None
                    used_mem = vm["mem_gb"]*vm["mem_p95"]/100 if pd.notna(vm["mem_p95"]) else None
                    cap_fig.add_bar(x=["Provisioned","Used at p95 peak","Recommended"],y=[vm["vcpus"],used_cpu,vm["rec_vcpus"]],marker_color=["#3678d4","#e09a37","#11a7a1"],showlegend=False,row=1,col=1)
                    cap_fig.add_bar(x=["Provisioned","Used at p95 peak","Recommended"],y=[vm["mem_gb"],used_mem,vm["rec_mem_gb"]],marker_color=["#3678d4","#e09a37","#11a7a1"],showlegend=False,row=1,col=2)
                    cap_fig.update_layout(title="Capacity: provisioned vs used vs recommended")
                    st.plotly_chart(style_chart(cap_fig,340),use_container_width=True)

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
