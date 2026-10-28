"""Render a self-contained static HTML dashboard from the marts.

One file, no external requests: charts are inline SVG generated here, styling is inline CSS
with light and dark themes, and a few lines of inline JavaScript add hover/focus tooltips.
Every chart also has a data table, so nothing depends on colour or hovering.
"""

from __future__ import annotations

import html
import json
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from wbpipe.config import Settings
from wbpipe.report import assert_quality_gate
from wbpipe.warehouse import connect

log = logging.getLogger(__name__)

OUTPUT_NAME = "dashboard.html"
# Income groups get categorical slots in a fixed order (high -> low), never by rank in a chart.
GROUP_SLOT = {"HIC": 1, "UMC": 2, "LMC": 3, "LIC": 4}
YOY_BINS = ("d3", "d2", "d1", "n", "u1", "u2", "u3")


# ---- formatting ------------------------------------------------------------------------------


def fmt(value: float | None, digits: int = 3) -> str:
    """Compact human number: 1.45B, 29.3T, 86.2K, 79.3."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= threshold:
            return f"{_sig(value / threshold, digits)}{suffix}"
    return _sig(value, digits)


def _sig(value: float, digits: int) -> str:
    if value == 0:
        return "0"
    decimals = max(0, digits - 1 - math.floor(math.log10(abs(value))))
    return f"{value:,.{decimals}f}"


def fmt_pct(value: float | None) -> str:
    return "–" if value is None else f"{value:+.2f}%"


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def tip(title: str, rows: Sequence[tuple[str, str]]) -> str:
    """Tooltip payload as data attributes; the script inserts it with textContent."""
    return (
        f' tabindex="0" data-tip-title="{esc(title)}" '
        f'data-tip-rows="{esc(json.dumps([list(r) for r in rows]))}"'
    )


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    start = math.floor(lo / step) * step
    ticks = []
    value = start
    while value <= hi + step * 1e-9:
        ticks.append(round(value, 10))
        value += step
    if ticks[-1] < hi:
        ticks.append(ticks[-1] + step)
    return ticks


# ---- data ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IndicatorInfo:
    code: str
    short_name: str
    name: str
    aggregation: str


def rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    # execute() (not the relation API): DuckDB 2.x refuses con.sql() while an earlier
    # execute() result on the connection is only partly fetched.
    cur = con.execute(sql)
    columns = [d[0] for d in cur.description or []]
    return [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]


def load(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    return {
        "indicators": [
            IndicatorInfo(
                r["indicator_code"],
                r["short_name"],
                r["indicator_name"] or r["short_name"],
                r["aggregation"],
            )
            for r in rows(
                con,
                """
                SELECT d.indicator_code, d.short_name, d.indicator_name, c.aggregation
                FROM core.dim_indicator d JOIN config.indicator c USING (indicator_code)
                ORDER BY d.config_order""",
            )
        ],
        "countries": rows(
            con, "SELECT country_iso3, country_name FROM core.dim_country ORDER BY config_order"
        ),
        "years": [r["year"] for r in rows(con, "SELECT year FROM core.dim_year ORDER BY year")],
        "freshness": rows(con, "SELECT * FROM marts.v_data_freshness ORDER BY indicator_code"),
        "coverage": rows(
            con,
            """
            SELECT count(*) AS expected, count(*) FILTER (WHERE has_value) AS present,
                   count(*) FILTER (WHERE value_status = 'null_in_source') AS null_in_source,
                   count(*) FILTER (WHERE value_status = 'not_returned') AS not_returned
            FROM marts.v_indicator_value""",
        )[0],
        "dq": rows(
            con,
            """
            SELECT check_name, severity, status, violation_count, description
            FROM quality.dq_result
            WHERE checked_at = (SELECT max(checked_at) FROM quality.dq_result)
            ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'fail' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END,
                     check_name""",
        ),
        "income": rows(con, "SELECT * FROM marts.v_income_group_by_year"),
        "latest": rows(con, "SELECT * FROM marts.v_latest_comparison"),
        "yoy": rows(con, "SELECT * FROM marts.v_yoy_change"),
        "completeness": rows(con, "SELECT * FROM marts.v_series_completeness"),
        "missing": rows(con, "SELECT * FROM marts.v_missing_values ORDER BY ALL"),
    }


# ---- charts ----------------------------------------------------------------------------------


def line_chart(chart_id: str, years: list[int], series: list[dict[str, Any]], label: str) -> str:
    """Small line chart; series = [{name, slot, values: {year: value|None}}]."""
    w, h = 360, 220
    ml, mr, mt, mb = 52, 64, 12, 28
    values = [v for s in series for v in s["values"].values() if v is not None]
    if not values:
        return '<p class="empty">No values to plot.</p>'
    lo, hi = min(values), max(values)
    if lo > 0 and lo / hi < 0.5:
        lo = 0.0  # magnitudes far apart: anchor at zero
    ticks = nice_ticks(lo, hi)
    y0, y1 = ticks[0], ticks[-1]
    x_of = {y: ml + (w - ml - mr) * (i / max(1, len(years) - 1)) for i, y in enumerate(years)}

    def y_of(v: float) -> float:
        return mt + (h - mt - mb) * (1 - (v - y0) / (y1 - y0))

    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}" class="chart">']
    for t in ticks:
        y = y_of(t)
        out.append(f'<line class="gridline" x1="{ml}" x2="{w - mr}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(
            f'<text class="tick" x="{ml - 6}" y="{y + 3.5:.1f}" text-anchor="end">{fmt(t, 2)}</text>'
        )
    for yr in years:
        out.append(
            f'<text class="tick" x="{x_of[yr]:.1f}" y="{h - 8}" text-anchor="middle">{yr}</text>'
        )
    end_labels: list[list[Any]] = []  # [y, name, slot]
    for s in series:
        pts = [(x_of[yr], y_of(v)) for yr in years if (v := s["values"].get(yr)) is not None]
        # break the line at gaps rather than bridging missing years
        segments: list[list[tuple[float, float]]] = [[]]
        for yr in years:
            v = s["values"].get(yr)
            if v is None:
                segments.append([])
            else:
                segments[-1].append((x_of[yr], y_of(v)))
        for seg in segments:
            if len(seg) > 1:
                d = " ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
                out.append(f'<polyline class="line s{s["slot"]}" points="{d}"/>')
            elif len(seg) == 1:
                out.append(
                    f'<circle class="dot s{s["slot"]}" cx="{seg[0][0]:.1f}" cy="{seg[0][1]:.1f}" r="4"/>'
                )
        if pts:
            end_labels.append([pts[-1][1], s["name"], s["slot"]])
    # direct labels at the line ends, nudged apart so they never overlap
    end_labels.sort()
    for i in range(1, len(end_labels)):
        end_labels[i][0] = max(end_labels[i][0], end_labels[i - 1][0] + 12)
    overflow = end_labels[-1][0] - (h - mb) if end_labels else 0
    if overflow > 0:  # pushed past the baseline: shift the stack back up
        for entry in end_labels:
            entry[0] -= overflow
    for y, name, slot in end_labels:
        out.append(
            f'<line class="key s{slot}" x1="{w - mr + 6}" x2="{w - mr + 14}" y1="{y:.1f}" y2="{y:.1f}"/>'
        )
        out.append(f'<text class="direct" x="{w - mr + 17}" y="{y + 3.5:.1f}">{esc(name)}</text>')
    # hover layer: one column per year, crosshair + every series' value
    step = (w - ml - mr) / max(1, len(years) - 1)
    for yr in years:
        x = x_of[yr]
        tip_rows = [(s["name"], fmt(s["values"].get(yr))) for s in series]
        out.append(
            f'<line class="crosshair" id="{chart_id}-x{yr}" x1="{x:.1f}" x2="{x:.1f}" '
            f'y1="{mt}" y2="{h - mb}"/>'
        )
        out.append(
            f'<rect class="hit" x="{x - step / 2:.1f}" y="{mt}" width="{step:.1f}" '
            f'height="{h - mt - mb}" data-crosshair="{chart_id}-x{yr}"'
            f"{tip(f'{label} · {yr}', tip_rows)}/>"
        )
    out.append("</svg>")
    return "".join(out)


def bar_chart(label: str, bars: list[dict[str, Any]]) -> str:
    """Horizontal bars, sorted; bars = [{name, value, note}]."""
    bars = [b for b in bars if b["value"] is not None]
    if not bars:
        return '<p class="empty">No year in which every country has a value.</p>'
    w, row, mt = 360, 24, 4
    ml, mr = 104, 56
    h = mt + row * len(bars) + 4
    top = max(b["value"] for b in bars) or 1
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}" class="chart">']
    out.append(f'<line class="axis" x1="{ml}" x2="{ml}" y1="{mt}" y2="{h - 4}"/>')
    for i, b in enumerate(sorted(bars, key=lambda b: -b["value"])):
        y = mt + i * row
        bw = max(1.0, (w - ml - mr) * b["value"] / top)
        out.append(
            f'<text class="cat" x="{ml - 8}" y="{y + row / 2 + 4:.1f}" text-anchor="end">{esc(b["name"])}</text>'
        )
        out.append(
            f'<path class="bar s1" d="M{ml},{y + 5} h{bw - 4:.1f} a4,4 0 0 1 4,4 v{row - 18} '
            f'a4,4 0 0 1 -4,4 h{-(bw - 4):.1f} z"/>'
        )
        out.append(
            f'<text class="val" x="{ml + bw + 6:.1f}" y="{y + row / 2 + 4:.1f}">{fmt(b["value"])}</text>'
        )
        out.append(
            f'<rect class="hit" x="0" y="{y}" width="{w}" height="{row}"'
            f"{tip(b['name'], [(label, fmt(b['value'])), *b.get('extra', [])])}/>"
        )
    out.append("</svg>")
    return "".join(out)


def yoy_bin(pct: float, scale: float) -> str:
    """Diverging class for a YoY %: three steps per sign, relative to the indicator's range."""
    r = pct / scale if scale else 0.0
    if abs(r) < 0.1:
        return "n"
    level = 1 if abs(r) < 0.4 else 2 if abs(r) < 0.7 else 3
    return f"{'u' if r > 0 else 'd'}{level}"


