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

from rightsizing import (LABEL_COLUMNS, LABEL_FIELDS, NOT_LABELED, Params, STATUS_ORDER,
                         build_recommendations, extract_labels, labels_text)
import billing_leadership as bl
import leadership as lead
import vm_charts as vc

st.set_page_config(page_title="GCP FinOps | Cost Intelligence", page_icon="💠", layout="wide")
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "iks-finops")
TABLE_ID = os.getenv("GCP_BILLING_TABLE", "iks-finops.GCP_Finops_billing.gcp_billing_export_resource_v1_019EFB_4492BC_7B3DE6")
CACHE_TTL = int(os.getenv("FINOPS_CACHE_TTL", "300"))
# CPU/RAM (Cloud Monitoring) uses a separate service account key and a fixed list of projects.
MONITORING_KEY_FILE = os.getenv("GCP_MONITORING_KEY_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "iksgcp.json"))
MONITORING_PROJECTS = [p.strip() for p in os.getenv("GCP_MONITORING_PROJECTS", "iksgcp").split(",") if p.strip()]
NO_ORG = ((), (), ())   # (business units, feature clusters, applications) chosen in the sidebar; empty = all

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


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_org_options(start_day, end_day):
    """Business unit / feature cluster / application combinations with spend in the dates (feeds the cascading sidebar filters)."""
    sql = f"""
    SELECT {bl.dim_sql(bl.BU)} AS business_unit, {bl.dim_sql(bl.CLUSTER)} AS feature_cluster, {bl.dim_sql(bl.APP)} AS application,
      SUM(cost) AS cost
    FROM `{TABLE_ID}`
    WHERE usage_start_time >= TIMESTAMP(@start_day)
      AND usage_start_time < TIMESTAMP(DATE_ADD(@end_day, INTERVAL 1 DAY))
    GROUP BY 1,2,3
    """
    params = [bigquery.ScalarQueryParameter("start_day", "DATE", start_day),
              bigquery.ScalarQueryParameter("end_day", "DATE", end_day)]
    out = run_query(sql, params, "org_filters")
    out["cost"] = pd.to_numeric(out["cost"], errors="coerce").fillna(0)
    return out.sort_values("cost", ascending=False).reset_index(drop=True)


def org_choices(combos, column, keep):
    """Options for one organisation filter, limited by the filters chosen above it. Plan values first, then by spend."""
    rows = combos
    for col, chosen in keep.items():
        if chosen:
            rows = rows[rows[col].isin(chosen)]
    values = list(dict.fromkeys(rows[column]))
    plan = {"business_unit": bl.MAPPING["bus"], "feature_cluster": bl.MAPPING["clusters"]}.get(column, [])
    tail = [v for v in (bl.UNMAPPED, bl.NOT_LABELED) if v in values]
    return [v for v in plan if v in values] + [v for v in values if v not in plan and v not in tail] + tail


def query_params(start_day, end_day, services, projects, regions, skus, currency, resource_search, org=NO_ORG):
    bus, clusters, apps = org
    return [
        bigquery.ScalarQueryParameter("start_day", "DATE", start_day),
        bigquery.ScalarQueryParameter("end_day", "DATE", end_day),
        bigquery.ArrayQueryParameter("services", "STRING", services),
        bigquery.ArrayQueryParameter("projects", "STRING", projects),
        bigquery.ArrayQueryParameter("regions", "STRING", regions),
        bigquery.ArrayQueryParameter("skus", "STRING", skus),
        bigquery.ScalarQueryParameter("currency", "STRING", currency),
        bigquery.ScalarQueryParameter("resource_search", "STRING", resource_search.strip()),
        bigquery.ArrayQueryParameter("bus", "STRING", list(bus)),
        bigquery.ArrayQueryParameter("clusters", "STRING", list(clusters)),
        bigquery.ArrayQueryParameter("apps", "STRING", list(apps)),
    ]


def filter_sql(start_param="start_day"):
    return f"""
    WHERE usage_start_time >= TIMESTAMP(@{start_param})
      AND usage_start_time < TIMESTAMP(DATE_ADD(@end_day, INTERVAL 1 DAY))
      AND (IFNULL(ARRAY_LENGTH(@services),0)=0 OR service.description IN UNNEST(@services))
      AND (IFNULL(ARRAY_LENGTH(@projects),0)=0 OR COALESCE(project.name,project.id,'Unspecified project') IN UNNEST(@projects))
      AND (IFNULL(ARRAY_LENGTH(@regions),0)=0 OR COALESCE(location.region,'Unspecified region') IN UNNEST(@regions))
      AND (IFNULL(ARRAY_LENGTH(@skus),0)=0 OR COALESCE(sku.description,'Unspecified SKU') IN UNNEST(@skus))
      AND (@currency='ALL' OR currency=@currency)
      AND (@resource_search='' OR STRPOS(LOWER(COALESCE(resource.name,resource.global_name,'')),LOWER(@resource_search))>0)
      AND (IFNULL(ARRAY_LENGTH(@bus),0)=0 OR {bl.dim_sql(bl.BU)} IN UNNEST(@bus))
      AND (IFNULL(ARRAY_LENGTH(@clusters),0)=0 OR {bl.dim_sql(bl.CLUSTER)} IN UNNEST(@clusters))
      AND (IFNULL(ARRAY_LENGTH(@apps),0)=0 OR {bl.dim_sql(bl.APP)} IN UNNEST(@apps))
    """


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_summary_data(start_day, end_day, services, projects, regions, skus, currency, resource_search, org=NO_ORG):
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
    return run_query(sql, query_params(start_day,end_day,services,projects,regions,skus,currency,resource_search,org), "summary")


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_resource_data(start_day, end_day, services, projects, regions, skus, currency, resource_search, org=NO_ORG):
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
    return run_query(sql, query_params(start_day,end_day,services,projects,regions,skus,currency,resource_search,org), "resources")


def previous_period_start(start_day, end_day):
    """First day of the period of the same length that ends the day before ``start_day``."""
    return start_day - timedelta(days=(end_day - start_day).days + 1)


def leadership_params(start_day, end_day, prev_start, services, projects, regions, skus, currency, resource_search, org=NO_ORG):
    return query_params(start_day, end_day, services, projects, regions, skus, currency, resource_search, org) + [
        bigquery.ScalarQueryParameter("prev_start", "DATE", prev_start)]


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_leadership_billing(start_day, end_day, prev_start, services, projects, regions, skus, currency, resource_search, org=NO_ORG):
    """Net cost for the selected and the previous period, grouped by every leadership dimension (labels, service, project)."""
    dims = ",\n      ".join(f"{bl.dim_sql(d)} AS {d}" for d in bl.ALL_DIMS)
    sql = f"""
    SELECT IF(usage_start_time >= TIMESTAMP(@start_day),'current','previous') AS period,
      IFNULL(currency,'UNKNOWN') AS currency,
      {dims},
      SUM(cost) AS gross_cost,
      SUM(cost+IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS net_cost
    FROM `{TABLE_ID}`
    {filter_sql("prev_start")}
    GROUP BY {",".join(str(i) for i in range(1, 3 + len(bl.ALL_DIMS)))}
    """
    return bl.prepare(run_query(sql, leadership_params(start_day, end_day, prev_start, services, projects, regions, skus, currency, resource_search, org), "leadership"))


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_leadership_daily(start_day, end_day, prev_start, services, projects, regions, skus, currency, resource_search, org=NO_ORG):
    sql = f"""
    SELECT IF(usage_start_time >= TIMESTAMP(@start_day),'current','previous') AS period,
      DATE(usage_start_time) AS usage_date,
      {bl.dim_sql(bl.BU)} AS {bl.BU},
      SUM(cost+IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS net_cost
    FROM `{TABLE_ID}`
    {filter_sql("prev_start")}
    GROUP BY 1,2,3
    """
    return bl.prepare_daily(run_query(sql, leadership_params(start_day, end_day, prev_start, services, projects, regions, skus, currency, resource_search, org), "leadership_daily"))


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_leadership_drill(dim, value, start_day, end_day, services, projects, regions, skus, currency, resource_search, org=NO_ORG):
    """Daily cost and the biggest resources behind one clicked bar. ``dim`` must be a known dimension (whitelisted by dim_sql)."""
    expr = bl.dim_sql(dim)
    params = query_params(start_day, end_day, services, projects, regions, skus, currency, resource_search, org) + [
        bigquery.ScalarQueryParameter("dim_value", "STRING", value)]
    where = f"{filter_sql()} AND ({expr}) = @dim_value"
    daily = run_query(f"""
    SELECT DATE(usage_start_time) AS usage_date, SUM(cost+IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS net_cost
    FROM `{TABLE_ID}` {where} GROUP BY 1""", params, "leadership_drill_daily")
    resources = run_query(f"""
    SELECT COALESCE(NULLIF(resource.name,''),NULLIF(resource.global_name,''),'Resource not reported') AS resource,
      COALESCE(service.description,'Unspecified service') AS service,
      COALESCE(project.name,project.id,'Unspecified project') AS project,
      SUM(cost+IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS net_cost
    FROM `{TABLE_ID}` {where} GROUP BY 1,2,3 ORDER BY net_cost DESC LIMIT 25""", params, "leadership_drill_resources")
    for frame in (daily, resources):
        frame["net_cost"] = pd.to_numeric(frame["net_cost"], errors="coerce").fillna(0)
    return daily, resources


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_resource_counts(start_day, end_day, services, projects, regions, skus, currency, resource_search, org=NO_ORG):
    """Billed resources and projects in the selected period, and how many resources roll up to a business unit.

    Transient resources (BigQuery jobs, Dataproc batches, Dataflow jobs: millions of IDs) and resources that cost less than 1
    are left out, so the count reflects the inventory leaders can act on."""
    res = "COALESCE(NULLIF(resource.global_name,''),NULLIF(resource.name,''))"
    sql = f"""
    WITH r AS (
      SELECT {res} AS res, ANY_VALUE(project.id) AS project, LOGICAL_OR({bl.unit_sql()}) AS mapped,
        SUM(cost+IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c),0)) AS net_cost
      FROM `{TABLE_ID}`
      {filter_sql()}
        AND {res} IS NOT NULL AND NOT REGEXP_CONTAINS({res}, r'/(jobs|batches)/')
      GROUP BY 1
      HAVING net_cost >= 1)
    SELECT COUNT(*) AS resources, COUNTIF(mapped) AS mapped, COUNT(DISTINCT project) AS projects FROM r
    """
    row = run_query(sql, query_params(start_day,end_day,services,projects,regions,skus,currency,resource_search,org), "resource_counts").iloc[0]
    return {k: int(row[k] or 0) for k in ("resources","mapped","projects")}


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


def load_vm_bundle(projects_t, start_day, end_day, currency):
    """Load VM inventory, hourly CPU/RAM, machine types and billed cost into session state.

    Shared by the CPU & RAM and Leadership views, so loading once serves both."""
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
        st.session_state["finops_vm_signature"] = (str(start_day),str(end_day),projects_t,currency)
    except Exception as exc:
        st.error("Unable to load VM data.")
        st.code(str(exc))


def render_lead_details(rows, kind, value, subtitle, vm_data, fmt_money):
    """Body of the details pop-up opened by clicking a leadership chart."""
    st.caption(subtitle)
    if rows.empty:
        st.info("No VMs found for this selection.")
        return
    if kind=="vm":
        v = rows.iloc[0]
        t = vc.prepare_table(rows).iloc[0]
        c1,c2,c3,c4 = st.columns(4)
        c1.markdown(vc.kpi_card("Current size",v["machine_type"],f"{v['vcpus']:.0f} vCPU · {v['mem_gb']:.1f} GB RAM",vc.CURRENT_COLOR),unsafe_allow_html=True)
        c2.markdown(vc.kpi_card("Status",v["status"],t["action"],vc.STATUS_COLORS.get(v["status"],vc.CURRENT_COLOR)),unsafe_allow_html=True)
        has_rec = pd.notna(v["rec_vcpus"])
        c3.markdown(vc.kpi_card("Recommended",str(v["recommended_type"]) if v["recommended_type"] else "—",
                                f"{v['rec_vcpus']:.0f} vCPU · {v['rec_mem_gb']:.1f} GB RAM" if has_rec else "",vc.RECOMMENDED_COLOR),unsafe_allow_html=True)
        saving = v["monthly_saving"]
        c4.markdown(vc.kpi_card("Est. monthly saving",fmt_money(saving) if pd.notna(saving) else "n/a",
                                f"VM costs {fmt_money(v['monthly_cost'])} / month" if pd.notna(v["monthly_cost"]) else "no billing match",
                                "#10B981" if (pd.isna(saving) or saving>=0) else "#EF4444"),unsafe_allow_html=True)
        st.caption(f"{v['reason']} · confidence: {v['confidence'] or 'n/a'}")
        g1,g2 = st.columns(2)
        fig = vc.gauge(v["cpu_p95"],"CPU · p95 of hourly peaks",60,85,vc.CPU_COLOR)
        if fig is not None:
            g1.plotly_chart(fig,use_container_width=True)
        fig = vc.gauge(v["mem_p95"],"RAM · p95 of hourly peaks",70,90,vc.RAM_COLOR)
        if fig is not None:
            g2.plotly_chart(fig,use_container_width=True)
        else:
            g2.info("No RAM data for this VM (Ops Agent not installed).")
        util = vm_data["util"]
        vm_util = util[util["instance_id"]==value].sort_values("time")
        t1,t2 = st.columns(2)
        if vm_util["cpu_pct"].notna().any():
            t1.plotly_chart(vc.vm_timeseries(vm_util,"cpu_pct","cpu_max_pct","CPU",vc.CPU_COLOR,v["cpu_p95"],60,85),use_container_width=True)
        if vm_util["mem_pct"].notna().any():
            t2.plotly_chart(vc.vm_timeseries(vm_util,"mem_pct","mem_max_pct","RAM",vc.RAM_COLOR,v["mem_p95"],70,90),use_container_width=True)
        if pd.notna(v["vcpus"]):
            st.plotly_chart(vc.vm_capacity(v),use_container_width=True)
        st.markdown("**Ownership (from VM labels)**")
        o_cols = st.columns(4)
        for index,(col,(title,_)) in enumerate(LABEL_FIELDS.items()):
            o_cols[index%4].markdown(f"<div class='muted'>{title}</div><b>{v[col]}</b>",unsafe_allow_html=True)
        if v["labels"]:
            with st.expander("All labels on this VM"):
                st.code(v["labels"].replace("; ","\n"))
        return
    # a group of VMs (status / client / owner / environment / ... / VMs missing a label)
    down = rows[rows["status"].isin(lead.DOWNSIZE)]
    under = rows[rows["status"]=="Under-provisioned"]
    c1,c2,c3,c4 = st.columns(4)
    c1.markdown(vc.kpi_card("VMs",f"{len(rows):,}","in this selection",vc.CPU_COLOR),unsafe_allow_html=True)
    c2.markdown(vc.kpi_card("Monthly compute cost",fmt_money(rows["monthly_cost"].sum(skipna=True)),"billed core + RAM + licences",vc.CURRENT_COLOR),unsafe_allow_html=True)
    c3.markdown(vc.kpi_card("Est. monthly saving",fmt_money(down["monthly_saving"].sum(skipna=True)),f"from downsizing {len(down)} VM{'s' if len(down)!=1 else ''}","#10B981"),unsafe_allow_html=True)
    c4.markdown(vc.kpi_card("Extra cost to fix hot VMs",fmt_money(-under["monthly_saving"].sum(skipna=True)),f"{len(under)} under-provisioned","#EF4444"),unsafe_allow_html=True)
    if kind!="unlabeled":
        fig,_lookup = lead.top_vm_savings(rows,fmt_money,n=10,clickable=False)
        if fig is not None:
            st.plotly_chart(fig,use_container_width=True)
        st.markdown("<div class='sec-title'>VMs in this selection</div>",unsafe_allow_html=True)
        styled,labels = lead.detail_table(rows)
        cfg = dict(labels)
        cfg.update(cpu_p95=st.column_config.NumberColumn("CPU p95 %",format="%.0f"),mem_p95=st.column_config.NumberColumn("RAM p95 %",format="%.0f"),
                   monthly_cost=st.column_config.NumberColumn("Monthly cost",format="%.0f"),monthly_saving=st.column_config.NumberColumn("Est. monthly saving",format="%.0f"))
        st.dataframe(styled,use_container_width=True,hide_index=True,column_config=cfg)
        csv_frame = rows.drop(columns=["labels"],errors="ignore")
    else:
        st.markdown("<div class='sec-title'>VMs missing this label</div>",unsafe_allow_html=True)
        cols = ["instance_name","zone","machine_type","status","owner","client","environment","monthly_cost","labels"]
        st.dataframe(rows[cols].sort_values("monthly_cost",ascending=False,na_position="last"),use_container_width=True,hide_index=True,
                     column_config={"instance_name":"VM","machine_type":"Type","monthly_cost":st.column_config.NumberColumn("Monthly cost",format="%.0f"),"labels":"Labels it already has"})
        csv_frame = rows[cols]
    st.download_button("⬇️ Download this list (CSV)",data=csv_frame.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"gcp_finops_leadership_{kind}_{str(value)[:30]}.csv",mime="text/csv",key=f"lead_dl_{kind}_{value}")


def render_bill_details(dim, value, billing, filters, fmt_money, start_day, end_day):
    """Body of the pop-up opened by clicking a billing leadership chart or action."""
    _title,subtitle = bl.describe(dim,value)
    st.caption(subtitle)
    rows = bl.select_rows(billing,dim,value)
    cost = float(rows.loc[rows["period"]=="current","net_cost"].sum())
    prev = float(rows.loc[rows["period"]=="previous","net_cost"].sum())
    if rows.empty or (cost==0 and prev==0):
        st.info("No cost found for this selection.")
        return
    total = float(billing.loc[billing["period"]=="current","net_cost"].sum())
    delta = cost-prev
    delta_pct = delta/prev if prev>0 else float("nan")
    services = bl.group_table(rows,"service")
    projects = bl.group_table(rows,"project")
    top = services.iloc[0] if len(services) else None
    c1,c2,c3,c4 = st.columns(4)
    c1.markdown(vc.kpi_card("Net cost",fmt_money(cost),f"{cost/total:.1%} of total spend" if total>0 else "",bl.BASE_COLOR),unsafe_allow_html=True)
    c2.markdown(vc.kpi_card("vs previous period",bl.arrow_text(delta,delta_pct,fmt_money) if prev>0 else "new",f"was {fmt_money(prev)}",
                            "#EF4444" if delta>0 else "#10B981"),unsafe_allow_html=True)
    c3.markdown(vc.kpi_card("Services · projects",f"{int((services['cost']>0).sum())} · {int((projects['cost']>0).sum())}","with cost in this selection","#8B5CF6"),unsafe_allow_html=True)
    c4.markdown(vc.kpi_card("Biggest service",str(top["value"]) if top is not None else "—",f"{top['share']:.0%} of this selection" if top is not None else "","#06B6D4"),unsafe_allow_html=True)
    if dim==bl.BU and value==bl.UNMAPPED:
        st.warning("**Action:** these features are not in the BU billing plan. Open the **By feature** tab, then add each feature to the right "
                   "business unit and application in `bu_mapping.json`.")
    elif value==bl.NOT_LABELED:
        msg = "**Action:** this cost cannot be charged to anyone. Open the **By service**, **By project** and **Top resources** tabs and start with the biggest rows."
        keys = bl.source_keys(dim)
        if keys:
            label = "feature" if dim in bl.DERIVED else bl.TITLES[dim].lower()
            msg += f" Then add the {label} label (key: {', '.join(f'`{k}`' for k in keys)}) in Google Cloud."
        st.warning(msg)
    elif delta>0 and prev>0 and delta_pct>=bl.SPIKE_PCT:
        grown = services[services["delta"]>0].sort_values("delta",ascending=False)
        if len(grown):
            g = grown.iloc[0]
            st.warning(f"**Action:** cost is up {delta_pct:.0%} ({fmt_money(delta)}). The biggest increase is **{g['value']}** (+{fmt_money(g['delta'])}). "
                       "Check the tabs below, then ask the owner whether this is expected.")
    try:
        daily,resources = load_leadership_drill(dim,value,start_day,end_day,*filters)
    except Exception as exc:
        st.error("Could not load the daily trend and resources for this selection.")
        st.code(str(exc))
        daily,resources = pd.DataFrame(),pd.DataFrame()
    if not daily.empty:
        st.plotly_chart(bl.drill_trend(daily,value),use_container_width=True,key="bill_drill_trend")
    others = [d for d in bl.ALL_DIMS if d!=dim]
    tabs = st.tabs([f"By {bl.TITLES[d].lower()}" for d in others]+["Top resources"])
    for tab,d in zip(tabs,others):
        with tab:
            fig,table = bl.breakdown_pie(rows,d,fmt_money)
            if fig is None:
                st.info("Nothing to show.")
                continue
            st.plotly_chart(fig,use_container_width=True,key=f"bill_bd_{d}")
            shown = table.sort_values("cost",ascending=False).head(50).assign(delta_pct=lambda t: t["delta_pct"]*100,share=lambda t: t["share"]*100)
            st.dataframe(shown,use_container_width=True,hide_index=True,column_config={
                "value":bl.TITLES[d],"cost":st.column_config.NumberColumn("Net cost",format="%.0f"),
                "prev":st.column_config.NumberColumn("Previous period",format="%.0f"),
                "delta":st.column_config.NumberColumn("Change",format="%.0f"),
                "delta_pct":st.column_config.NumberColumn("Change %",format="%.0f%%"),
                "share":st.column_config.NumberColumn("Share %",format="%.0f%%")})
    with tabs[-1]:
        if resources.empty:
            st.info("No resource detail available.")
        else:
            st.caption("Top 25 resources by net cost in the selected dates.")
            st.dataframe(resources,use_container_width=True,hide_index=True,column_config={
                "resource":"Resource","service":"Service","project":"Project","net_cost":st.column_config.NumberColumn("Net cost",format="%.0f")})
    st.download_button("⬇️ Download this selection (CSV)",data=rows.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"gcp_finops_leadership_{dim}_{str(value)[:30]}.csv",mime="text/csv",key=f"bill_dl_{dim}_{value}")


def style_chart(fig, height=360):
    fig.update_layout(height=height, margin=dict(l=10,r=10,t=48,b=10), paper_bgcolor="white",
                      plot_bgcolor="white", font=dict(family="Arial",color="#34435b"),
                      legend=dict(orientation="h",yanchor="bottom",y=1.02,xanchor="left",x=0),
                      hoverlabel=dict(bgcolor="white"))
    fig.update_xaxes(showgrid=False,zeroline=False)
    fig.update_yaxes(gridcolor="#edf1f6",zeroline=False)
    return fig


VIEWS = ["📊 Overview","🏛️ Leadership","🧱 Resource explorer","🧭 Cost breakdowns","📈 CPU & RAM","⬇️ Data & export"]
LEADERSHIP = "🏛️ Leadership"
PERIODS = {"Last 7 days":7,"Last 30 days":30,"Last 60 days":60,"Last 90 days":90,"Last 180 days":180}
st.markdown(bl.EXEC_CSS,unsafe_allow_html=True)
hero_slot = st.empty()
if st.session_state.get("view_nav",VIEWS[0])==LEADERSHIP:
    hero_slot.markdown(bl.exec_hero("…","Loading billing data",""),unsafe_allow_html=True)
else:
    hero_slot.markdown("""<div class="hero"><h1>💠 GCP FinOps · Cost Intelligence</h1>
<p>Understand cloud spend by service, project, region, SKU and resource — using the latest available billing export.</p></div>""", unsafe_allow_html=True)
view = st.radio("Dashboard view",VIEWS,horizontal=True,label_visibility="collapsed",key="view_nav")

# "View by" row: period plus the organisation filters (business unit -> feature cluster -> product). They apply to every view.
today = date.today()
vb_label,vb_bu,vb_cluster,vb_app,vb_period,vb_note = st.columns([0.55,1.5,1.5,1.5,1.25,1.0])
vb_label.markdown('<div class="xview">VIEW BY</div>',unsafe_allow_html=True)
with vb_period:
    period = st.selectbox("Period",list(PERIODS)+["Custom"],index=1,label_visibility="collapsed")
    if period == "Custom":
        chosen = st.date_input("Billing usage dates",value=(today-timedelta(days=29),today),max_value=today,label_visibility="collapsed")
        if isinstance(chosen,(tuple,list)) and len(chosen)==2:
            start_day,end_day = chosen
        else:
            start_day=end_day=chosen
    else:
        start_day,end_day = today-timedelta(days=PERIODS[period]-1),today
if start_day>end_day:
    st.error("Start date must be before end date.")
    st.stop()
vb_note.markdown('<div class="xnote">Filters apply to every view</div>',unsafe_allow_html=True)

with st.sidebar:
    st.markdown("## 🎛️ Dashboard controls")
    st.caption("Period, business unit, feature cluster and product are in the **View by** row at the top. These filters also apply to all views.")
    if st.button("↻ Refresh billing data", use_container_width=True):
        get_filter_options.clear()
        get_org_options.clear()
        load_summary_data.clear()
        load_resource_data.clear()
        load_resource_counts.clear()
        load_leadership_billing.clear()
        load_leadership_daily.clear()
        load_leadership_drill.clear()
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

try:
    org_combos = get_org_options(start_day,end_day)
except Exception:
    # fall back to the BU billing plan itself when the options query fails
    org_combos = pd.DataFrame([(bu,bl.NOT_LABELED,app) for app,bu in bl.MAPPING["app_bu"].items()],
                              columns=["business_unit","feature_cluster","application"])

bus_sel = vb_bu.multiselect("Business unit",org_choices(org_combos,"business_unit",{}),placeholder="All business units",format_func=bl.bu_name,
                            label_visibility="collapsed",help="Rolled up from the feature label with bu_mapping.json.")
clusters_sel = vb_cluster.multiselect("Feature cluster",org_choices(org_combos,"feature_cluster",{"business_unit":bus_sel}),placeholder="All feature clusters",
                                      format_func=lambda v: bl.display(bl.CLUSTER,v),label_visibility="collapsed")
apps_sel = vb_app.multiselect("Product",org_choices(org_combos,"application",{"business_unit":bus_sel,"feature_cluster":clusters_sel}),
                              placeholder="All products",format_func=lambda v: bl.display(bl.APP,v),label_visibility="collapsed")
org = (tuple(bus_sel),tuple(clusters_sel),tuple(apps_sel))

with st.sidebar:
    st.markdown("**Billing filters**")
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
        df = load_summary_data(start_day,end_day,services,projects,regions,skus,currency,resource_search,org)
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

if view!=LEADERSHIP:
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
    signature = (str(start_day),str(end_day),tuple(services),tuple(projects),tuple(regions),tuple(skus),currency,resource_search.strip(),org)
    if st.button("Load resource details (up to 1,000 rows)",type="primary"):
        try:
            with st.spinner("Loading resource details from BigQuery…"):
                result = load_resource_data(start_day,end_day,services,projects,regions,skus,currency,resource_search,org)
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

elif view==LEADERSHIP:
    st.markdown(vc.KPI_CSS+bl.LEAD_CSS,unsafe_allow_html=True)
    tab_cost,tab_vm = st.tabs(["🏢 Executive overview","🖥️ VM efficiency"])
    with tab_cost:
        prev_start = previous_period_start(start_day,end_day)
        bill_filters = (services,projects,regions,skus,currency,resource_search,org)
        billing = daily_all = None
        try:
            with st.spinner("Loading leadership billing data…"):
                billing = load_leadership_billing(start_day,end_day,prev_start,*bill_filters)
                daily_all = load_leadership_daily(start_day,end_day,prev_start,*bill_filters)
        except Exception as exc:
            st.error("Unable to load the leadership billing data.")
            st.code(str(exc))
        if billing is not None and billing.loc[billing["period"]=="current","net_cost"].abs().sum()==0:
            st.warning("No cost found for these dates and filters.")
        elif billing is not None:
            fmt_money = lambda v: money(v,cur_label)
            sm = bl.summary(billing,daily_all,date.today())
            score = bl.bu_scorecard(billing,daily_all,date.today())
            colors = bl.bu_colors()
            has_prev = sm["prev"]>0
            bu_options = score.loc[score["cost"]>0,"value"].tolist()
            if bu_options and st.session_state.get("bu_focus") not in bu_options:
                st.session_state["bu_focus"] = bu_options[0]
            found = bl.insights(billing,score,fmt_money)

            def open_or_focus(dim,value):
                """A business unit opens in the product view and deep dive; anything else opens the details pop-up."""
                if dim==bl.BU and value in bu_options:
                    st.session_state["bu_focus"] = value
                elif value is not None:
                    st.session_state["bill_open"] = (dim,value)

            def on_chart_click(chart_key,lookup,dim):
                """Callback for a click on a chart slice or bar; the pop-up / focus change shows on the rerun."""
                def _cb():
                    state = st.session_state.get(chart_key)
                    open_or_focus(dim,bl.resolve_click(state["selection"]["points"] if state else [],lookup))
                return _cb

            def on_pick(widget_key,dim):
                """Callback for the pills under a pie: open that value, then clear the pill so the same one can be picked again."""
                def _cb():
                    open_or_focus(dim,st.session_state.get(widget_key))
                    st.session_state[widget_key] = None
                return _cb

            def on_action_pick():
                """Callback for the pills under the leadership actions: open the numbered action."""
                number = st.session_state.get("exec_act_pick")
                if number:
                    item = found[number-1]
                    open_or_focus(item["dim"],item["value"])
                st.session_state["exec_act_pick"] = None

            def on_table_pick():
                """Callback for a row picked in the scorecard table: focus that unit."""
                picked = (st.session_state.get("bu_table") or {}).get("selection",{}).get("rows",[])
                if picked:
                    open_or_focus(bl.BU,score.iloc[picked[0]]["value"])

            def on_coverage_click(chart_key,lookup):
                """Callback for the coverage bars: the lookup holds the (dimension, value) to open."""
                def _cb():
                    state = st.session_state.get(chart_key)
                    key = bl.resolve_click(state["selection"]["points"] if state else [],lookup)
                    if key is not None:
                        st.session_state["bill_open"] = key
                return _cb

            def pie_card(rows,dim,key,pick_label="Open details for",pills=True,**kwargs):
                """A spend donut. Clicking a slice opens it; the pills underneath do the same and always work."""
                fig,lookup,options = bl.spend_pie(rows,dim,fmt_money,**kwargs)
                if fig is None:
                    st.info(f"No {bl.TITLES[dim].lower()} cost to show.")
                    return
                if kwargs.get("clickable",True):
                    st.plotly_chart(fig,use_container_width=True,key=key,on_select=on_chart_click(key,lookup,dim),selection_mode="points",
                                    config={"displayModeBar":False})
                    if pills:
                        st.pills(pick_label,options,selection_mode="single",key=f"{key}_pick",format_func=lambda v: bl.display(dim,v),
                                 on_change=on_pick(f"{key}_pick",dim))
                else:
                    st.plotly_chart(fig,use_container_width=True,key=key,config={"displayModeBar":False})

            # VM utilization (shared with the CPU & RAM view): needed for underutilized candidates, savings and the watchlist
            projects_t = tuple(MONITORING_PROJECTS)
            vm_signature = (str(start_day),str(end_day),projects_t,currency)
            recs = None
            if st.session_state.get("finops_vm_signature")==vm_signature:
                vm_data = st.session_state["finops_vm_data"]
                window_start = datetime.combine(start_day,dtime.min,tzinfo=timezone.utc)
                window_end = min(datetime.combine(end_day+timedelta(days=1),dtime.min,tzinfo=timezone.utc),datetime.now(timezone.utc))
                recs_all = build_recommendations(vm_data["inventory"],vm_data["util"],vm_data["costs"],vm_data["catalogs"],window_start,window_end,Params())
                recs = bl.filter_org(bl.vm_org(recs_all),org) if not recs_all.empty else recs_all
            try:
                counts = load_resource_counts(start_day,end_day,*bill_filters)
            except Exception:
                counts = None

            scope = [f"{bl.TITLES[d]}: {', '.join(bl.display(d,v) for v in chosen)}" for d,chosen in zip((bl.BU,bl.CLUSTER,bl.APP),org) if chosen]
            hero_slot.markdown(bl.exec_hero(fmt_money(sm["cost"]),f"Net cloud spend · {start_day:%d %b} – {end_day:%d %b %Y}",
                                            ("Showing " + "; ".join(scope) + ".") if scope else ""),unsafe_allow_html=True)

            down = recs[recs["status"].isin(lead.DOWNSIZE)] if recs is not None else None
            sched = bl.schedule_saving(billing,start_day,end_day)
            cards = [bl.exec_kpi("Total cloud spend",fmt_money(sm["cost"]),
                                 (bl.change_span(sm["delta"],sm["delta_pct"],fmt_money," vs previous period") if has_prev else "Selected period · net of credits"),
                                 {"INR":"₹","USD":"$","EUR":"€","GBP":"£"}.get(cur_label,"¤"))]
            if counts:
                cards.append(bl.exec_kpi("Resources inventoried",f"{counts['resources']:,}",f"Billed resources across {counts['projects']} GCP projects · excl. jobs","▦","#6250d6"))
            else:
                cards.append(bl.exec_kpi("Resources inventoried","—","Resource count unavailable","▦","#6250d6"))
            if down is not None:
                cards.append(bl.exec_kpi("Underutilized candidates",f"{len(down):,}",f"of {len(recs):,} running VMs · requires owner validation","⌁","#eb6834"))
                cards.append(bl.exec_kpi("Potential monthly savings",fmt_money(down["monthly_saving"].sum(skipna=True)),
                                         "VM rightsizing estimate, not realized" + (f" · +{fmt_money(sched)} from non-prod schedules" if sched>0 else ""),
                                         "↘","#0ca30c","#0a7d0a"))
            else:
                cards.append(bl.exec_kpi("Underutilized candidates","—","Load VM utilization to evaluate","⌁","#eb6834"))
                cards.append(bl.exec_kpi("Potential monthly savings",fmt_money(sched) if sched>0 else "—",
                                         "Non-prod schedules only · load VM utilization for rightsizing","↘","#0ca30c","#0a7d0a"))
            if counts and counts["resources"]:
                cards.append(bl.exec_kpi("Optimization coverage",f"{counts['mapped']/counts['resources']:.0%}",
                                         f"Resources with BU / product mapping · {sm['assigned_pct']:.0%} of spend","✓","#1baf7a"))
            else:
                cards.append(bl.exec_kpi("Optimization coverage",f"{sm['assigned_pct']:.0%}","Spend with BU / product mapping","✓","#1baf7a"))
            st.markdown(bl.exec_kpis(cards),unsafe_allow_html=True)
            if recs is None:
                note,load_col = st.columns([4,1])
                note.info(f"Underutilized candidates, rightsizing savings and the watchlist need VM CPU/RAM history (project(s): {', '.join(projects_t)}). "
                          "Loading it once also serves the **CPU & RAM** view.")
                if load_col.button("Load VM utilization",type="primary",key="exec_vm_load",use_container_width=True):
                    load_vm_bundle(projects_t,start_day,end_day,currency)
                    st.rerun()

            row_h = 380
            t_col,d_col = st.columns([1.45,1])
            with t_col:
                st.plotly_chart(bl.period_trend(daily_all,start_day,end_day,fmt_money,row_h),use_container_width=True,key="exec_trend",
                                config={"displayModeBar":False})
            with d_col:
                pie_card(billing,bl.BU,"exec_bu_donut",pills=False,color_map=colors,height=row_h,title="Spend by business unit",
                         subtitle="Share of total selected spend · click a slice to open the unit")

            panel_h = 330
            b_col,o_col,w_col = st.columns(3)
            b_col.markdown(bl.html_card("Spend by business unit","Net cost per unit · change vs the previous period",
                                        bl.bu_bars_html(score,colors,fmt_money),panel_h),unsafe_allow_html=True)
            o_col.markdown(bl.html_card("Top optimization opportunities","Estimated monthly savings by action",
                                        bl.opportunities_html(bl.opportunities(recs,billing,start_day,end_day),fmt_money,recs is not None),panel_h),
                           unsafe_allow_html=True)
            w_col.markdown(bl.html_card("Underutilized resource watchlist","VM findings requiring engineering review",
                                        bl.watchlist_html(recs,fmt_money),panel_h),unsafe_allow_html=True)

            if bu_options:
                sel_col,_spacer = st.columns([1.45,1])
                focus = sel_col.selectbox("Product cost view for",bu_options,key="bu_focus",format_func=bl.bu_name)
                vm_savings = (down[down[bl.BU]==focus].groupby(bl.APP)["monthly_saving"].sum().to_dict() if down is not None else None)
                p_col,a_col = st.columns([1.45,1])
                p_col.markdown(bl.html_card(f"{bl.bu_name(focus)} · product cost view",
                                            "Business unit → feature cluster → product · potential = VM rightsizing savings per month",
                                            bl.product_table_html(billing,focus,vm_savings,fmt_money),row_h),unsafe_allow_html=True)
                with a_col:
                    st.markdown(bl.html_card("Recommended leadership actions","Move from opportunity to verified savings",bl.actions_html(found),row_h),
                                unsafe_allow_html=True)
                    if found:
                        st.pills("Open action",list(range(1,min(len(found),5)+1)),selection_mode="single",key="exec_act_pick",
                                 format_func=lambda n: f"#{n}",on_change=on_action_pick)

            st.markdown(bl.section("Explore further","drill into a unit, what changed and how well spend is mapped"),unsafe_allow_html=True)
            t_dive,t_mix,t_score,t_change,t_matrix,t_health = st.tabs(["🔎 Business unit deep dive","🍩 Spend breakdown","📋 Scorecard",
                                                                        "📉 What changed","🧩 BU × dimension","🩺 Mapping health"])
            with t_dive:
                if not bu_options:
                    st.info("No business unit spend in this selection.")
                else:
                    focus = st.session_state["bu_focus"]
                    r = score[score["value"]==focus].iloc[0]
                    rows = bl.select_rows(billing,bl.BU,focus)
                    color = colors.get(focus,bl.BASE_COLOR)
                    head_col,open_col = st.columns([3,1])
                    head_col.caption(f"Showing **{bl.bu_name(focus)}** · change the unit in **Product cost view for** above."
                                     + (" Applications in the BU billing plan: " + ", ".join(bl.MAPPING["apps_by_bu"][focus]) if focus in bl.MAPPING["apps_by_bu"] else ""))
                    if open_col.button("Open full details →",key="bu_focus_open",use_container_width=True):
                        st.session_state["bill_open"] = (bl.BU,focus)
                    st.markdown(bl.focus_story(r,rows,sm["bu_count"],fmt_money),unsafe_allow_html=True)
                    c1,c2,c3,c4 = st.columns(4)
                    c1.markdown(vc.kpi_card("Net cost",fmt_money(r["cost"]),f"{r['share']:.0%} of total spend",color),unsafe_allow_html=True)
                    c2.markdown(vc.kpi_card("vs previous period",bl.arrow_text(r["delta"],r["delta_pct"],fmt_money) if r["prev"]>0 else "new",
                                            f"was {fmt_money(r['prev'])}","#EF4444" if r["delta"]>0 else "#10B981"),unsafe_allow_html=True)
                    c3.markdown(vc.kpi_card("Run rate / 30 days",fmt_money(r["run_rate"]),f"{fmt_money(r['run_rate']/30)} per day","#6250d6"),unsafe_allow_html=True)
                    c4.markdown(vc.kpi_card("Non-production",f"{r['nonprod_pct']:.0%}" if pd.notna(r["nonprod_pct"]) else "n/a",
                                            f"{r['owner_pct']:.0%} of spend has an owner" if pd.notna(r["owner_pct"]) else "","#eb6834"),unsafe_allow_html=True)
                    f1,f2 = st.columns(2)
                    with f1:
                        fig = bl.focus_trend(daily_all,focus,start_day,end_day,color)
                        if fig is not None:
                            st.plotly_chart(fig,use_container_width=True,key="bu_focus_trend",config={"displayModeBar":False})
                    with f2:
                        fig,_lookup = bl.bridge(rows,bl.APP,fmt_money,n=6,title="What drove the change",
                                                subtitle="Previous → selected period, by product · red = more spend, green = less.",clickable=False)
                        if fig is None:
                            st.info("No previous-period spend to compare with.")
                        else:
                            st.plotly_chart(fig,use_container_width=True,key="bu_focus_bridge",config={"displayModeBar":False})
                    for pair in (bl.FOCUS_DIMS[:2],bl.FOCUS_DIMS[2:]):
                        for holder,dim in zip(st.columns(2),pair):
                            with holder:
                                pie_card(rows,dim,f"bu_focus_{dim}",clickable=False,title=f"{bl.bu_name(focus)} · by {bl.TITLES[dim].lower()}",
                                         subtitle="Share of this unit's net cost.")
            with t_mix:
                m1,m2 = st.columns(2)
                with m1:
                    pie_card(billing,bl.CLUSTER,"pie_cluster")
                with m2:
                    pie_card(billing,bl.APP,"pie_app",title="Spend by product")
                s1,s2 = st.columns([1,1.25])
                with s1:
                    fig = bl.bu_sunburst(billing,colors,fmt_money)
                    if fig is not None:
                        st.plotly_chart(fig,use_container_width=True,key="bu_sunburst",config={"displayModeBar":False})
                with s2:
                    fig = bl.bu_trend(daily_all,score,colors,start_day,end_day,fmt_money)
                    if fig is not None:
                        st.plotly_chart(fig,use_container_width=True,key="bu_trend",config={"displayModeBar":False})
            with t_score:
                st.caption("Pick a row to show that unit in the product cost view and the deep dive.")
                table = score.assign(name=score["value"].map(bl.bu_name),share=score["share"]*100,delta_pct=score["delta_pct"]*100,
                                     nonprod_pct=score["nonprod_pct"]*100,owner_pct=score["owner_pct"]*100)
                st.dataframe(table[["name","signal","cost","share","delta","delta_pct","run_rate","trend","apps","top_app","top_service","nonprod_pct","owner_pct"]],
                             use_container_width=True,hide_index=True,key="bu_table",on_select=on_table_pick,selection_mode="single-row",
                             column_config={
                                 "name":"Business unit","signal":"Signal",
                                 "cost":st.column_config.NumberColumn("Net cost",format="%.0f"),
                                 "share":st.column_config.ProgressColumn("Share of spend",format="%.0f%%",min_value=0,max_value=100),
                                 "delta":st.column_config.NumberColumn("Change",format="%+.0f"),
                                 "delta_pct":st.column_config.NumberColumn("Change %",format="%+.0f%%"),
                                 "run_rate":st.column_config.NumberColumn("Run rate / 30d",format="%.0f"),
                                 "trend":st.column_config.LineChartColumn("Daily trend",y_min=0),
                                 "apps":st.column_config.NumberColumn("Products",format="%d"),
                                 "top_app":"Top product","top_service":"Top service",
                                 "nonprod_pct":st.column_config.ProgressColumn("Non-prod",format="%.0f%%",min_value=0,max_value=100),
                                 "owner_pct":st.column_config.ProgressColumn("Has owner",format="%.0f%%",min_value=0,max_value=100)})
            with t_change:
                if not has_prev:
                    st.info("No billing data was found for the previous period, so changes cannot be shown.")
                else:
                    mover_dim = st.radio("Break the change down by",bl.ALL_DIMS,format_func=lambda d: bl.TITLES[d],horizontal=True,key="bill_mover_dim")
                    fig,lookup = bl.bridge(billing,mover_dim,fmt_money)
                    if fig is None:
                        st.info("Nothing changed for this view.")
                    else:
                        st.plotly_chart(fig,use_container_width=True,key="bill_chart_bridge",on_select=on_chart_click("bill_chart_bridge",lookup,mover_dim),
                                        selection_mode="points",config={"displayModeBar":False})
            with t_matrix:
                matrix_dim = st.radio("Compare business units by",bl.MATRIX_DIMS,format_func=lambda d: bl.TITLES[d],horizontal=True,key="bu_matrix_dim")
                fig = bl.bu_matrix(billing,matrix_dim,fmt_money)
                if fig is not None:
                    st.plotly_chart(fig,use_container_width=True,key="bu_matrix",config={"displayModeBar":False})
            with t_health:
                h1,h2 = st.columns(2)
                with h1:
                    fig,lookup = bl.coverage_bars(billing)
                    if fig is not None:
                        st.plotly_chart(fig,use_container_width=True,key="bu_coverage",on_select=on_coverage_click("bu_coverage",lookup),
                                        selection_mode="points",config={"displayModeBar":False})
                with h2:
                    fig = bl.tagging_trend(daily_all,end_day)
                    if fig is not None:
                        st.plotly_chart(fig,use_container_width=True,key="bu_tag_trend",config={"displayModeBar":False})
                unmapped = bl.unmapped_features(billing)
                if len(unmapped):
                    st.markdown(f"**{len(unmapped)} feature(s) outside the BU billing plan · {fmt_money(sm['unmapped'])}**")
                    st.caption("Add each feature (lower case) to the right business unit and product in `bu_mapping.json`, then click **↻ Refresh billing data**.")
                    st.dataframe(unmapped.assign(share=unmapped["share"]*100)[["value","cluster","cost","prev","delta","share"]],
                                 use_container_width=True,hide_index=True,column_config={
                                     "value":"Feature label","cluster":"Feature cluster",
                                     "cost":st.column_config.NumberColumn("Net cost",format="%.0f"),
                                     "prev":st.column_config.NumberColumn("Previous period",format="%.0f"),
                                     "delta":st.column_config.NumberColumn("Change",format="%+.0f"),
                                     "share":st.column_config.ProgressColumn("Share of total",format="%.1f%%",min_value=0,max_value=100)})

            bill_opened = st.session_state.pop("bill_open",None)
            if bill_opened:
                open_dim,open_value = bill_opened
                open_title,_open_sub = bl.describe(open_dim,open_value)

                @st.dialog(open_title,width="large")
                def show_bill_details():
                    render_bill_details(open_dim,open_value,billing,bill_filters,fmt_money,start_day,end_day)
                show_bill_details()
    with tab_vm:
        st.markdown("<div class='sec-title'>Leadership view · Cloud efficiency</div>"
                    "<div class='sec-sub'>Where VM spend can be reduced, who owns it and how healthy the fleet is. "
                    "<b>Click any bar to open the details.</b></div>",unsafe_allow_html=True)
        projects_t = tuple(MONITORING_PROJECTS)
        lead_signature = (str(start_day),str(end_day),projects_t,currency)
        if st.button("Load leadership data",type="primary",key="lead_load"):
            load_vm_bundle(projects_t,start_day,end_day,currency)
        if st.session_state.get("finops_vm_signature")!=lead_signature:
            st.info("Click **Load leadership data**. It reads the VM list, CPU/RAM history and billed cost for the sidebar dates "
                    "(the same data as the **CPU & RAM** view, so loading it once serves both).")
        else:
            vm_data = st.session_state["finops_vm_data"]
            window_start = datetime.combine(start_day,dtime.min,tzinfo=timezone.utc)
            window_end = min(datetime.combine(end_day+timedelta(days=1),dtime.min,tzinfo=timezone.utc),datetime.now(timezone.utc))
            recs = build_recommendations(vm_data["inventory"],vm_data["util"],vm_data["costs"],vm_data["catalogs"],window_start,window_end,Params())
            if vm_data["errors"]:
                with st.expander(f"⚠️ {len(vm_data['errors'])} request(s) failed",expanded=False):
                    for message in vm_data["errors"]:
                        st.code(message)
            if recs.empty:
                st.warning("No running VMs were found for the configured project(s).")
            else:
                vm_cur = vm_data["costs"]["currency"].iloc[0] if not vm_data["costs"].empty else cur_label
                fmt_money = lambda v: money(v,vm_cur)
                sm = lead.summary(recs)
                st.caption(f"Running VMs in {', '.join(MONITORING_PROJECTS)} · {start_day:%d %b %Y} to {end_day:%d %b %Y} · default sizing assumptions "
                           "(CPU 60% / RAM 70% at the p95 hourly peak). Change them in the **CPU & RAM** view for a technical deep dive.")
                cards = [
                    ("Est. monthly savings",fmt_money(sm["savings"]),f"≈ {fmt_money(sm['yearly'])} a year","#10B981",
                     f"Downsizing idle and over-provisioned VMs. vCPUs {sm['vcpu_now']:,.0f} → {sm['vcpu_rec']:,.0f}."),
                    ("Savings potential",f"{sm['savings_pct']:.0%}",f"of {fmt_money(sm['base_cost'])} monthly VM compute","#F59E0B","Share of VM compute cost that could be saved."),
                    ("VMs to downsize",f"{sm['down']:,}",f"{sm['idle']} idle · {sm['over']} over-provisioned","#8B5CF6","Idle and over-provisioned VMs."),
                    ("VMs running hot",f"{sm['up']:,}",f"+{fmt_money(sm['extra'])} a month to fix","#EF4444","VMs that need more CPU or RAM."),
                    ("Fleet right-sized",f"{sm['right_pct']:.0%}",f"{sm['right']} of {sm['evaluated']} evaluated VMs","#3B82F6","Share of evaluated VMs whose size fits."),
                    ("Owner label coverage",f"{sm['owner_pct']:.0%}",f"{sm['no_owner']} VMs have no owner","#06B6D4","VMs carrying an owner label."),
                ]
                for holder,(label,value,sub,accent,tip) in zip(st.columns(6),cards):
                    holder.markdown(vc.kpi_card(label,value,sub,accent,tip),unsafe_allow_html=True)
                st.write("")

                def on_chart_click(chart_key,lookup,kind):
                    """Callback: remember which bar was clicked; the details pop-up opens on the rerun."""
                    def _cb():
                        state = st.session_state.get(chart_key)
                        points = state["selection"]["points"] if state else []
                        value = lead.resolve_click(points,lookup)
                        if value is not None:
                            st.session_state["lead_open"] = (kind,value)
                    return _cb

                def clickable(fig,lookup,chart_key,kind):
                    st.plotly_chart(fig,use_container_width=True,key=chart_key,on_select=on_chart_click(chart_key,lookup,kind),selection_mode="points",
                                    config={"displayModeBar":False})

                row1a,row1b = st.columns(2)
                with row1a:
                    fig,lookup = lead.health_bars(recs,fmt_money)
                    clickable(fig,lookup,"lead_chart_health","status")
                with row1b:
                    fig,lookup = lead.savings_by_group(recs,"client",fmt_money)
                    if fig is None:
                        st.info("No downsizing savings to show by client.")
                    else:
                        clickable(fig,lookup,"lead_chart_client","client")
                row2a,row2b = st.columns(2)
                with row2a:
                    fig,lookup = lead.savings_by_group(recs,"owner",fmt_money)
                    if fig is None:
                        st.info("No downsizing savings to show by owner.")
                    else:
                        clickable(fig,lookup,"lead_chart_owner","owner")
                with row2b:
                    fig,lookup = lead.savings_by_group(recs,"environment",fmt_money)
                    if fig is None:
                        st.info("No downsizing savings to show by environment.")
                    else:
                        clickable(fig,lookup,"lead_chart_env","environment")
                row3a,row3b = st.columns(2)
                with row3a:
                    fig,lookup = lead.top_vm_savings(recs,fmt_money)
                    if fig is None:
                        st.info("No VM savings to show.")
                    else:
                        clickable(fig,lookup,"lead_chart_vms","vm")
                with row3b:
                    fig,lookup = lead.label_coverage(recs)
                    clickable(fig,lookup,"lead_chart_labels","unlabeled")

                with st.expander("Prefer a list? Open details without clicking",expanded=False):
                    kinds = {"Status":"status","Client":"client","Owner":"owner","Environment":"environment","Department":"department","Application":"application","A single VM":"vm"}
                    pick_label = st.selectbox("What do you want to look at?",list(kinds),key="lead_pick_kind")
                    pick_kind = kinds[pick_label]
                    if pick_kind=="vm":
                        vm_names = recs.sort_values("monthly_saving",ascending=False,na_position="last").set_index("instance_id")["instance_name"].to_dict()
                        pick_value = st.selectbox("VM",list(vm_names),format_func=lambda i: vm_names[i],key="lead_pick_vm")
                    elif pick_kind=="status":
                        pick_value = st.selectbox("Status",[s for s in STATUS_ORDER if s in set(recs["status"])],key="lead_pick_status")
                    else:
                        pick_value = st.selectbox(pick_label,sorted(recs[pick_kind].unique(),key=lambda v:(v==NOT_LABELED,str(v).lower())),key=f"lead_pick_{pick_kind}")
                    if st.button("Open details",key="lead_pick_open"):
                        st.session_state["lead_open"] = (pick_kind,pick_value)
                        st.rerun()

                opened = st.session_state.pop("lead_open",None)
                if opened:
                    open_kind,open_value = opened
                    open_rows = lead.select_rows(recs,open_kind,open_value)
                    open_title,open_sub = lead.describe(open_kind,open_value,recs)

                    @st.dialog(open_title,width="large")
                    def show_lead_details():
                        render_lead_details(open_rows,open_kind,open_value,open_sub,vm_data,fmt_money)
                    show_lead_details()

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
        load_vm_bundle(projects_t,start_day,end_day,currency)
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
        all_recs = build_recommendations(vm_data["inventory"],vm_data["util"],vm_data["costs"],vm_data["catalogs"],window_start,window_end,
                                         Params(target_cpu=target_cpu,target_mem=target_mem,min_saving_pct=min_saving))
        filtered = all_recs
        recs = all_recs
        if not all_recs.empty:
            filter_keys = {col:f"finops_vm_f_{col}" for col in LABEL_COLUMNS}
            filter_keys.update(series="finops_vm_f_series",confidence="finops_vm_f_conf",name="finops_vm_f_name")
            def reset_vm_filters():
                for key in filter_keys.values():
                    st.session_state[key] = "" if key.endswith("_name") else []
            with st.expander("🔎 Filters: owner, client, environment, application…",expanded=True):
                st.caption("Leave a filter empty to include everything. Values come from the VM labels in Google Cloud; "
                           f"VMs without a usable label show as **{NOT_LABELED}**.")
                label_cols = list(LABEL_FIELDS.items())
                for row_start in range(0,len(label_cols),4):
                    holders = st.columns(4)
                    for holder,(col,(title,_)) in zip(holders,label_cols[row_start:row_start+4]):
                        options = sorted(all_recs[col].dropna().unique(),key=lambda v:(v==NOT_LABELED,str(v).lower()))
                        holder.multiselect(title,options,key=filter_keys[col],placeholder=f"All ({len(options)})")
                x1,x2,x3,x4 = st.columns(4)
                x1.multiselect("Machine series",sorted(all_recs["series"].dropna().unique()),key=filter_keys["series"],placeholder="All")
                conf_options = sorted([c for c in all_recs["confidence"].dropna().unique() if c])
                x2.multiselect("Confidence",conf_options,key=filter_keys["confidence"],placeholder="All")
                x3.text_input("VM name contains",key=filter_keys["name"],placeholder="e.g. iksgcp-1")
                with x4:
                    st.write("")
                    st.button("Clear filters",on_click=reset_vm_filters,use_container_width=True)
                apply_everywhere = st.checkbox("Also apply filters to the KPIs and charts (otherwise only the recommendations table is filtered)",value=True,key="finops_vm_f_all")
            mask = pd.Series(True,index=all_recs.index)
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
                mask &= all_recs["instance_name"].str.contains(name_query,case=False,regex=False,na=False)
            filtered = all_recs[mask]
            recs = filtered if apply_everywhere else all_recs
            st.caption(f"Showing **{len(filtered):,}** of {len(all_recs):,} running VMs after filters.")
        if all_recs.empty:
            st.warning("No running VMs were found for the configured project(s).")
        elif filtered.empty:
            st.warning("No VMs match the selected filters. Clear or loosen a filter.")
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
                                         hover_name="instance_name",hover_data={"owner":True,"client":True,"machine_type":True,"recommended_type":True,"cpu_p95":":.0f","mem_p95":":.0f","bubble":False},
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

                st.markdown("<div class='section-head'>Who owns the savings?</div>",unsafe_allow_html=True)
                dim_titles = {col:title for col,(title,_) in LABEL_FIELDS.items()}
                dim = st.selectbox("Group by",list(dim_titles),format_func=lambda c: dim_titles[c],key="finops_vm_group_dim")
                grouped = recs.assign(
                    saving=recs["monthly_saving"].where(recs["status"].isin(["Idle","Over-provisioned"]),0.0),
                    extra=(-recs["monthly_saving"]).where(recs["status"]=="Under-provisioned",0.0)).groupby(dim,as_index=False).agg(
                    VMs=("instance_id","count"),saving=("saving","sum"),extra=("extra","sum"),cost=("monthly_cost","sum"))
                g_left,g_right = st.columns(2)
                with g_left:
                    g_top = grouped.nlargest(15,"saving").sort_values("saving")
                    g_top = g_top.assign(label=g_top["saving"].map(lambda v: money(v,vm_cur)))
                    fig = px.bar(g_top,x="saving",y=dim,orientation="h",text="label",color_discrete_sequence=["#e09a37"],
                                 hover_data={"VMs":True,"extra":":,.0f","cost":":,.0f","saving":":,.0f","label":False},
                                 title=f"Estimated monthly savings by {dim_titles[dim].lower()}")
                    fig.update_layout(yaxis_title="",xaxis_title=f"Est. monthly saving ({vm_cur})")
                    st.plotly_chart(style_chart(fig,420),use_container_width=True)
                with g_right:
                    top_groups = grouped.nlargest(12,"VMs")[dim]
                    mix = recs[recs[dim].isin(top_groups)].groupby([dim,"status"],as_index=False).size().rename(columns={"size":"VMs"})
                    fig = px.bar(mix,x="VMs",y=dim,color="status",orientation="h",color_discrete_map=STATUS_COLORS,barmode="stack",
                                 title=f"VM status mix by {dim_titles[dim].lower()}",category_orders={"status":STATUS_ORDER})
                    fig.update_layout(yaxis_title="",xaxis_title="VMs")
                    st.plotly_chart(style_chart(fig,420),use_container_width=True)

                st.markdown("<div class='section-head'>Right-sizing recommendations</div>",unsafe_allow_html=True)
                chosen = st.multiselect("Status",STATUS_ORDER,default=[s for s in STATUS_ORDER if s in set(filtered["status"])],key="finops_vm_status_filter")
                table = filtered[filtered["status"].isin(chosen)].sort_values("monthly_saving",ascending=False,na_position="last")
                table = table[["instance_name"]+LABEL_COLUMNS+["zone","status","machine_type","vcpus","mem_gb","cpu_avg","cpu_p95","mem_avg","mem_p95",
                               "recommended_type","rec_vcpus","rec_mem_gb","monthly_cost","monthly_saving","confidence","reason","instance_id","labels"]]
                label_titles = {col:title for col,(title,_) in LABEL_FIELDS.items()}
                st.dataframe(table,use_container_width=True,hide_index=True,column_config={
                    **label_titles,"labels":"All labels",
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
                st.markdown("**Ownership (from VM labels)**")
                o_cols = st.columns(4)
                for index,(col,(title,_)) in enumerate(LABEL_FIELDS.items()):
                    o_cols[index%4].markdown(f"<div class='muted'>{title}</div><b>{vm[col]}</b>",unsafe_allow_html=True)
                if vm["labels"]:
                    with st.expander("All labels on this VM"):
                        st.code(vm["labels"].replace("; ","\n"))
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
