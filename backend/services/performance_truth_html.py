"""The evaluation's dashboard: one static page from the aggregates, no assets, no scripts.

Rendered by services/performance_truth_job.build_outputs into out/index.html
beside latest.json, history.json and nodes/<node>.json. Served on loopback by
deploy/perf-eval/systemd/retina-perf-dashboard.service and read over an SSH
tunnel: it names nodes and towers and shows detection figures, never a receiver
position.

Layout: a fleet table (one row per node and span), then a section per node
with the trend of its three-hour windows, the coverage-gate comparison, the
raw-against-validated figures, and detection probability by class, range,
altitude and bearing with counts and intervals. Every figure shows its
denominator beside it, and a span without enough data shows its status word.
"""

from __future__ import annotations

import html as _html
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from services import performance_truth as pt

SPAN_TITLES = (("latest", "Latest 3 h"), ("day", "24 h"), ("week", "7 d"), ("cumulative", "Since start"))
STATUS_WORDS = {
    "ok": "ok",
    "insufficient": "too few opportunities",
    "no_truth": "no ADS-B truth",
    "node_inactive": "node inactive",
    "no_data": "no data",
    "failed": "failed",
}
FLAG_WORDS = {
    "truth_gaps": "truth gaps",
    "node_partial": "node partly down",
    "background_dominated": "chance matches dominate",
    "tags_refuted": "many tags refuted",
    "ambiguous_geometry": "ambiguous geometry",
    "tags_not_in_truth": "tags not in truth",
}
_CSS = """
:root{--bg:#fff;--fg:#1b1b1b;--muted:#666;--line:#ddd;--ok:#1d7a3a;--warn:#b26a00;--bad:#b3261e;--bar:#3a6ea5;--bar2:#9fb9d6;--raw:#c58b3a}
@media(prefers-color-scheme:dark){:root{--bg:#141618;--fg:#e6e6e6;--muted:#9a9a9a;--line:#333;--bar:#7aa6d6;--bar2:#3d5a7a;--raw:#d9a35a}}
body{font:14px/1.45 system-ui,sans-serif;margin:0 auto;max-width:1200px;padding:16px;background:var(--bg);color:var(--fg)}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 8px;border-top:1px solid var(--line);padding-top:12px}
h3{font-size:15px;margin:16px 0 6px}p.meta{color:var(--muted);margin:0 0 12px}
table{border-collapse:collapse;width:100%;margin:6px 0 10px}th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:right;white-space:nowrap}
th{font-weight:600;color:var(--muted)}td:first-child,th:first-child{text-align:left}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.bar{display:inline-block;height:9px;background:var(--bar);vertical-align:middle}.bar.raw{background:var(--raw)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}
svg text{fill:var(--muted);font-size:10px}details{margin:6px 0}summary{cursor:pointer;color:var(--muted)}
.small{font-size:12px}.note{font-size:12px;color:var(--muted);margin:4px 0 8px}
"""


def esc(value: Any) -> str:
    return _html.escape("" if value is None else str(value))