def heatmap(label: str, countries: list[dict[str, Any]], years: list[int],
            cells: dict[tuple[str, int], dict[str, Any]], scale: float) -> str:  # fmt: skip
    w = 360
    ml, mt, cell_h = 104, 18, 22
    cell_w = (w - ml) / max(1, len(years))
    h = mt + cell_h * len(countries) + 2
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}" class="chart">']
    for j, yr in enumerate(years):
        out.append(
            f'<text class="tick" x="{ml + (j + 0.5) * cell_w:.1f}" y="12" text-anchor="middle">{yr}</text>'
        )
    for i, c in enumerate(countries):
        y = mt + i * cell_h
        out.append(
            f'<text class="cat" x="{ml - 8}" y="{y + cell_h / 2 + 4:.1f}" text-anchor="end">{esc(c["country_name"] or c["country_iso3"])}</text>'
        )
        for j, yr in enumerate(years):
            cell = cells.get((c["country_iso3"], yr), {})
            pct = cell.get("yoy_pct_change")
            css = f"heat {yoy_bin(pct, scale)}" if pct is not None else "heat missing"
            x = ml + j * cell_w
            out.append(
                f'<rect class="{css}" x="{x + 1:.1f}" y="{y + 1}" width="{cell_w - 2:.1f}" '
                f'height="{cell_h - 2}" rx="2"'
                + tip(
                    f"{c['country_name'] or c['country_iso3']} · {yr}",
                    [
                        ("YoY change", fmt_pct(pct)),
                        ("Value", fmt(cell.get("value"))),
                        ("Prior year", fmt(cell.get("prior_value"))),
                        ("Status", str(cell.get("yoy_status", "–")).replace("_", " ")),
                    ],
                )
                + "/>"
            )
    out.append("</svg>")
    return "".join(out)


