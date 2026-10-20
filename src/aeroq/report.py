"""Builds the stakeholder dashboard (single self-contained HTML file) and
Tableau / Power BI-ready CSV extracts from the warehouse."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from sqlalchemy.engine import Engine

from . import analytics
from .config import settings


def _export_extracts(engine: Engine, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    analytics.first_pass_yield(engine, "program").to_csv(out / "fpy_by_program.csv", index=False)
    analytics.first_pass_yield(engine, "month").to_csv(out / "fpy_by_month.csv", index=False)
    analytics.defect_pareto(engine).to_csv(out / "defect_pareto.csv", index=False)
    analytics.weekly_defect_rate(engine).to_csv(out / "weekly_defect_rate.csv", index=False)
    analytics.process_capability(engine).to_csv(out / "process_capability.csv", index=False)
    analytics.cycle_time_stats(engine).to_csv(out / "cycle_time.csv", index=False)


def build_dashboard(engine: Engine, found: dict, dq_results: list, run_id: str, out_dir: Path | None = None) -> Path:
    out_dir = Path(out_dir or settings.reports_dir)
    _export_extracts(engine, out_dir / "extracts")

    spc: pd.DataFrame = found["spc"]
    worst_wc = (
        spc.groupby("work_center")["out_of_control"].sum().sort_values(ascending=False).index[0]
        if spc["out_of_control"].any() else spc["work_center"].iloc[0]
    )
    spc_wc = spc[spc["work_center"] == worst_wc]
    iso = found["isolation_forest"]
    events = found["events"]
    data = {
        "run_id": run_id,
        "kpis": analytics.kpis(engine),
        "fpy_program": analytics.first_pass_yield(engine, "program").to_dict("list"),
        "fpy_wc": analytics.first_pass_yield(engine, "work_center").to_dict("list"),
        "pareto": analytics.defect_pareto(engine).to_dict("list"),
        "spc": {"wc": worst_wc, **spc_wc[["week", "defect_rate", "ucl", "pbar"]].to_dict("list")},
        "cpk": analytics.process_capability(engine).to_dict("records"),
        "iso": {
            "x": iso["temp_mean"].round(2).tolist(), "y": iso["temp_slope"].round(4).tolist(),
            "a": iso["is_anomaly"].tolist(), "id": iso["work_order_id"].tolist(),
        },
        "events": (pd.concat([events[events["method"] == "SPC p-chart"].sort_values("period"),
                             events[events["method"] != "SPC p-chart"].sort_values("value", ascending=False).head(8)])
                   .to_dict("records") if len(events) else []),
        "n_events": int(len(events)),
        "dq": [r.__dict__ for r in dq_results],
    }
    html = TEMPLATE.replace("__DATA__", json.dumps(data, default=str))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "dashboard.html"
    path.write_text(html, encoding="utf-8")
    return path


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AeroQ Quality Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--muted:#8b949e;--a:#58a6ff;--b:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:24px 16px 8px;max-width:1200px;margin:auto}h1{margin:0;font-size:22px}header p{margin:4px 0 0;color:var(--muted)}
main{max-width:1200px;margin:auto;padding:16px;display:grid;gap:16px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;grid-column:span 12;min-width:0}
.c6{grid-column:span 6}.c4{grid-column:span 4}.c8{grid-column:span 8}
@media(max-width:860px){.c6,.c4,.c8{grid-column:span 12}}
h2{font-size:14px;margin:0 0 10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}.kpi b{display:block;font-size:26px}.kpi span{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{color:var(--muted);font-weight:600}
.tbl{overflow-x:auto}.ok{color:var(--b)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.chart{position:relative;height:300px}footer{color:var(--muted);text-align:center;padding:24px;font-size:12px}
</style></head><body>
<header><h1>AeroQ — Manufacturing Quality Dashboard</h1>
<p>Synthetic aerospace production data · run <code id="run"></code></p></header>
<main>
<section class="card"><div class="kpis" id="kpis"></div></section>
<section class="card c8"><h2>SPC p-chart — <span id="spcwc"></span></h2><div class="chart"><canvas id="spc"></canvas></div></section>
<section class="card c4"><h2>First-pass yield by program</h2><div class="chart"><canvas id="fpy"></canvas></div></section>
<section class="card c6"><h2>Defect Pareto</h2><div class="chart"><canvas id="pareto"></canvas></div></section>
<section class="card c6"><h2>Autoclave cure profiles — Isolation Forest</h2><div class="chart"><canvas id="iso"></canvas></div></section>
<section class="card c6"><h2>Process capability (Cpk target ≥ 1.33)</h2><div class="tbl"><table id="cpk"></table></div></section>
<section class="card c6"><h2>Data-quality checks (this run)</h2><div class="tbl"><table id="dq"></table></div></section>
<section class="card"><h2>Anomaly events — SPC signals + top Isolation Forest outliers (<span id="nev"></span> total)</h2><div class="tbl"><table id="ev"></table></div></section>
</main>
<footer>Generated by the AeroQ pipeline · Python · SQLAlchemy · scikit-learn · FastAPI</footer>
<script>
const D=__DATA__;
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
Chart.defaults.color=css('--muted');Chart.defaults.borderColor=css('--line');Chart.defaults.maintainAspectRatio=false;
const pct=v=>(v*100).toFixed(1)+'%';
document.getElementById('run').textContent=D.run_id;
const K=D.kpis;
document.getElementById('kpis').innerHTML=[["Work orders",K.work_orders.toLocaleString()],["Inspections",K.inspections.toLocaleString()],["First-pass yield",pct(K.fpy)],["Defect rate",pct(K.defect_rate)],["Scrapped",K.scrapped],["Anomalies flagged",D.n_events]].map(([l,v])=>`<div class="kpi"><b>${v}</b><span>${l}</span></div>`).join('');
document.getElementById('spcwc').textContent=D.spc.wc;
const oc=D.spc.defect_rate.map((v,i)=>v>D.spc.ucl[i]);
new Chart(spc,{type:'line',data:{labels:D.spc.week,datasets:[
 {label:'Defect rate',data:D.spc.defect_rate,borderColor:css('--a'),pointBackgroundColor:oc.map(o=>o?css('--bad'):css('--a')),pointRadius:oc.map(o=>o?5:2),tension:.2},
 {label:'UCL',data:D.spc.ucl,borderColor:css('--bad'),borderDash:[6,4],pointRadius:0,stepped:true},
 {label:'p̄ (baseline)',data:D.spc.pbar,borderColor:css('--b'),borderDash:[2,3],pointRadius:0}]},
 options:{scales:{y:{ticks:{callback:pct}}},plugins:{tooltip:{callbacks:{label:c=>c.dataset.label+': '+pct(c.raw)}}}}});
new Chart(fpy,{type:'bar',data:{labels:D.fpy_program.program,datasets:[{label:'FPY',data:D.fpy_program.fpy,backgroundColor:css('--a'),borderRadius:4}]},
 options:{indexAxis:'y',scales:{x:{min:0,max:1,ticks:{callback:pct}}},plugins:{legend:{display:false}}}});
new Chart(pareto,{data:{labels:D.pareto.defect_desc,datasets:[
 {type:'bar',label:'Defects',data:D.pareto.defects,backgroundColor:css('--a'),borderRadius:4,yAxisID:'y'},
 {type:'line',label:'Cumulative %',data:D.pareto.cum_pct,borderColor:css('--warn'),yAxisID:'y1'}]},
 options:{scales:{y1:{position:'right',min:0,max:1,grid:{display:false},ticks:{callback:pct}}}}});
const pts=f=>D.iso.x.map((x,i)=>({x,y:D.iso.y[i],id:D.iso.id[i]})).filter((_,i)=>D.iso.a[i]===f);
new Chart(iso,{type:'scatter',data:{datasets:[{label:'Normal',data:pts(false),backgroundColor:css('--a')+'99',pointRadius:3},{label:'Anomaly',data:pts(true),backgroundColor:css('--bad'),pointRadius:5}]},
 options:{scales:{x:{title:{display:true,text:'Mean cure temp (°C)'}},y:{title:{display:true,text:'Temp slope (°C/min)'}}},plugins:{tooltip:{callbacks:{label:c=>c.raw.id}}}}});
const tbl=(id,cols,rows)=>document.getElementById(id).innerHTML='<tr>'+cols.map(c=>`<th>${c[0]}</th>`).join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>`<td>${c[1](r)}</td>`).join('')+'</tr>').join('');
tbl('cpk',[["Characteristic",r=>r.characteristic],["n",r=>r.n],["Mean",r=>r.mean],["σ",r=>r.std],["Cp",r=>r.cp],["Cpk",r=>`<span class="${r.cpk>=1.33?'ok':r.cpk>=1?'warn':'bad'}">${r.cpk}</span>`]],D.cpk);
tbl('dq',[["Dataset",r=>r.dataset],["Rule",r=>r.rule],["Result",r=>r.passed?'<span class="ok">PASS</span>':`<span class="${r.severity==='error'?'bad':'warn'}">${r.severity.toUpperCase()}</span>`],["Failed",r=>`${r.failed_rows}/${r.total_rows}`]],D.dq);
tbl('ev',[["Method",r=>r.method],["Entity",r=>r.entity],["Period",r=>r.period],["Metric",r=>r.metric],["Value",r=>(+r.value).toFixed(3)],["Threshold",r=>(+r.threshold).toFixed(3)],["Detail",r=>r.detail]],D.events);
document.getElementById('nev').textContent=D.n_events;
</script></body></html>"""