def pct(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{100 * value:.{digits}f} %"


def ci(boot: Mapping[str, Any] | None) -> str:
    if not boot or boot.get("lo") is None:
        return ""
    return f" <span class=muted>[{100 * boot['lo']:.0f}–{100 * boot['hi']:.0f}]</span>"


def stamp(ms: int | None) -> str:
    return "–" if not ms else datetime.fromtimestamp(ms / 1000.0, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def status_cell(status: str, flags: Mapping[str, int] | Sequence[str] | None = None) -> str:
    cls = "ok" if status == "ok" else ("bad" if status in ("failed",) else "warn")
    words = STATUS_WORDS.get(status, status)
    extra = ""
    if flags:
        names = flags if isinstance(flags, Sequence) and not isinstance(flags, str) else sorted(flags)
        extra = " <span class='muted small'>" + ", ".join(FLAG_WORDS.get(f, f) for f in names) + "</span>"
    return f"<span class={cls}>{esc(words)}</span>{extra}"


def bar(value: float | None, width: int = 80, raw: bool = False) -> str:
    if value is None:
        return ""
    w = max(0, min(width, int(round(width * value))))
    return f"<span class='bar{' raw' if raw else ''}' style='width:{w}px'></span>"


def tower_label(tower: Sequence[Any] | None) -> str:
    if not tower:
        return "?"
    callsign, fc = tower
    mhz = f" {fc / 1e6:.0f} MHz" if isinstance(fc, int | float) else ""
    return f"{callsign}{mhz}"


# ── Fleet ────────────────────────────────────────────────────────────────────


def fleet_table(latest: Mapping[str, Any]) -> str:
    rows = []
    for node_id, node in latest["nodes"].items():
        spans = node["spans"]
        cells = []
        for key, _ in SPAN_TITLES:
            s = spans.get(key)
            if not s:
                cells.append("<td class=muted>no data</td>")
                continue
            if s["status"] != "ok":
                cells.append(
                    f"<td>{status_cell(s['status'], s['flags'])}<br><span class='muted small'>{s['n']:,} opp / {s['encounters']} enc</span></td>"
                )
                continue
            lo, hi = s["validated_ci"]
            interval = f" <span class=muted>[{100 * lo:.0f}–{100 * hi:.0f}]</span>" if lo is not None else ""
            flags = " ".join(f"<span class='warn small'>{esc(FLAG_WORDS.get(f, f))}</span>" for f in sorted(s["flags"]))
            cells.append(
                f"<td><b>{pct(s['validated'])}</b>{interval} {bar(s['validated'], 60)}<br>"
                f"<span class='muted small'>raw {pct(s['raw_pm1'])} · bg {pct(s['background'])} · {s['n']:,} opp / {s['encounters']} enc / {s['aircraft']} ac</span>"
                f"{'<br>' + flags if flags else ''}</td>"
            )
        rows.append(
            f"<tr><td><a href='#n-{esc(node_id)}'>{esc(node_id)}</a><br><span class='muted small'>{esc(tower_label(node['tower']))}</span></td>{''.join(cells)}</tr>"
        )
    head = "".join(f"<th>{title}</th>" for _, title in SPAN_TITLES)
    return f"<table><tr><th>node</th>{head}</tr>{''.join(rows)}</table>"


# ── Per node ─────────────────────────────────────────────────────────────────


def trend_svg(history: Sequence[Mapping[str, Any]], width: int = 720, height: int = 150) -> str:
    """Validated Pd per window with its interval, raw Pd, background, and the opportunity count underneath."""
    rows = [h for h in history if h["status"] in ("ok", "insufficient")]
    if not rows:
        return "<p class=note>No scored windows yet.</p>"
    rows = rows[-112:]  # two weeks of three-hour windows
    n = len(rows)
    left, right, top, bottom = 36, 8, 8, 40
    plot_w, plot_h = width - left - right, height - top - bottom
    step = plot_w / max(n, 1)

    def x(i: int) -> float:
        return left + step * (i + 0.5)

    def y(v: float) -> float:
        return top + plot_h * (1 - max(0.0, min(1.0, v)))

    parts = [
        f"<svg viewBox='0 0 {width} {height}' width='100%' height='{height}' role='img' aria-label='detection probability per window'>"
    ]
    for g in (0.0, 0.25, 0.5, 0.75, 1.0):
        parts.append(
            f"<line x1='{left}' x2='{width - right}' y1='{y(g):.1f}' y2='{y(g):.1f}' stroke='var(--line)' stroke-width='1'/>"
        )
        parts.append(f"<text x='{left - 4}' y='{y(g) + 3:.1f}' text-anchor='end'>{int(g * 100)}</text>")
    max_n = max((r["n"] for r in rows), default=1) or 1
    for i, r in enumerate(rows):
        h = 24 * r["n"] / max_n
        parts.append(
            f"<rect x='{x(i) - step * 0.4:.1f}' y='{height - 14 - h:.1f}' width='{step * 0.8:.1f}' height='{h:.1f}' fill='var(--bar2)'><title>{esc(r['window'])}: {r['n']:,} opportunities, {r['encounters']} encounters</title></rect>"
        )
        if r["validated_lo"] is not None and r["status"] == "ok":
            parts.append(
                f"<line x1='{x(i):.1f}' x2='{x(i):.1f}' y1='{y(r['validated_lo']):.1f}' y2='{y(r['validated_hi']):.1f}' stroke='var(--bar)' stroke-width='1.5' opacity='0.6'/>"
            )

    def path(key: str, colour: str, dash: str = "") -> None:
        pts = [(x(i), y(r[key])) for i, r in enumerate(rows) if r.get(key) is not None and r["status"] == "ok"]
        if len(pts) >= 2:
            d = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
            parts.append(f"<path d='{d}' fill='none' stroke='{colour}' stroke-width='1.5' {dash}/>")
        for px, py in pts:
            parts.append(f"<circle cx='{px:.1f}' cy='{py:.1f}' r='2' fill='{colour}'/>")

    path("raw_pm1", "var(--raw)", "stroke-dasharray='4 3'")
    path("background", "var(--bad)", "stroke-dasharray='2 3'")
    path("validated", "var(--bar)")
    first, last = rows[0]["window"], rows[-1]["window"]
    parts.append(
        f"<text x='{left}' y='{height - 2}'>{esc(first)}</text><text x='{width - right}' y='{height - 2}' text-anchor='end'>{esc(last)}</text>"
    )
    parts.append("</svg>")
    legend = "<p class=note>Blue: validated detection probability per 3 h window with 95 % encounter-bootstrap interval. Orange dashed: raw tag rate (±1 bucket). Red dotted: chance-association background. Grey bars: opportunities per window.</p>"
    return "".join(parts) + legend


def cell_table(
    section: Mapping[str, Mapping[str, Any]],
    title: str,
    min_n: int = pt.MIN_OPPORTUNITIES,
    order: Sequence[str] | None = None,
) -> str:
    labels = list(order) if order else list(section)
    labels = [name for name in labels if name in section]
    if not labels:
        return ""
    rows = []
    for label in labels:
        c = section[label]
        if c["n"] < min_n:
            rows.append(
                f"<tr><td>{esc(label)}</td><td class=muted>{c['n']:,}</td><td class=muted>{c['encounters']}</td><td class=muted colspan=5>too few opportunities</td></tr>"
            )
            continue
        rows.append(
            f"<tr><td>{esc(label)}</td><td>{c['n']:,}</td><td>{c['encounters']}</td>"
            f"<td>{pct(c['raw_pm1'])} {bar(c['raw_pm1'], 50, raw=True)}</td>"
            f"<td><b>{pct(c['validated'])}</b> {bar(c['validated'], 50)}</td>"
            f"<td>{pct(c['background'])}</td><td>{pct(c['net'])}</td>"
            f"<td class='muted small'>{pct(c['refuted_share'], 0) if c['refuted_share'] is not None else '–'}</td></tr>"
        )
    return (
        f"<h3>{esc(title)}</h3><table><tr><th></th><th>opportunities</th><th>encounters</th><th>raw ±1</th>"
        f"<th>validated</th><th>background</th><th>net</th><th>tags refuted</th></tr>{''.join(rows)}</table>"
    )


def gate_table(agg: Mapping[str, Any]) -> str:
    rows = []
    for gate, words in (
        ("geometric", "geometric: delay window + horizon"),
        ("declared", "declared: + antenna wedge"),
        ("empirical", "empirical: + learned field of view (prior)"),
    ):
        g = agg["gates"][gate]
        rows.append(
            f"<tr><td>{esc(words)}</td><td>{g['n']:,}</td><td>{g['encounters']}</td><td>{pct(g['raw_pm1'])}</td>"
            f"<td><b>{pct(g['validated'])}</b></td><td>{pct(g['background'])}</td><td>{pct(g['net'])}</td></tr>"
        )
    return (
        "<h3>Coverage gates side by side</h3><table><tr><th>gate</th><th>opportunities</th><th>encounters</th>"
        f"<th>raw ±1</th><th>validated</th><th>background</th><th>net</th></tr>{''.join(rows)}</table>"
        "<p class=note>The headline uses the geometric gate: nothing is removed from the denominator because the radar has missed aircraft there. "
        "The empirical gate is the learned field of view from a copy saved before the window; a smaller denominator with a higher rate is coverage, not detection.</p>"
    )


def class_table(agg: Mapping[str, Any]) -> str:
    section = agg["cells"]["geometric"].get("class", {})
    boot = (agg.get("bootstrap") or {}).get("by_class", {})
    order = [
        "widebody",
        "narrowbody",
        "regional_jet",
        "turboprop",
        "bizjet",
        "light_GA",
        "helicopter",
        "military_transport",
        "unknown",
    ]
    labels = [name for name in order if name in section] + sorted(name for name in section if name not in order)
    rows = []
    for label in labels:
        c = section[label]
        b = (boot.get(label) or {}).get("validated") or {}
        if c["n"] < pt.MIN_OPPORTUNITIES or c["encounters"] < pt.MIN_ENCOUNTERS:
            rows.append(
                f"<tr><td>{esc(label)}</td><td class=muted>{c['n']:,}</td><td class=muted>{c['encounters']}</td><td class=muted colspan=4>too few opportunities or encounters</td></tr>"
            )
            continue
        rows.append(
            f"<tr><td>{esc(label)}</td><td>{c['n']:,}</td><td>{c['encounters']}</td><td>{pct(c['raw_pm1'])}</td>"
            f"<td><b>{pct(c['validated'])}</b>{ci(b)} {bar(c['validated'], 50)}</td><td>{pct(c['background'])}</td><td>{pct(c['net'])}</td></tr>"
        )
    if not rows:
        return ""
    return (
        "<h3>By aircraft class</h3><table><tr><th>class</th><th>opportunities</th><th>encounters</th><th>raw ±1</th>"
        f"<th>validated [95 %]</th><th>background</th><th>net</th></tr>{''.join(rows)}</table>"
        "<p class=note>Classes are config/aircraft_classes.py's: military_transport mixes transports and fighters; King Air, TBM and Cheyenne sit in light_GA, not turboprop.</p>"
    )


def type_table(agg: Mapping[str, Any], limit: int = 16) -> str:
    section = agg["cells"]["geometric"].get("type", {})
    labels = sorted(section, key=lambda name: -section[name]["n"])[:limit]
    return cell_table(
        {name: section[name] for name in labels},
        f"By ICAO type (the {limit} with most opportunities)",
        min_n=pt.MIN_OPPORTUNITIES,
    )


def bearing_svg(section: Mapping[str, Mapping[str, Any]], size: int = 220) -> str:
    """Validated Pd per 30° sector as a rose; sector radius is the rate, label is the count."""
    if not section:
        return ""
    import math

    cx = cy = size / 2
    r_max = size / 2 - 18
    parts = [
        f"<svg viewBox='0 0 {size} {size}' width='{size}' height='{size}' role='img' aria-label='detection probability by bearing'>"
    ]
    for g in (0.5, 1.0):
        parts.append(f"<circle cx='{cx}' cy='{cy}' r='{r_max * g:.1f}' fill='none' stroke='var(--line)'/>")
    for label, c in section.items():
        try:
            a0 = float(label.split("-")[0])
        except ValueError:
            continue
        a1 = a0 + pt.BEARING_SECTOR_DEG
        rate = c["validated"] if c["n"] >= pt.MIN_OPPORTUNITIES and c["validated"] is not None else None
        r = r_max * (rate if rate is not None else 0.0)
        t0, t1 = math.radians(a0 - 90), math.radians(a1 - 90)
        x0, y0 = cx + r * math.cos(t0), cy + r * math.sin(t0)
        x1, y1 = cx + r * math.cos(t1), cy + r * math.sin(t1)
        fill = "var(--bar)" if rate is not None else "var(--line)"
        parts.append(
            f"<path d='M{cx},{cy} L{x0:.1f},{y0:.1f} A{r:.1f},{r:.1f} 0 0 1 {x1:.1f},{y1:.1f} Z' fill='{fill}' opacity='0.75'><title>{esc(label)}: {pct(rate)} of {c['n']:,} opportunities</title></path>"
        )
        tm = math.radians((a0 + a1) / 2 - 90)
        parts.append(
            f"<text x='{cx + (r_max + 10) * math.cos(tm):.1f}' y='{cy + (r_max + 10) * math.sin(tm) + 3:.1f}' text-anchor='middle'>{c['n']}</text>"
        )
    parts.append(f"<text x='{cx}' y='12' text-anchor='middle'>N</text></svg>")
    return "".join(parts)


def diagnostics_table(agg: Mapping[str, Any]) -> str:
    d = agg.get("diagnostics") or {}
    geo = agg["gates"]["geometric"]
    tagged = d.get("tagged_detections", 0)
    rows = [
        ("windows scored", f"{agg['windows']}"),
        ("truth available (share of buckets)", pct(agg.get("truth_share"))),
        ("node active (share of buckets)", pct(agg.get("active_share"))),
        ("frames archived", f"{d.get('frames', 0):,}"),
        ("detections", f"{d.get('detections', 0):,}"),
        ("tagged detections", f"{tagged:,}"),
        ("tags whose aircraft is in the truth", pct(d.get("tags_in_truth", 0) / tagged) if tagged else "–"),
        ("tags whose geometry fits the truth", pct(d.get("tags_fitting_truth", 0) / tagged) if tagged else "–"),
        ("detections matched to an aircraft (any)", f"{d.get('matched_detections', 0):,}"),
        ("ambiguous detections (two aircraft fit)", f"{d.get('ambiguous_detections', 0):,}"),
        (
            "opportunities with a tag refuted by geometry",
            f"{geo.get('refuted_share') if geo.get('refuted_share') is None else pct(geo['refuted_share'])}",
        ),
        ("chance-association background (shifted control)", pct(geo["background"])),
        (
            "legacy node-denominator score (tag persistence, PR #727)",
            f"{pct(agg['legacy_node_rate']['pd'])} over {agg['legacy_node_rate']['n']:,} node-fix buckets",
        ),
    ]
    return (
        "<h3>Data quality and false associations</h3><table>"
        + "".join(f"<tr><td>{esc(k)}</td><td>{v}</td></tr>" for k, v in rows)
        + "</table>"
    )


def node_section(node_id: str, doc: Mapping[str, Any]) -> str:
    spans = doc.get("spans") or {}
    parts = [
        f"<h2 id='n-{esc(node_id)}'>{esc(node_id)} <span class='muted small'>{esc(tower_label(doc.get('tower')))}</span></h2>"
    ]
    parts.append(trend_svg(doc.get("history") or []))
    head = "".join(f"<th>{t}</th>" for _, t in SPAN_TITLES)
    line = []
    for key, _ in SPAN_TITLES:
        s = spans.get(key)
        if not s:
            line.append("<td class=muted>no data</td>")
            continue
        g = s["gates"]["geometric"]
        b = (s.get("bootstrap") or {}).get("validated") or {}
        if s["status"] != "ok":
            line.append(
                f"<td>{status_cell(s['status'], s['flags'])}<br><span class='muted small'>{g['n']:,} opp / {g['encounters']} enc</span></td>"
            )
        else:
            lat = s.get("latency_s") or {}
            latency = f"latency median {lat['median']:.0f} s" if lat.get("median") is not None else "latency –"
            line.append(
                f"<td><b>{pct(g['validated'])}</b>{ci(b)}<br><span class='muted small'>raw ±1 {pct(g['raw_pm1'])} · raw exact {pct(g['raw'])} · bg {pct(g['background'])} · net {pct(g['net'])}<br>"
                f"{g['n']:,} opp / {g['encounters']} enc / {g.get('aircraft', 0)} ac · {latency} · legacy {pct(s['legacy_node_rate']['pd'])}</span></td>"
            )
    parts.append(f"<table><tr><th>span</th>{head}</tr><tr><td>validated Pd</td>{''.join(line)}</tr></table>")
    week = spans.get("week") or spans.get("cumulative")
    if week and week.get("windows"):
        parts.append("<p class=note>Breakdowns below use the 7-day span (or everything since start when shorter).</p>")
        parts.append(gate_table(week))
        parts.append(class_table(week))
        cells = week["cells"]["geometric"]
        parts.append("<div class=grid>")
        parts.append(
            "<div>"
            + cell_table(
                cells.get("range_ground", {}), "By receiver ground range", order=pt._labels(pt.RANGE_EDGES_KM, " km")
            )
            + "</div>"
        )
        parts.append(
            "<div>"
            + cell_table(
                cells.get("range_slant", {}), "By receiver slant range", order=pt._labels(pt.RANGE_EDGES_KM, " km")
            )
            + "</div>"
        )
        parts.append(
            "<div>"
            + cell_table(
                cells.get("altitude", {}),
                "By pressure altitude",
                order=pt._labels(tuple(e * 1000 for e in pt.ALT_EDGES_KFT), " ft"),
            )
            + "</div>"
        )
        parts.append(
            "<div>"
            + cell_table(cells.get("delay", {}), "By bistatic delay", order=pt._labels(pt.DELAY_EDGES_US, " us"))
            + "</div>"
        )
        parts.append(
            "<div>"
            + cell_table(
                cells.get("bistatic", {}), "By bistatic angle", order=pt._labels(pt.BISTATIC_EDGES_DEG, " deg")
            )
            + "</div>"
        )
        parts.append(
            "<div>"
            + cell_table(cells.get("hour", {}), "By hour of day (UTC)", order=[f"{h:02d}h" for h in range(24)])
            + "</div>"
        )
        parts.append("</div>")
        parts.append("<h3>By bearing from the receiver (validated Pd per 30° sector; number is opportunities)</h3>")
        parts.append(bearing_svg(cells.get("bearing", {})))
        parts.append(type_table(week))
        parts.append(diagnostics_table(week))
    history = doc.get("history") or []
    if history:
        rows = []
        for h in reversed(history[-56:]):
            mix = ", ".join(f"{k} {v}" for k, v in sorted(h.get("class_mix", {}).items(), key=lambda kv: -kv[1])[:3])
            rows.append(
                f"<tr><td>{esc(h['window'])}</td><td>{status_cell(h['status'], h['flags'])}</td><td>{h['n']:,}</td><td>{h['encounters']}</td>"
                f"<td>{pct(h['raw_pm1'])}</td><td><b>{pct(h['validated'])}</b>{ci({'lo': h['validated_lo'], 'hi': h['validated_hi']})}</td>"
                f"<td>{pct(h['background'])}</td><td>{pct(h['net'])}</td><td>{pct(h['truth_share'], 0)}</td><td>{pct(h['active_share'], 0)}</td>"
                f"<td>{pct(h['legacy'])}</td><td class='muted small'>{esc(mix)}</td></tr>"
            )
        parts.append(
            "<details><summary>Window history (newest first)</summary><table><tr><th>window</th><th>status</th><th>opp</th><th>enc</th>"
            "<th>raw ±1</th><th>validated</th><th>bg</th><th>net</th><th>truth</th><th>active</th><th>legacy</th><th>traffic mix</th></tr>"
            + "".join(rows)
            + "</table></details>"
        )
    return "".join(parts)


def render(latest: Mapping[str, Any], nodes: Mapping[str, Mapping[str, Any]]) -> str:
    gen = stamp(latest.get("generated_ms"))
    last_end = stamp(latest.get("latest_window_end_ms"))
    start = stamp(latest.get("evaluation_start_ms"))
    n_ok = sum(1 for n in latest["nodes"].values() if (n["spans"].get("latest") or {}).get("status") == "ok")
    partial = latest.get("windows_partial") or []
    warn = (
        f"<p class=warn>{len(partial)} window(s) have a failed node: {esc(', '.join(partial[-5:]))}</p>"
        if partial
        else ""
    )
    method = latest.get("method", {})
    body = [
        "<h1>RETINA node detection performance, independent denominator</h1>",
        f"<p class=meta>Generated {gen}. Latest complete window ends {last_end}. Evaluation since {start}. "
        f"{latest.get('windows_processed', 0)} windows scored. {n_ok} of {len(latest['nodes'])} nodes have a usable latest window.</p>",
        warn,
        "<p class=note>Denominator: aircraft seen by ADSBHub (adsb.retina.fm, polled apart from the radar), interpolated to each 10 s bucket, "
        "inside the node's delay window and above its horizon, while the node was sending frames and the region was being polled. "
        "Validated: a detection in the bucket whose delay and Doppler fit the aircraft's independently predicted values within "
        f"{method.get('validate_delay_us')} µs and {method.get('validate_doppler_hz')} Hz, unambiguously. Raw ±1: the node's own tag in the bucket or a neighbour (the earlier method's numerator). "
        f"Background: the same match against trajectories shifted by {', '.join(str(s) for s in method.get('background_shifts_s', []))} s. "
        "Intervals: 95 % bootstrap over encounters. A span needs "
        f"{method.get('min_opportunities')} opportunities and {method.get('min_encounters')} encounters to show a rate.</p>",
        "<h2>Fleet</h2>",
        fleet_table(latest),
        "<p class=note>Machine-readable: <a href='latest.json'>latest.json</a>, <a href='history.json'>history.json</a>, nodes/&lt;node&gt;.json.</p>",
    ]
    for node_id in sorted(latest["nodes"]):
        doc = nodes.get(node_id)
        if doc:
            body.append(node_section(node_id, doc))
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>RETINA node performance</title><style>{_CSS}</style></head><body>{''.join(body)}</body></html>"
    )
