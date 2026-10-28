"""Live dashboard over the DuckDB warehouse.

    make app        # then open http://localhost:8501

Reads the warehouse through short-lived read-only connections (see app_data), so the pipeline
can rebuild it while the app is open; the "Refresh data" button does exactly that.
The filter row above the tabs scopes every view.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

from wbpipe import app_data as d
from wbpipe.config import load_settings
from wbpipe.dashboard import fmt

CONFIG = os.environ.get("WBPIPE_CONFIG", "config/pipeline.yaml")

# Categorical slots (fixed order) and the diverging steps, each validated for its surface.
PALETTE = {
    "light": {
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
        "diverging": ["#952321", "#d6403e", "#ef8a87", "#f0efec", "#86b6ef", "#2a78d6", "#184f95"],
        "muted": "#898781",
    },
    "dark": {
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
        "diverging": ["#f4aaa8", "#d95454", "#8e2f2e", "#383835", "#184f95", "#3987e5", "#9ec5f4"],
        "muted": "#898781",
    },
}
GROUP_ORDER = ["HIC", "UMC", "LMC", "LIC", "INX"]  # INX = not classified (historically)
# Beyond these, views stop being readable: show the first N of the selection and say so.
MAX_PANELS = 24
MAX_ROWS = 60  # income groups keep their colour whatever is filtered
STATUS_ICON = {"pass": "✅", "warn": "⚠️", "fail": "⛔", "error": "⛔"}
# Axis labels as 1.5B / 40T rather than d3's 1.5G
COMPACT_AXIS = "replace(format(datum.value, '~s'), 'G', 'B')"


def warehouse() -> d.Warehouse:
    override = os.environ.get("WBPIPE_WAREHOUSE")
    if override:
        return d.Warehouse(Path(override))
    return d.Warehouse(load_settings(CONFIG).paths.warehouse)


@st.cache_data(show_spinner=False)
def _cached(path: str, version: int, name: str, args: tuple[Any, ...]) -> Any:
    return getattr(d, name)(d.Warehouse(Path(path)), *args)


def q(name: str, *args: Any) -> Any:
    """Cached query, keyed on the warehouse file version so a rebuild invalidates it."""
    wh = warehouse()
    return _cached(str(wh.path), wh.version(), name, args)


def palette() -> dict[str, Any]:
    # Follows theme.base in .streamlit/config.toml (a server setting): Altair colours are fixed
    # values, and the theme the browser session reports proved unreliable.
    return PALETTE["dark" if st.get_option("theme.base") == "dark" else "light"]


def refresh_pipeline() -> None:
    with st.spinner("Fetching from the World Bank API and rebuilding the warehouse…"):
        proc = subprocess.run(
            [sys.executable, "-m", "wbpipe.cli", "--config", CONFIG, "run"],
            capture_output=True,
            text=True,
            timeout=600,
        )
    st.cache_data.clear()
    log = (proc.stdout + proc.stderr).strip().splitlines()
    st.session_state["refresh_result"] = (proc.returncode, "\n".join(log[-12:]))


# ---- page ------------------------------------------------------------------------------------


def status_bar(status: dict[str, Any]) -> None:
    """Header row: what is being shown, its data-quality state, and the refresh action."""
    left, mid, right = st.columns([5, 4, 2], vertical_alignment="center")
    with left:
        st.subheader("World Bank indicators", anchor=False)
        if status.get("ingest_run_id"):
            st.caption(
                f"Live view of the local warehouse · ingest run `{status['ingest_run_id']}` · "
                f"fetched {status['fetched_at']:%Y-%m-%d %H:%M} · source updated "
                f"{pd.Timestamp(status['source_lastupdated']):%Y-%m-%d}"
            )
    dq = status["dq"]
    total = sum(dq.values())
    with mid:
        if not status["dq_checked"]:
            st.warning("Data-quality checks have not run for this build.")
        elif dq["fail"] or dq["error"]:
            st.error(f"⛔ {dq['fail'] + dq['error']} blocking data-quality check(s) failed")
        elif dq["warn"]:
            st.warning(f"⚠️ {dq['pass']}/{total} checks pass · {dq['warn']} warning(s)")
        else:
            st.success(f"✅ {dq['pass']}/{total} data-quality checks pass")
    with right:
        st.button(
            "Refresh data",
            on_click=refresh_pipeline,
            help="Fetches the latest data from the World Bank API and runs the full pipeline "
            "(ingest → transform → check → report → dashboard). "
            "Unchanged source data creates no new records.",
            width="stretch",
        )
    issues_panel()
    result = st.session_state.get("refresh_result")
    if result:
        code, log = result
        with st.expander(
            "✅ Pipeline run succeeded" if code == 0 else f"⛔ Pipeline run failed (exit {code})",
            expanded=code != 0,
        ):
            st.code(log or "(no output)", language="text")


def issues_panel() -> None:
    """Name every warning or failure right under the banner, with what it means and examples."""
    dq = q("dq_latest")
    issues = dq[dq["status"] != "pass"]
    if issues.empty:
        return
    summary = ", ".join(
        f"{r.check_name} ({'n/a' if pd.isna(r.violation_count) else int(r.violation_count)})"
        for r in issues.itertuples()
    )
    icon = "⛔" if issues["status"].isin(["fail", "error"]).any() else "⚠️"
    with st.expander(f"{icon} Data-quality findings: {summary}"):
        for r in issues.itertuples():
            count = (
                "could not run"
                if pd.isna(r.violation_count)
                else f"{int(r.violation_count)} row(s)"
            )
            st.markdown(
                f"**{STATUS_ICON.get(r.status, '')} `{r.check_name}`** · {r.severity} · {count}"
            )
            st.caption(r.description)
            sample = json.loads(r.sample) if isinstance(r.sample, str) and r.sample else []
            if sample:
                shown = (
                    f"first {len(sample)} of {count}"
                    if len(sample) < (r.violation_count or 0)
                    else count
                )
                st.dataframe(pd.DataFrame(sample), hide_index=True, width="stretch")
                st.caption(
                    f"Showing {shown}. To list every row, run the check's SQL from "
                    "`config/dq_checks.yaml` in the *Explore & SQL* tab."
                )


def filters(
    countries: pd.DataFrame, indicators: pd.DataFrame, years: list[int]
) -> tuple[pd.Series, list[str], int, int, list[str]]:
    c1, c3, c4 = st.columns([4, 3, 3])
    names = dict(zip(indicators["indicator_code"], indicators["name"], strict=True))
    code = c1.selectbox("Indicator", list(names), format_func=names.__getitem__)
    ind = indicators.set_index("indicator_code").loc[code]
    y0, y1 = c3.slider("Years", years[0], years[-1], (years[0], years[-1]))
    groups_present = [g for g in GROUP_ORDER if g in set(q("income_group_ids"))]
    groups = (
        c4.pills("Income groups", groups_present, selection_mode="multi", default=groups_present)
        or []
    )
    country_names = dict(zip(countries["country_iso3"], countries["country_name"], strict=True))
    focus = list(countries.loc[countries["is_focus"], "country_iso3"]) or list(country_names)
    iso3 = st.multiselect(
        f"Countries ({len(country_names)} in scope; the brief's focus countries are preselected)",
        list(country_names),
        default=focus,
        format_func=country_names.__getitem__,
    )
    ind = ind.copy()
    ind["indicator_code"] = code
    return ind, iso3, int(y0), int(y1), list(groups)


def capped(iso3: list[str], limit: int, what: str) -> list[str]:
    if len(iso3) > limit:
        st.info(f"Showing the first {limit} of {len(iso3)} selected countries as {what}; "
                "narrow the selection to see others.")  # fmt: skip
    return iso3[:limit]


def tab_trends(ind: pd.Series, iso3: list[str], y0: int, y1: int) -> None:
    iso3 = capped(iso3, MAX_PANELS, "panels")
    df = q("values", ind["indicator_code"], tuple(iso3), y0, y1)
    c1, c2 = st.columns([3, 2])
    mode = c1.segmented_control(
        "Scale", ["Values", "Indexed (first year = 100)"], default="Values", key="trend_scale"
    )
    shared = c2.toggle("Shared y-axis", value=False, help="Off: each panel uses its own scale")
    if mode and mode.startswith("Indexed"):
        base = (
            df.dropna(subset=["value"]).sort_values("year").groupby("country_iso3")["value"].first()
        )
        df = df.assign(plot=df["value"] / df["country_iso3"].map(base) * 100)
        y_title, y_axis = "Index (first year = 100)", alt.Axis(format="~f")
    else:
        df = df.assign(plot=df["value"])
        y_title, y_axis = ind["name"], alt.Axis(labelExpr=COMPACT_AXIS)
    df = df.assign(label=df["value"].map(fmt), status=df["value_status"].str.replace("_", " "))
    color = palette()["series"][0]
    base_chart = alt.Chart(df).encode(
        x=alt.X("year:O", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("plot:Q", title=None, axis=y_axis, scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("country_name:N", title="Country"),
            alt.Tooltip("year:O", title="Year"),
            alt.Tooltip("label:N", title=ind["short_name"]),
            alt.Tooltip("status:N", title="Status"),
        ],
    )
    chart = (
        (
            base_chart.mark_line(color=color, strokeWidth=2)
            + base_chart.mark_point(color=color, filled=True, size=40)
        )
        .properties(width=220, height=140)
        .facet(
            facet=alt.Facet(
                "country_name:N",
                title=None,
                sort=list(df.drop_duplicates("country_iso3")["country_name"]),
            ),
            columns=3,
        )
        .resolve_scale(y="shared" if shared else "independent")
    )
    st.caption(y_title)
    st.altair_chart(chart, width="content")
    gaps = int((df["value_status"] != "present").sum())
    if gaps:
        st.info(
            f"{gaps} country-year value(s) are missing in this selection; lines break at gaps "
            "rather than bridging them. See *Data quality & lineage* for the reasons."
        )


def tab_compare(ind: pd.Series, iso3: list[str], years: list[int]) -> None:
    latest = "Latest year all selected countries have"
    choice = st.selectbox("Year", [latest, *reversed(years)], key="compare_year")
    year, df = q(
        "comparison", ind["indicator_code"], tuple(iso3), None if choice == latest else choice
    )
    if year is None or df.empty:
        st.warning("No year in the range has a value for every selected country.")
        return
    st.caption(f"{ind['name']} · {year} · ranked within the selection")
    missing = df[df["value"].isna()]
    if len(df) > MAX_ROWS:
        st.info(f"Showing the top {MAX_ROWS} of {len(df)} selected countries.")
        df = df.head(MAX_ROWS)
    df = df.assign(label=df["value"].map(fmt))
    bars = alt.Chart(df).encode(
        y=alt.Y("country_name:N", sort="-x", title=None),
        x=alt.X("value:Q", title=None, axis=alt.Axis(labelExpr=COMPACT_AXIS)),
        tooltip=[
            alt.Tooltip("country_name:N", title="Country"),
            alt.Tooltip("label:N", title="Value"),
            alt.Tooltip("rank:Q", title="Rank"),
            alt.Tooltip("pct_of_selected_total:Q", title="% of selected total", format=".1f"),
        ],
    )
    chart = (
        bars.mark_bar(color=palette()["series"][0], cornerRadiusEnd=4, height=16)
        + bars.mark_text(align="left", dx=6, color=palette()["muted"]).encode(text="label:N")
    ).properties(height=max(120, 30 * len(df)))
    st.altair_chart(chart, width="stretch")
    if len(missing):
        st.info("No value in this year for: " + ", ".join(missing["country_name"]))


def tab_yoy(ind: pd.Series, iso3: list[str], y0: int, y1: int, years: list[int]) -> None:
    measure = st.segmented_control(
        "Measure",
        ["Strict YoY %", "Annualised vs last available %"],
        default="Strict YoY %",
        key="yoy_measure",
        help="Strict compares with exactly the previous year (empty when it is missing). "
        "Annualised compares with the last year that has a value, as a compound annual rate.",
    )
    field = (
        "annualized_pct_change_since_last_available"
        if measure and measure.startswith("Annualised")
        else "yoy_pct_change"
    )
    iso3 = capped(iso3, MAX_ROWS, "heatmap rows")
    df = q("yoy", ind["indicator_code"], tuple(iso3), max(y0, years[0] + 1), y1)
    if df.empty:
        st.info(f"Pick a range after {years[0]}: its prior year is outside the configured range.")
        return
    df = df.assign(
        pct=df[field],
        value_label=df["value"].map(fmt),
        prior_label=df["prior_value"].map(fmt),
        pct_label=df[field].map(lambda v: "–" if pd.isna(v) else f"{v:+.2f}%"),
        status=df["yoy_status"].str.replace("_", " "),
    )
    scale = float(df["pct"].abs().max() or 1.0)
    edges = [e * scale for e in (-0.7, -0.4, -0.1, 0.1, 0.4, 0.7)]
    order = list(dict.fromkeys(df["country_name"]))
    enc = {
        "x": alt.X("year:O", title=None, axis=alt.Axis(labelAngle=0, orient="top")),
        "y": alt.Y("country_name:N", title=None, sort=order),
        "tooltip": [
            alt.Tooltip("country_name:N", title="Country"),
            alt.Tooltip("year:O", title="Year"),
            alt.Tooltip("pct_label:N", title="Change"),
            alt.Tooltip("value_label:N", title="Value"),
            alt.Tooltip("prior_label:N", title="Prior year"),
            alt.Tooltip("status:N", title="Status"),
        ],
    }
    cells = (
        alt.Chart(df[df["pct"].notna()])
        .mark_rect(cornerRadius=2)
        .encode(
            color=alt.Color(
                "pct:Q",
                title="% change",
                scale=alt.Scale(type="threshold", domain=edges, range=palette()["diverging"]),
                legend=alt.Legend(orient="bottom", format="+.2~f"),
            ),
            **enc,
        )
    )
    empty = (
        alt.Chart(df[df["pct"].isna()])
        .mark_rect(filled=False, strokeDash=[3, 2], stroke=palette()["muted"], cornerRadius=2)
        .encode(**enc)
    )
    st.altair_chart(
        (cells + empty).properties(height=alt.Step(28)).configure_scale(bandPaddingInner=0.08),
        width="stretch",
    )
    st.caption(
        "Colour is relative to this indicator's range in the selection. "
        "Dashed cells could not be computed; hover for the reason."
    )


def tab_income(ind: pd.Series, groups: list[str], y0: int, y1: int) -> None:
    if not groups:
        st.info("Select at least one income group.")
        return
    agg_label = {
        "sum": "Sum",
        "ratio": "Ratio of group totals",
        "weighted_mean": "Weighted mean",
        "mean": "Mean",
    }.get(ind["aggregation"], ind["aggregation"])
    measure = st.segmented_control(
        "Measure",
        [f"Configured: {agg_label}", "Simple mean"],
        default=f"Configured: {agg_label}",
        key="income_measure",
    )
    field = "simple_mean" if measure == "Simple mean" else "aggregate_value"
    df = q("income_groups", ind["indicator_code"], tuple(groups), y0, y1)
    df = df.assign(
        label=df[field].map(fmt),
        group=df["income_group_id"] + " · " + df["income_group_name"],
        members=df["countries"].str.replace(";", ", "),
        complete=df["is_complete"].map({True: "yes", False: "no"}),
    )
    present = [g for g in GROUP_ORDER if g in set(df["income_group_id"])]
    names = df.drop_duplicates("income_group_id").set_index("income_group_id")["group"]
    chart = (
        alt.Chart(df)
        .mark_line(point=alt.OverlayMarkDef(filled=True, size=50), strokeWidth=2)
        .encode(
            x=alt.X("year:O", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y(
                f"{field}:Q",
                title=None,
                axis=alt.Axis(labelExpr=COMPACT_AXIS),
                scale=alt.Scale(zero=False),
            ),
            color=alt.Color(
                "group:N",
                title=None,
                legend=alt.Legend(orient="top", labelLimit=0),
                scale=alt.Scale(
                    domain=[names[g] for g in present],
                    range=[palette()["series"][GROUP_ORDER.index(g)] for g in present],
                ),
            ),
            tooltip=[
                alt.Tooltip("group:N", title="Group"),
                alt.Tooltip("year:O", title="Year"),
                alt.Tooltip("label:N", title="Value"),
                alt.Tooltip("members:N", title="Members"),
                alt.Tooltip("complete:N", title="All members contributed"),
            ],
        )
        .properties(height=360)
    )
    st.altair_chart(chart, width="stretch")
    if not df["is_complete"].all():
        st.warning(
            "Some group-years are incomplete (a member had no value); their aggregate covers "
            "fewer countries and is not comparable with other years."
        )
    st.caption(
        "Aggregated over every economy in the warehouse in each group, not only the selected "
        "countries. Each year uses that year's World Bank classification (historical workbook); "
        "years it does not cover yet use the current classification, so membership can change."
    )


def tab_quality(ind: pd.Series, iso3: list[str], y0: int, y1: int, countries: pd.DataFrame) -> None:
    dq = q("dq_latest")
    st.markdown("##### Latest data-quality checks")
    if dq.empty:
        st.warning("Data-quality checks have not run for this build (`wbpipe check`).")
    st.dataframe(
        dq.assign(status=dq["status"].map(lambda s: f"{STATUS_ICON.get(s, '')} {s}")).drop(
            columns=["sample"]
        ),
        hide_index=True,
        width="stretch",
        column_config={"description": st.column_config.TextColumn(width="large")},
    )
    for _, row in dq[dq["status"] != "pass"].iterrows():
        with st.expander(f"{STATUS_ICON[row['status']]} {row['check_name']}: sample rows"):
            st.json(row["sample"] or "[]")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### Completeness")
        comp = q("completeness", tuple(iso3))
        if len(comp):
            matrix = comp.assign(
                cell=comp.apply(
                    lambda r: (
                        f"{'✓' if r.completeness_status == 'complete' else '!'} "
                        f"{r.years_with_value}/{r.years_expected}"
                    ),
                    axis=1,
                )
            ).pivot(index="country_name", columns="indicator", values="cell")
            st.dataframe(matrix, width="stretch")
        miss = q("missing", tuple(iso3), y0, y1)
        if miss.empty:
            st.success("No missing values in this selection.")
        else:
            st.dataframe(miss, hide_index=True, width="stretch")
    with c2:
        st.markdown("##### Source revisions")
        rev = q("revisions", tuple(iso3))
        if rev.empty:
            st.caption("No source revisions detected across ingest runs.")
        else:
            st.dataframe(rev, hide_index=True, width="stretch")
        st.markdown("##### Pipeline runs")
        st.dataframe(q("runs"), hide_index=True, width="stretch", height=220)

    st.markdown("##### Trace a value to its source")
    names = dict(zip(countries["country_iso3"], countries["country_name"], strict=True))
    l1, l2 = st.columns(2)
    who = l1.selectbox("Country", iso3, format_func=names.__getitem__, key="lineage_country")
    when = l2.selectbox("Year", list(range(y1, y0 - 1, -1)), key="lineage_year")
    info = q("lineage", who, ind["indicator_code"], when) if who else None
    if info is None:
        return
    m1, m2, m3 = st.columns(3)
    m1.metric(ind["short_name"], fmt(info["value"]))
    m2.metric("Status", str(info["value_status"]).replace("_", " "))
    m3.metric(
        "Source last updated",
        "–"
        if pd.isna(info["source_lastupdated"])
        else f"{pd.Timestamp(info['source_lastupdated']):%Y-%m-%d}",
    )
    if info["response_id"]:
        st.markdown(
            f"Response `{info['response_id']}` · page {info['page']} · "
            f"first fetched {info['first_fetched_at']:%Y-%m-%d %H:%M} · "
            f"sha256 `{str(info['payload_sha256'])[:16]}…`"
        )
        st.code(info["request_url"], language="text")
        st.caption(f"Landing file: data/landing/{info['landing_path']}")
        st.json(info["source_row"] or "{}")
    else:
        st.info("The API did not return this value, so there is no source response to trace.")


def tab_explore(iso3: list[str], y0: int, y1: int, indicators: pd.DataFrame) -> None:
    wide = st.toggle("One column per indicator", value=True)
    frames = [
        q("values", code, tuple(iso3), y0, y1).assign(indicator=short)
        for code, short in zip(indicators["indicator_code"], indicators["short_name"], strict=True)
    ]
    df = pd.concat(frames, ignore_index=True)
    if wide:
        df = df.pivot_table(
            index=["country_name", "year"],
            columns="indicator",
            values="value",
            aggfunc="first",
            dropna=False,
        ).reset_index()
        df = df[["country_name", "year", *[s for s in indicators["short_name"] if s in df.columns]]]
    else:
        df = df[["country_name", "indicator", "year", "value", "value_status", "response_id"]]
    st.dataframe(df, hide_index=True, width="stretch", height=360)
    st.download_button(
        "Download CSV", df.to_csv(index=False), "world_bank_selection.csv", "text/csv"
    )

    st.markdown("##### Query the warehouse (read-only)")
    st.caption(
        "Schemas: `marts` (analyst views), `core`, `staging`, `raw`, `quality`, `config`. "
        f"Results are capped at {d.MAX_SQL_ROWS:,} rows; writes are rejected."
    )
    sql = st.text_area(
        "SQL",
        height=120,
        label_visibility="collapsed",
        value="SELECT indicator, country_name, latest_common_year, value_at_common_year,\n"
        "       rank_at_common_year\n"
        "FROM marts.v_latest_comparison\nORDER BY indicator, rank_at_common_year",
    )
    if st.button("Run query", type="primary"):
        try:
            st.dataframe(d.run_sql(warehouse(), sql), hide_index=True, width="stretch")
        except duckdb.Error as exc:
            st.error(f"{type(exc).__name__}: {exc}")


def main() -> None:
    st.set_page_config(page_title="World Bank Indicators", layout="wide")
    try:
        status = q("status")
        countries = q("countries")
        indicators = q("indicators")
        years = q("years")
    except d.WarehouseUnavailable as exc:
        st.title("World Bank indicators")
        st.warning(str(exc))
        st.button("Build it now", on_click=refresh_pipeline, type="primary")
        return
    status_bar(status)
    ind, iso3, y0, y1, groups = filters(countries, indicators, years)
    if not iso3:
        st.info("Select at least one country.")
        return
    if isinstance(ind.get("source_note"), str):
        with st.expander(f"About: {ind['name']}"):
            st.write(ind["source_note"])
    tabs = st.tabs(
        [
            "Trends",
            "Compare",
            "Year-over-year",
            "Income groups",
            "Data quality & lineage",
            "Explore & SQL",
        ]
    )
    try:
        with tabs[0]:
            tab_trends(ind, iso3, y0, y1)
        with tabs[1]:
            tab_compare(ind, iso3, years)
        with tabs[2]:
            tab_yoy(ind, iso3, y0, y1, years)
        with tabs[3]:
            tab_income(ind, groups, y0, y1)
        with tabs[4]:
            tab_quality(ind, iso3, y0, y1, countries)
        with tabs[5]:
            tab_explore(iso3, y0, y1, indicators)
    except d.WarehouseUnavailable as exc:
        st.warning(str(exc))


main()