def heat_legend(scale: float) -> str:
    """Stepped scale: seven swatches with the boundary values between them."""
    edges = [-0.7, -0.4, -0.1, 0.1, 0.4, 0.7]
    sw, h = 42, 30
    out = [f'<svg class="scale" viewBox="0 0 {sw * 8 + 100} {h}" role="img" '
           f'aria-label="Colour scale for year-over-year change">']  # fmt: skip
    for k, b in enumerate(YOY_BINS):
        out.append(
            f'<rect class="heat {b}" x="{k * sw + 1}" y="2" width="{sw - 2}" height="12" rx="2"/>'
        )
    for k, e in enumerate(edges, start=1):
        out.append(
            f'<text class="tick" x="{k * sw}" y="{h - 3}" text-anchor="middle">{e * scale:+.2g}</text>'
        )
    x = sw * 7 + 14
    out.append(f'<rect class="heat missing" x="{x}" y="2" width="{sw - 2}" height="12" rx="2"/>')
    out.append(f'<text class="tick" x="{x + sw + 4}" y="12">not computable</text>')
    out.append("</svg>")
    return f'<div class="scalewrap">{"".join(out)}</div>'


def table(
    headers: Sequence[str], body: Iterable[Sequence[Any]], numeric: set[int] | None = None
) -> str:
    numeric = numeric or set()

    def cls(i: int) -> str:
        return ' class="num"' if i in numeric else ""

    head = "".join(f"<th{cls(i)}>{esc(h)}</th>" for i, h in enumerate(headers))
    trs = "".join(
        "<tr>" + "".join(f"<td{cls(i)}>{esc(v)}</td>" for i, v in enumerate(r)) + "</tr>"
        for r in body
    )
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table></div>'


