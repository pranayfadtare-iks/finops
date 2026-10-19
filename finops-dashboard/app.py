"""GCP FinOps dashboard: cached summary view and on-demand resource details."""
from datetime import date, timedelta
import os
import pandas as pd
import plotly.express as px
import streamlit as st
from google.cloud import bigquery

st.set_page_config(page_title="GCP FinOps | Cost Intelligence", page_icon="💠", layout="wide")
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "iks-finops")
TABLE_ID = os.getenv("GCP_BILLING_TABLE", "iks-finops.GCP_Finops_billing.gcp_billing_export_resource_v1_019EFB_4492BC_7B3DE6")
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

view = st.radio("Dashboard view",["📊 Overview","🧱 Resource explorer","🧭 Cost breakdowns","⬇️ Data & export"],horizontal=True,label_visibility="collapsed")

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