def details(summary: str, content: str) -> str:
    return f"<details><summary>{esc(summary)}</summary>{content}</details>"


# ---- page ------------------------------------------------------------------------------------


STATUS_ICON = {"pass": "✓", "warn": "!", "fail": "✕", "error": "✕"}


def render(data: dict[str, Any]) -> str:
    inds: list[IndicatorInfo] = data["indicators"]
    years: list[int] = data["years"]
    countries = data["countries"]
    name_of = {c["country_iso3"]: c["country_name"] or c["country_iso3"] for c in countries}
    fresh = data["freshness"]
    cov = data["coverage"]
    dq = data["dq"]
    dq_counts = {s: sum(r["status"] == s for r in dq) for s in ("pass", "warn", "fail", "error")}
    dq_state = (
        "critical"
        if dq_counts["fail"] or dq_counts["error"]
        else "warning"
        if dq_counts["warn"]
        else "good"
    )
    run_id = fresh[0]["ingest_run_id"] if fresh else "–"
    lastupdated = max(
        (str(r["source_lastupdated"]) for r in fresh if r["source_lastupdated"]), default="–"
    )
    fetched = fresh[0]["fetched_at"] if fresh else None

    sections = []

    # -- overview tiles
    tiles = f"""
    <section class="tiles" aria-label="Overview">
      <div class="tile"><div class="tile-label">Coverage</div>
        <div class="tile-value">{cov["present"]} / {cov["expected"]}</div>
        <div class="tile-note">country × indicator × year values present
          ({cov["null_in_source"]} null in source, {cov["not_returned"]} not returned)</div></div>
      <div class="tile"><div class="tile-label">Data quality</div>
        <div class="tile-value"><span class="status {dq_state}">{STATUS_ICON["pass" if dq_state == "good" else "warn" if dq_state == "warning" else "fail"]}</span>
          {dq_counts["pass"]}/{len(dq)} pass</div>
        <div class="tile-note">checks passing · {dq_counts["warn"]} warning(s), {dq_counts["fail"] + dq_counts["error"]} blocking</div></div>
      <div class="tile"><div class="tile-label">Source last updated</div>
        <div class="tile-value">{esc(lastupdated)}</div>
        <div class="tile-note">World Bank WDI · fetched {esc(f"{fetched:%Y-%m-%d %H:%M} UTC" if fetched else "–")}</div></div>
      <div class="tile"><div class="tile-label">Scope</div>
        <div class="tile-value">{len(countries)} × {len(inds)} × {len(years)}</div>
        <div class="tile-note">countries × indicators × years ({years[0]}–{years[-1]})</div></div>
    </section>"""  # fmt: skip
    sections.append(tiles)

    # -- Q4 income groups over time
    groups = sorted(
        {(r["income_group_id"], r["income_group_name"], r["income_group_rank"]) for r in data["income"]
         if r["income_group_id"]},
        key=lambda g: -(g[2] or 0),
    )  # fmt: skip
    cards, table_rows = [], []
    for ind in inds:
        series = []
        for gid, _name, _rank in groups:
            vals = {r["year"]: r["aggregate_value"] for r in data["income"]
                    if r["indicator_code"] == ind.code and r["income_group_id"] == gid}  # fmt: skip
            series.append({"name": gid, "slot": GROUP_SLOT.get(gid, 5), "values": vals})
        cards.append(
            f'<figure class="card"><figcaption><strong>{esc(ind.name)}</strong>'
            f'<span class="sub">{esc(AGG_TEXT.get(ind.aggregation, ind.aggregation))}</span></figcaption>'
            f"{line_chart('inc-' + ind.short_name, years, series, ind.name)}</figure>"
        )
    for r in sorted(
        data["income"],
        key=lambda r: (r["indicator_code"], r["year"], -(r["income_group_rank"] or 0)),
    ):
        table_rows.append([r["indicator"], r["year"], r["income_group_name"], fmt(r["aggregate_value"]),
                           fmt(r["simple_mean"]), f'{r["countries_in_aggregate"]}/{r["countries_in_group"]}',
                           "yes" if r["is_complete"] else "no"])  # fmt: skip
    legend = "".join(
        f'<span class="lg"><svg width="16" height="10" aria-hidden="true"><line class="key s{GROUP_SLOT.get(g, 5)}" '
        f'x1="1" x2="15" y1="5" y2="5"/></svg>{esc(g)} · {esc(n)}</span>'
        for g, n, _ in groups
    )
    members = "; ".join(
        f"{g}: " + ", ".join(sorted({name_of[c] for r in data["income"] if r["income_group_id"] == g
                                       for c in (r["countries"] or "").split(";") if c in name_of}))
        for g, _, _ in groups
    )  # fmt: skip
    sections.append(f"""
    <section><h2>Income groups over time</h2>
      <p class="lede">Each indicator combined across the selected countries in each income group, using the
        aggregation configured for it. Groups use the <em>current</em> World Bank classification for every year.
        Members — {esc(members)}.</p>
      <div class="legend">{legend}</div>
      <div class="grid">{"".join(cards)}</div>
      {details("Show data table", table(["Indicator", "Year", "Income group", "Aggregate", "Simple mean", "Countries", "Complete"], table_rows, {3, 4}))}
    </section>""")  # fmt: skip

    # -- Q3 latest comparison
    cards, table_rows = [], []
    for ind in inds:
        rows_ = [r for r in data["latest"] if r["indicator_code"] == ind.code]
        common = rows_[0]["latest_common_year"] if rows_ else None
        bars = [{"name": r["country_name"] or r["country_iso3"], "value": r["value_at_common_year"],
                 "extra": [("Rank", str(r["rank_at_common_year"])),
                           ("Share of selected", f'{r["pct_of_selected_total_at_common_year"]:.1f}%'
                            if r["pct_of_selected_total_at_common_year"] is not None else "–")]}
                for r in rows_]  # fmt: skip
        later = [
            f"{r['country_iso3']} {r['country_latest_year']}"
            for r in rows_
            if not r["is_same_as_common_year"]
        ]
        note = (
            f"Latest year all countries have: {common}"
            if common
            else "No year with values for every country"
        )
        if later:
            note += f" · own latest differs: {', '.join(later)}"
        cards.append(
            f'<figure class="card"><figcaption><strong>{esc(ind.name)}</strong>'
            f'<span class="sub">{esc(note)}</span></figcaption>{bar_chart(ind.name, bars)}</figure>'
        )
        for r in sorted(
            rows_, key=lambda r: (r["rank_at_common_year"] is None, r["rank_at_common_year"] or 0)
        ):
            table_rows.append([ind.short_name, r["country_name"], r["latest_common_year"] or "–",
                               fmt(r["value_at_common_year"]), r["rank_at_common_year"] or "–",
                               r["country_latest_year"] or "–", fmt(r["country_latest_value"])])  # fmt: skip
    sections.append(f"""
    <section><h2>Country comparison at the latest year</h2>
      <p class="lede">Ranked at the most recent year in which every selected country has a value, so all bars
        compare the same year. Where a country has a more recent value of its own, it is noted.</p>
      <div class="grid">{"".join(cards)}</div>
      {details("Show data table", table(["Indicator", "Country", "Common year", "Value", "Rank", "Own latest year", "Own latest value"], table_rows, {3, 6}))}
    </section>""")  # fmt: skip

    # -- Q2 YoY heatmaps
    yoy_years = years[1:]
    cards, table_rows = [], []
    for ind in inds:
        cells = {
            (r["country_iso3"], r["year"]): r
            for r in data["yoy"]
            if r["indicator_code"] == ind.code
        }
        pcts = [abs(r["yoy_pct_change"]) for r in cells.values() if r["yoy_pct_change"] is not None]
        scale = max(pcts) if pcts else 1.0
        cards.append(
            f'<figure class="card"><figcaption><strong>{esc(ind.name)}</strong>'
            f'<span class="sub">Year-over-year change, % · colour relative to this indicator\'s range</span>'
            f"</figcaption>{heatmap(ind.name, countries, yoy_years, cells, scale)}{heat_legend(scale)}</figure>"
        )
        for r in sorted(cells.values(), key=lambda r: (r["country_iso3"], r["year"])):
            if r["year"] in yoy_years:
                table_rows.append([ind.short_name, r["country_name"], r["year"], fmt(r["value"]),
                                   fmt_pct(r["yoy_pct_change"]), r["yoy_status"].replace("_", " ")])  # fmt: skip
    sections.append(f"""
    <section><h2>Year-over-year change</h2>
      <p class="lede">Change against exactly the previous year. Empty dashed cells could not be computed (value or
        prior value missing, or prior value zero); hover or open the table for the reason. {years[0]} is omitted because
        its prior year is outside the configured range.</p>
      <div class="grid">{"".join(cards)}</div>
      {details("Show data table", table(["Indicator", "Country", "Year", "Value", "YoY change", "Status"], table_rows, {3, 4}))}
    </section>""")  # fmt: skip

    # -- Q1 completeness
    comp = {(r["country_iso3"], r["indicator_code"]): r for r in data["completeness"]}
    head = "".join(f'<th class="num">{esc(i.short_name)}</th>' for i in inds)
    body = []
    for c in countries:
        tds = []
        for ind in inds:
            r = comp.get((c["country_iso3"], ind.code))
            if r is None:
                tds.append('<td class="num">–</td>')
                continue
            state = {"complete": "good", "incomplete": "warning", "empty": "critical"}[
                r["completeness_status"]
            ]
            icon = {"good": "✓", "warning": "!", "critical": "✕"}[state]
            missing = f' title="missing: {esc(r["missing_years"])}"' if r["missing_years"] else ""
            tds.append(
                f'<td class="num"{missing}><span class="status {state}" aria-hidden="true">{icon}</span> '
                f'{r["years_with_value"]}/{r["years_expected"]} <span class="sr">{r["completeness_status"]}</span></td>'
            )
        body.append(
            f'<tr><th scope="row">{esc(c["country_name"] or c["country_iso3"])}</th>{"".join(tds)}</tr>'
        )
    missing_rows = [[name_of.get(r["country_iso3"], r["country_iso3"]), r["indicator"], r["year"],
                     r["missing_reason"].replace("_", " "), r["gap_type"].replace("_", " ")]
                    for r in data["missing"]]  # fmt: skip
    missing_html = (
        table(["Country", "Indicator", "Year", "Reason", "Gap type"], missing_rows)
        if missing_rows
        else f'<p class="ok"><span class="status good" aria-hidden="true">✓</span> No missing values: the source '
        f"returned a value for every configured country, indicator and year ({years[0]}–{years[-1]}).</p>"
    )
    sections.append(f"""
    <section><h2>Data completeness</h2>
      <p class="lede">Years with a value out of years expected, per country and indicator. Missing data is kept as an
        explicit status (null in source, or not returned by the API), never as zero.</p>
      <div class="tablewrap"><table class="matrix"><thead><tr><th>Country</th>{head}</tr></thead>
        <tbody>{"".join(body)}</tbody></table></div>
      <h3>Missing combinations</h3>{missing_html}
    </section>""")  # fmt: skip

    # -- data quality
    dq_rows = "".join(
        f'<tr><td><code>{esc(r["check_name"])}</code></td><td>{esc(r["severity"])}</td>'
        f'<td><span class="status {"good" if r["status"] == "pass" else "warning" if r["status"] == "warn" else "critical"}"'
        f' aria-hidden="true">{STATUS_ICON[r["status"]]}</span> {esc(r["status"])}</td>'
        f'<td class="num">{"–" if r["violation_count"] is None else r["violation_count"]}</td>'
        f'<td>{esc(r["description"])}</td></tr>'
        for r in dq
    )  # fmt: skip
    sections.append(f"""
    <section><h2>Data quality</h2>
      <p class="lede">Checks from <code>config/dq_checks.yaml</code> run after every build. A failing check stops
        publication; warnings are reported here.</p>
      <div class="tablewrap"><table><thead><tr><th>Check</th><th>Severity</th><th>Status</th>
        <th class="num">Violations</th><th>What it verifies</th></tr></thead><tbody>{dq_rows}</tbody></table></div>
    </section>""")  # fmt: skip

    return PAGE.format(
        run_id=esc(run_id),
        body="".join(sections),
        css=CSS,
        script=SCRIPT,
    )


AGG_TEXT = {
    "sum": "Sum across group members",
    "ratio": "Ratio of group totals",
    "weighted_mean": "Weighted mean across group members",
    "mean": "Simple mean across group members",
}


def run_dashboard(settings: Settings) -> Path:
    with connect(settings) as con:
        assert_quality_gate(con)
        data = load(con)
    path = settings.paths.output_dir / OUTPUT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    log.info("wrote %s", path.name)
    return path


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>World Bank Indicators</title>
<style>{css}</style>
</head>
<body>
<header class="page-head">
  <h1>World Bank indicators</h1>
  <p class="meta">Generated by <code>wbpipe</code> from ingest run <code>{run_id}</code>.
    Source: World Bank API v2 (World Development Indicators).</p>
</header>
<main>{body}</main>
<footer><p>Every value traces back to the API response it came from (<code>response_id</code> in the warehouse).
  Built from the marts in <code>data/warehouse.duckdb</code>; see the README for definitions.</p></footer>
<div id="tooltip" role="tooltip" hidden></div>
<script>{script}</script>
</body>
</html>
"""

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb; --text-primary: #0b0b0b; --text-secondary: #52514e;
  --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100; --series-5: #e87ba4;
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b; --on-warning: #0b0b0b;
  --d3: #952321; --d2: #d6403e; --d1: #ef8a87; --n: #f0efec; --u1: #86b6ef; --u2: #2a78d6; --u3: #184f95;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #f0efec; --text-secondary: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500; --series-5: #d55181;
    --d3: #f4aaa8; --d2: #d95454; --d1: #8e2f2e; --n: #383835; --u1: #184f95; --u2: #3987e5; --u3: #9ec5f4;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #f0efec; --text-secondary: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500; --series-5: #d55181;
  --d3: #f4aaa8; --d2: #d95454; --d1: #8e2f2e; --n: #383835; --u1: #184f95; --u2: #3987e5; --u3: #9ec5f4;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text-primary);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.page-head, main, footer { max-width: 1200px; margin: 0 auto; padding: 0 16px; }
.page-head { padding-top: 28px; }
h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 19px; margin: 40px 0 6px; }
h3 { font-size: 15px; margin: 20px 0 8px; }
.meta, .lede, footer, .sub, .tile-note { color: var(--text-secondary); }
.meta { margin: 0; font-size: 13px; }
.lede { margin: 0 0 14px; max-width: 75ch; font-size: 14px; }
code { font-size: 0.9em; }
.tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin-top: 20px; }
.tile, .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; }
.tile { padding: 14px 16px; }
.tile-label { font-size: 13px; color: var(--text-secondary); }
.tile-value { font-size: 24px; font-weight: 600; margin: 2px 0; display: flex; align-items: center; gap: 8px; }
.tile-note { font-size: 12.5px; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); }
.card { margin: 0; padding: 12px 12px 8px; min-width: 0; }
figcaption { display: flex; flex-direction: column; gap: 2px; margin-bottom: 6px; font-size: 14px; }
.sub { font-size: 12.5px; }
.chart { width: 100%; height: auto; display: block; overflow: visible; }
.chart text { font-size: 10.5px; fill: var(--text-secondary); font-variant-numeric: tabular-nums; }
.chart .tick { fill: var(--muted); }
.chart .direct, .chart .cat { fill: var(--text-primary); font-size: 11px; }
.chart .val { fill: var(--text-secondary); }
line.gridline { stroke: var(--grid); stroke-width: 1; }
line.axis { stroke: var(--axis); stroke-width: 1; }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.key { stroke-width: 2.5; stroke-linecap: round; }
.dot { stroke: var(--surface-1); stroke-width: 2; }
.s1 { stroke: var(--series-1); } .s2 { stroke: var(--series-2); } .s3 { stroke: var(--series-3); }
.s4 { stroke: var(--series-4); } .s5 { stroke: var(--series-5); }
.dot.s1, .bar.s1 { fill: var(--series-1); } .dot.s2 { fill: var(--series-2); } .dot.s3 { fill: var(--series-3); }
.dot.s4 { fill: var(--series-4); } .dot.s5 { fill: var(--series-5); }
.bar { stroke: none; }
.heat { stroke: none; }
.heat.d3 { fill: var(--d3); } .heat.d2 { fill: var(--d2); } .heat.d1 { fill: var(--d1); } .heat.n { fill: var(--n); }
.heat.n { stroke: var(--axis); stroke-width: 1; }
.heat.u1 { fill: var(--u1); } .heat.u2 { fill: var(--u2); } .heat.u3 { fill: var(--u3); }
.heat.missing { fill: transparent; stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 2; }
.scalewrap { display: flex; align-items: flex-start; gap: 8px; margin-top: 8px; }
.scale { width: 100%; max-width: 440px; height: auto; overflow: visible; }
.scale text { font-size: 10.5px; fill: var(--muted); font-variant-numeric: tabular-nums; }
.crosshair { stroke: var(--axis); stroke-width: 1; visibility: hidden; pointer-events: none; }
.crosshair.on { visibility: visible; }
.hit { fill: transparent; cursor: default; outline: none; }
.hit:focus-visible { stroke: var(--text-primary); stroke-width: 1.5; }
rect.heat[data-tip-title]:hover, rect.heat[data-tip-title]:focus-visible { stroke: var(--text-primary); stroke-width: 1.5; stroke-dasharray: none; outline: none; }
rect.heat.n { stroke: var(--axis); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12.5px; color: var(--text-secondary); margin: 6px 0 10px; }
.lg { display: inline-flex; align-items: center; gap: 6px; }
.status { display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px;
  border-radius: 50%; font-size: 11px; font-weight: 700; color: #fff; flex: none; }
.status.good { background: var(--good); } .status.warning { background: var(--warning); color: var(--on-warning); }
.status.critical { background: var(--critical); }
.tile .status { width: 22px; height: 22px; font-size: 13px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.tablewrap { position: relative; overflow-x: auto; background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 7px 12px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
td:last-child { white-space: normal; }
tbody tr:last-child > * { border-bottom: none; }
th { font-weight: 600; color: var(--text-secondary); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.matrix td .status { width: 16px; height: 16px; font-size: 10px; vertical-align: -3px; }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; padding: 4px 0; }
details .tablewrap { margin-top: 6px; max-height: 420px; overflow: auto; }
.ok { display: flex; gap: 8px; align-items: center; color: var(--text-secondary); font-size: 14px; }
.empty { color: var(--muted); font-size: 13px; }
footer { padding-top: 32px; padding-bottom: 32px; font-size: 12.5px; }
#tooltip { position: fixed; z-index: 10; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12.5px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); max-width: 260px; }
#tooltip .tt-title { color: var(--text-secondary); margin-bottom: 4px; }
#tooltip .tt-row { display: flex; justify-content: space-between; gap: 16px; }
#tooltip .tt-row b { font-variant-numeric: tabular-nums; }
@media (forced-colors: active) { .status { border: 1px solid CanvasText; } }
"""

SCRIPT = """
(function () {
  var tt = document.getElementById('tooltip');
  var activeLine = null;
  function show(el, x, y) {
    tt.replaceChildren();
    var title = document.createElement('div');
    title.className = 'tt-title';
    title.textContent = el.getAttribute('data-tip-title');
    tt.appendChild(title);
    JSON.parse(el.getAttribute('data-tip-rows') || '[]').forEach(function (r) {
      var row = document.createElement('div'); row.className = 'tt-row';
      var k = document.createElement('span'); k.textContent = r[0];
      var v = document.createElement('b'); v.textContent = r[1];
      row.append(k, v); tt.appendChild(row);
    });
    tt.hidden = false;
    var w = tt.offsetWidth, h = tt.offsetHeight;
    var left = Math.min(x + 14, window.innerWidth - w - 8);
    var top = y + 14 + h > window.innerHeight ? y - h - 10 : y + 14;
    tt.style.left = Math.max(8, left) + 'px'; tt.style.top = Math.max(8, top) + 'px';
    var id = el.getAttribute('data-crosshair');
    if (activeLine) activeLine.classList.remove('on');
    activeLine = id ? document.getElementById(id) : null;
    if (activeLine) activeLine.classList.add('on');
  }
  function hide() { tt.hidden = true; if (activeLine) activeLine.classList.remove('on'); activeLine = null; }
  document.addEventListener('pointermove', function (e) {
    var el = e.target.closest && e.target.closest('[data-tip-title]');
    if (el) show(el, e.clientX, e.clientY); else hide();
  });
  document.addEventListener('focusin', function (e) {
    var el = e.target.closest && e.target.closest('[data-tip-title]');
    if (!el) return hide();
    var r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top + r.height / 2);
  });
  document.addEventListener('focusout', hide);
  window.addEventListener('scroll', hide, { passive: true });
})();
"""
