"""U12 scorecard: ``scorecard.md`` + ``scorecard.html`` across arms (P1 §12 Report).

Purpose: one page that says whether the thesis holds. Contents: one card per
hypothesis H1–H6 (value, 95% interval, threshold, PASS / FAIL / N/A), the arm table,
the E1 table (``relation_mode`` from ``runs.manifest``), miss attribution, the
coverage summary, the token flow (source → micro → contracted → T2 → T3),
anchoring, audits and — when an arm was run more than once — nondeterminism.

Thresholds come from the first fenced ``yaml`` block with a ``thresholds:`` mapping
in ``docs/PREREGISTRATION.md`` (keys as in :data:`DEFAULT_THRESHOLDS`); without it
the spec's † initial values apply. Pass/fail is decided on the point estimate; the
interval is shown beside it.

Contract: :func:`build_report` ``(store, run_ids, out_dir) -> (md_path, html_path)``.
Arms are scored afresh (deterministic; no writes) so paired bootstraps can resample
the same documents in both arms. The HTML is one self-contained file: inline CSS,
no scripts, no external resources.
Stores written: none (files only). Token caps: none. Spec: P1 §1, §12, §13.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import yaml

from synth.audit.audits import audit_run
from synth.config import REPO_ROOT
from synth.eval import stats
from synth.eval.metrics import MISS_ORDER, ArmScore, RunData, _defaults, real_arms, score_arm

ARM_ORDER = ("full", "t2_only", "t3_only", "t1_only", "t1_t2")
PREREGISTRATION = REPO_ROOT / "docs" / "PREREGISTRATION.md"

#: Spec † initial values (P1 §1).
DEFAULT_THRESHOLDS = {
    "H1_max_f1_gap": 0.05,             # full fact F1 within 5 points of t3_only
    "H2_max_ftvf_ratio": 0.20,         # full FTVF ≤ 20% of t3_only's
    "H3_match_margin": 0.05,           # "matches" = within this many F1 points (at lower cost)
    "H4_min_provenance": 1.0,          # share of committed facts with located spans
    "H5_min_gold_coverage": 0.98,      # gold-fact sentences read by T2 or T3
    "H5_max_silent_drops": 0,
    "H6_max_false_merge_rate": 0.0,    # on gold identity traps
}


def load_thresholds(path: str | Path | None = None) -> tuple[dict[str, float], str]:
    path = Path(path) if path else PREREGISTRATION
    if path.exists():
        for block in re.findall(r"```ya?ml\s*\n(.*?)```", path.read_text(encoding="utf-8"), re.S):
            try:
                data = yaml.safe_load(block) or {}
            except yaml.YAMLError:
                continue
            if isinstance(data, dict) and isinstance(data.get("thresholds"), dict):
                merged = {**DEFAULT_THRESHOLDS, **{k: float(v) for k, v in data["thresholds"].items()}}
                return merged, path.name
    return dict(DEFAULT_THRESHOLDS), "spec † initial values"


# ------------------------------------------------------------------ formatting
def _num(v: float | None, digits: int = 3) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{v:.0f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{100 * v:.1f}%"


def _ci(lo: float | None, hi: float | None, fmt=_num) -> str:
    return "" if lo is None or hi is None else f"[{fmt(lo)}, {fmt(hi)}]"


def _cell(score: ArmScore | None, metric: str, slice_: str = "all", fmt=_num, ci: bool = False) -> str:
    row = score.row(metric, slice_) if score else None
    if not row:
        return "—"
    text = fmt(row["value"])
    if ci and row["ci_low"] is not None:
        text += " " + _ci(row["ci_low"], row["ci_high"], fmt)
    return text


def _f1(family: str):
    tp, sy, go = (("prf", family, "all", k) for k in ("tp", "sys", "gold"))
    return lambda c: (2 * c[tp] / (c[sy] + c[go])) if (c[sy] + c[go]) else None


def _ratio_fn(metric: str):
    num, den = ("ratio", metric, "all", "num"), ("ratio", metric, "all", "den")
    return lambda c: (c[num] / c[den]) if c[den] else None


# ------------------------------------------------------------------ the report
def build_report(store, run_ids: list[str], out_dir: str | Path, *, config=None, ontology=None,
                 prereg_path: str | Path | None = None) -> tuple[Path, Path]:
    config, ontology = _defaults(config, ontology)
    resamples, seed = stats.settings(config)
    thresholds, threshold_source = load_thresholds(prereg_path)
    runs: list[dict[str, Any]] = []
    for rid in run_ids:
        with store.reader() as r:
            run = r.get("runs", run_id=rid)
        if run is None:
            raise KeyError(f"no run {rid}")
        data = RunData(store, rid, config)
        arms = [run["arm"]] + (["t1_only", "t1_t2"] if run["arm"] == "full" else [])
        scores = {a: score_arm(store, rid, a, config=config, ontology=ontology, data=data) for a in arms}
        runs.append({"run": run, "data": data, "scores": scores, "audit": audit_run(store, rid)})

    by_arm: dict[str, list[tuple[str, ArmScore]]] = {}
    for item in runs:
        for arm, score in item["scores"].items():
            by_arm.setdefault(arm, []).append((item["run"]["run_id"], score))
    first = {arm: items[0][1] for arm, items in by_arm.items()}
    boot = dict(resamples=resamples, seed=seed)

    cards = _cards(first, runs, thresholds, boot)
    tables = [_arm_table(by_arm), _e1_table(runs), _miss_table(by_arm), _coverage_table(runs),
              _anchoring_table(first, by_arm), _audit_table(runs)]
    nondet = _nondeterminism_table(by_arm)
    if nondet:
        tables.append(nondet)
    flows = _flows(runs)
    meta = {"runs": [f"{i['run']['run_id']} ({i['run']['arm']})" for i in runs],
            "thresholds": threshold_source,
            "gold": sum(len(i["data"].gold) for i in runs[:1]),
            "bootstrap": f"{resamples} resamples over documents, seed {seed}"}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path, html_path = out / "scorecard.md", out / "scorecard.html"
    md_path.write_text(_markdown(meta, cards, tables, flows), encoding="utf-8")
    html_path.write_text(_html(meta, cards, tables, flows), encoding="utf-8")
    return md_path, html_path


# ------------------------------------------------------------------ hypothesis cards
def _card(hid: str, title: str, value: str, interval: str, threshold: str, status: str,
          detail: str) -> dict[str, str]:
    return dict(id=hid, title=title, value=value, interval=interval, threshold=threshold,
                status=status, detail=detail)


def _status(ok: bool | None) -> str:
    return "N/A" if ok is None else ("PASS" if ok else "FAIL")


def _cards(first: dict[str, ArmScore], runs, t: dict[str, float], boot) -> list[dict[str, str]]:
    cards = []
    full, t3, t2, t1t2 = (first.get(a) for a in ("full", "t3_only", "t2_only", "t1_t2"))

    # H1 quality
    if full and t3:
        res = stats.paired_bootstrap(full.gold_units, t3.gold_units, _f1("fact"), **boot)
        ok = None if res["value"] is None else res["value"] >= -t["H1_max_f1_gap"]
        cards.append(_card("H1", "Quality: full fact F1 vs t3_only",
                           f"{_pts(res['value'])} pts", _ci(res["ci_low"], res["ci_high"], _pts),
                           f"≥ −{_points(t['H1_max_f1_gap'])} pts", _status(ok),
                           f"full {_num(full.value('fact.f1'))} · t3_only {_num(t3.value('fact.f1'))} · "
                           f"paired bootstrap over {res['n']} documents"))
    else:
        cards.append(_card("H1", "Quality: full fact F1 vs t3_only", "—", "",
                           f"≥ −{_points(t['H1_max_f1_gap'])} pts", "N/A", "needs a full and a t3_only run"))
    # H2 cost
    if full and t3:
        res = stats.paired_bootstrap(full.run_units, t3.run_units, _ratio_fn("cost.ftvf"), mode="ratio", **boot)
        ok = None if res["value"] is None else res["value"] <= t["H2_max_ftvf_ratio"]
        cards.append(_card("H2", "Cost: FTVF full ÷ t3_only", _pct(res["value"]),
                           _ci(res["ci_low"], res["ci_high"], _pct), f"≤ {_pct(t['H2_max_ftvf_ratio'])}",
                           _status(ok), f"FTVF full {_num(full.value('cost.ftvf'))} · "
                           f"t3_only {_num(t3.value('cost.ftvf'))} frontier tokens per verified fact"))
    else:
        cards.append(_card("H2", "Cost: FTVF full ÷ t3_only", "—", "", f"≤ {_pct(t['H2_max_ftvf_ratio'])}",
                           "N/A", "needs a full and a t3_only run"))
    # H3 tier-1 value
    if t1t2 and t2:
        res = stats.paired_bootstrap(t1t2.gold_units, t2.gold_units, _f1("fact"), **boot)
        cost_a = _tokens_per_fact(t1t2)
        cost_b = _tokens_per_fact(t2)
        ok = None
        if res["value"] is not None:
            cheaper = cost_a is not None and cost_b is not None and cost_a < cost_b
            ok = res["value"] > 0 or (abs(res["value"]) <= t["H3_match_margin"] and cheaper)
        cards.append(_card("H3", "Tier 1 value: t1_t2 vs t2_only", f"{_pts(res['value'])} pts",
                           _ci(res["ci_low"], res["ci_high"], _pts),
                           f"> 0, or within {_points(t['H3_match_margin'])} pts at lower cost", _status(ok),
                           f"tokens per fact t1_t2 {_num(cost_a)} · t2_only {_num(cost_b)}"))
    else:
        cards.append(_card("H3", "Tier 1 value: t1_t2 vs t2_only", "—", "", "> 0, or match at lower cost",
                           "N/A", "needs a full and a t2_only run"))
    # H4 provenance
    prov_fail = sum(len(v) for i in runs for v in i["audit"]["provenance"].values())
    rates = [s.value("grounding.committed_rate") for i in runs for a, s in i["scores"].items()
             if a in real_arms()]
    rates = [r for r in rates if r is not None]
    rate = min(rates) if rates else None
    ok = None if not runs else (prov_fail == 0 and (rate is None or rate >= t["H4_min_provenance"]))
    cards.append(_card("H4", "Provenance: committed facts with located spans", _pct(rate), "",
                       f"≥ {_pct(t['H4_min_provenance'])}, 0 audit failures", _status(ok),
                       f"{prov_fail} provenance audit failure(s) over {len(runs)} run(s)"))
    # H5 coverage
    silent = sum(len(i["audit"]["coverage"]["silent_drops"]) for i in runs)
    cov_arm = full or next((first[a] for a in real_arms() if a in first), None)
    row = cov_arm.row("coverage.gold") if cov_arm else None
    value = row["value"] if row else None
    ok = None if value is None else (silent <= t["H5_max_silent_drops"] and value >= t["H5_min_gold_coverage"])
    if value is None and runs:
        ok = None if silent == 0 else False
    cards.append(_card("H5", "Coverage: gold-fact sentences read by T2/T3", _pct(value),
                       _ci(row["ci_low"], row["ci_high"], _pct) if row else "",
                       f"≥ {_pct(t['H5_min_gold_coverage'])}, {int(t['H5_max_silent_drops'])} silent drops",
                       _status(ok), f"{silent} silent drop(s) over {len(runs)} run(s)"
                       + (f" · arm {cov_arm.arm}" if cov_arm else "")))
    # H6 identity traps
    trap_arm = full or next((first[a] for a in real_arms() if a in first), None)
    row = trap_arm.row("resolution.trap_false_merge_rate") if trap_arm else None
    value = row["value"] if row else None
    ok = None if value is None else value <= t["H6_max_false_merge_rate"]
    cards.append(_card("H6", "Identity traps: false-merge rate", _pct(value),
                       _ci(row["ci_low"], row["ci_high"], _pct) if row else "",
                       f"= {_pct(t['H6_max_false_merge_rate'])}", _status(ok),
                       f"{row['n'] if row else 0} gold trap(s)" + (f" · arm {trap_arm.arm}" if trap_arm else "")))
    return cards


def _pts(v: float | None) -> str:
    return "—" if v is None else f"{100 * v:+.1f}".replace("+-", "−").replace("-", "−")


def _points(v: float) -> str:
    return f"{100 * v:.1f}"


def _tokens_per_fact(score: ArmScore) -> float | None:
    facts = score.value("cost.committed_facts")
    tokens = (score.value("cost.local_tokens") or 0) + (score.value("cost.t3_tokens") or 0)
    return tokens / facts if facts else None


# ------------------------------------------------------------------ tables
def _arm_table(by_arm):
    headers = ["arm", "run", "fact F1", "edge F1 strict", "edge F1 lenient", "entity F1", "prop F1",
               "link F1", "FTVF", "frontier tok / correct fact", "local tok / fact", "s / page",
               "escalation rate"]
    rows = []
    for arm in ARM_ORDER:
        for rid, s in by_arm.get(arm, []):
            rows.append([arm, rid[-8:], _cell(s, "fact.f1", ci=True), _cell(s, "edge.strict.f1", ci=True),
                         _cell(s, "edge.lenient.f1"), _cell(s, "entity.cluster.f1"), _cell(s, "proposition.f1"),
                         _cell(s, "link.f1"), _cell(s, "cost.ftvf"),
                         _cell(s, "cost.frontier_tokens_per_correct_fact"),
                         _cell(s, "cost.local_tokens_per_fact"), _cell(s, "cost.seconds_per_page"),
                         _cell(s, "escalation.rate", fmt=_pct)])
    return ("Arms", "Derived arms (t1_only, t1_t2) are read from the full run's change log.", headers, rows)


def _manifest_mode(run: dict[str, Any]) -> str | None:
    m = run.get("manifest") or {}
    return (m.get("relation_mode") or (m.get("extra") or {}).get("relation_mode")
            or (((m.get("config") or {}).get("model_settings") or {}).get("tier1") or {}).get("relation_mode"))


def _e1_table(runs):
    headers = ["relation_mode", "run", "micro P strict", "micro R strict", "micro F1 strict",
               "micro F1 lenient", "s / page", "micro-LLM tok / page"]
    rows = []
    for item in runs:
        mode = _manifest_mode(item["run"])
        s = item["scores"].get("t1_only")
        if mode and s:
            rows.append([mode, item["run"]["run_id"][-8:], _cell(s, "edge.strict.p"), _cell(s, "edge.strict.r"),
                         _cell(s, "edge.strict.f1", ci=True), _cell(s, "edge.lenient.f1"),
                         _cell(s, "cost.seconds_per_page"), _cell(s, "cost.t1_micro_tokens_per_page")])
    return ("E1 — Tier 1 relation source", "Micro-graph edges (t1_only) against gold.", headers, rows)


def _miss_table(by_arm):
    headers = ["arm", "run"] + list(MISS_ORDER) + ["total"]
    rows = []
    for arm in real_arms():
        for rid, s in by_arm.get(arm, []):
            counts = [int(s.value(f"miss.{c}") or 0) for c in MISS_ORDER]
            rows.append([arm, rid[-8:]] + [str(n) for n in counts] + [str(sum(counts))])
    return ("Miss attribution", "Gold facts no system fact matched (lenient), first cause in pipeline order.",
            headers, rows)


def _coverage_table(runs):
    headers = ["run", "arm", "read (T2/T3)", "micro read (T1)", "yield", "loss rate", "silent drops",
               "gold coverage", "terminal outcomes"]
    rows = []
    for item in runs:
        arm = item["run"]["arm"]
        s = item["scores"].get(arm)
        counts = item["data"].coverage.counts()
        terminal = ", ".join(f"{k} {v}" for k, v in counts.items() if v)
        rows.append([item["run"]["run_id"][-8:], arm, _cell(s, "coverage.read", fmt=_pct),
                     _cell(s, "coverage.micro_read", fmt=_pct), _cell(s, "coverage.yield", fmt=_pct),
                     _cell(s, "coverage.loss_rate", fmt=_pct), str(counts.get("SILENT_DROP", 0)),
                     _cell(s, "coverage.gold", fmt=_pct, ci=True), terminal or "—"])
    return ("Coverage", "Terminal outcome per sentence (P1 §7).", headers, rows)


def _anchoring_table(first, by_arm):
    headers = ["measure", "value"]
    t1t2, t2 = first.get("t1_t2"), first.get("t2_only")
    rows = [["T1 false edges kept by T2 ÷ T1 false edges", _cell(t1t2, "anchoring.t1_false_kept_rate", fmt=_pct)]]
    if t1t2 and t2:
        rows.append(["gold edges found by t2_only but missed by t1_t2", str(len(t2.matched_gold - t1t2.matched_gold))])
    rows.append(["contraction E (candidate ÷ source)", _cell(first.get("full"), "contract.E")])
    rows.append(["contraction C (contracted ÷ candidate)", _cell(first.get("full"), "contract.C")])
    return ("Anchoring and contraction", "Does T2 inherit T1's mistakes?", headers, rows)


def _audit_table(runs):
    headers = ["run", "arm", "audits", "failures"]
    rows = [[i["run"]["run_id"][-8:], i["run"]["arm"], "PASS" if i["audit"]["passed"] else "FAIL",
             "; ".join(i["audit"]["failures"]) or "—"] for i in runs]
    return ("Audits", "Provenance, coverage and budget (P1 §13).", headers, rows)


def _nondeterminism_table(by_arm):
    rows = []
    for arm, items in by_arm.items():
        if len(items) < 2:
            continue
        for metric in ("fact.f1", "edge.strict.f1", "cost.ftvf"):
            nd = stats.nondeterminism([s.value(metric) for _rid, s in items])
            rows.append([arm, metric, str(nd["n"]), _num(nd["mean"]), _num(nd["sd"]), _num(nd["range"])])
    if not rows:
        return None
    return ("Nondeterminism", "Repeated runs of the same arm.", ["arm", "metric", "runs", "mean", "sd", "range"], rows)


def _flows(runs) -> list[tuple[str, list[tuple[str, float]]]]:
    out = []
    for item in runs:
        s = item["scores"].get(item["run"]["arm"])
        if not s:
            continue
        steps = [(label, s.value(f"flow.{key}") or 0.0) for key, label in
                 (("source", "source"), ("micro", "micro"), ("contracted", "contracted"),
                  ("t2", "T2"), ("t3", "T3"))]
        if any(v for _l, v in steps):
            out.append((f"{item['run']['run_id'][-8:]} ({item['run']['arm']})", steps))
    return out


# ------------------------------------------------------------------ renderers
def _markdown(meta, cards, tables, flows) -> str:
    lines = ["# Synth P1 scorecard", "",
             f"Runs: {', '.join(meta['runs'])}  ", f"Thresholds: {meta['thresholds']}  ",
             f"Gold passages: {meta['gold']} · Intervals: 95%, {meta['bootstrap']}", "",
             "## Hypotheses", ""]
    for c in cards:
        lines += [f"### {c['id']} — {c['title']}: **{c['status']}**", "",
                  f"- value: {c['value']} {c['interval']}".rstrip(), f"- threshold: {c['threshold']}",
                  f"- {c['detail']}", ""]
    for title, note, headers, rows in tables:
        lines += [f"## {title}", "", note, ""]
        if rows:
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("|" + "---|" * len(headers))
            lines += ["| " + " | ".join(r) + " |" for r in rows]
        else:
            lines.append("_no data_")
        lines.append("")
    lines += ["## Token flow", ""]
    for label, steps in flows:
        lines.append(f"- {label}: " + " → ".join(f"{name} {_num(v, 0)}" for name, v in steps))
    if not flows:
        lines.append("_no data_")
    return "\n".join(lines) + "\n"


_CSS = """
:root { --bg:#f7f6f2; --card:#ffffff; --ink:#1d1d1b; --muted:#5f5e58; --line:#dedbd2;
        --pass:#1f7a3f; --fail:#b3261e; --na:#6b6a64; --bar:#3d6fb6; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --bg:#161615; --card:#21211f; --ink:#ecebe6; --muted:#a8a69e;
        --line:#3a3936; --pass:#5cc283; --fail:#f2847c; --na:#9b9a93; --bar:#7aa7e6; } }
:root[data-theme="dark"] { --bg:#161615; --card:#21211f; --ink:#ecebe6; --muted:#a8a69e;
        --line:#3a3936; --pass:#5cc283; --fail:#f2847c; --na:#9b9a93; --bar:#7aa7e6; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font: 18px/1.5 system-ui, -apple-system,
       "Segoe UI", Roboto, sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 2rem; margin: 0 0 4px; } h2 { font-size: 1.35rem; margin: 40px 0 8px; }
.meta { color: var(--muted); font-size: .95rem; }
.cards { display:grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap:16px; margin-top:20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px 20px; }
.card .head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; }
.card .hid { font-weight:700; font-size:1.1rem; }
.badge { font-weight:700; font-size:.85rem; padding:2px 10px; border-radius:999px; border:2px solid currentColor; }
.PASS { color:var(--pass); } .FAIL { color:var(--fail); } .NA { color:var(--na); }
.card .title { color:var(--muted); margin:4px 0 10px; }
.card .value { font-size:2.2rem; font-weight:700; line-height:1.1; }
.card .interval { color:var(--muted); font-size:1rem; }
.card .threshold, .card .detail { font-size:.95rem; margin-top:6px; }
.tablewrap { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:12px; }
table { border-collapse: collapse; width:100%; font-size:.95rem; }
th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--muted); font-weight:600; }
.note { color:var(--muted); margin:0 0 8px; }
.flow { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 20px; margin-bottom:12px; }
.flowrow { display:grid; grid-template-columns: 110px 1fr 120px; gap:12px; align-items:center; margin:6px 0; }
.bar { height:16px; background:var(--bar); border-radius:4px; min-width:2px; }
.empty { color:var(--muted); font-style:italic; }
"""


def _html(meta, cards, tables, flows) -> str:
    e = html.escape
    parts = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             "<title>Synth Scorecard</title>", f"<style>{_CSS}</style></head><body><main>",
             "<h1>Synth P1 scorecard</h1>",
             f'<p class="meta">Runs: {e(", ".join(meta["runs"]))}<br>Thresholds: {e(meta["thresholds"])} · '
             f'Gold passages: {meta["gold"]} · 95% intervals, {e(meta["bootstrap"])}</p>',
             "<h2>Hypotheses</h2>", '<div class="cards">']
    for c in cards:
        cls = "NA" if c["status"] == "N/A" else c["status"]
        parts.append(
            f'<section class="card"><div class="head"><span class="hid">{e(c["id"])}</span>'
            f'<span class="badge {cls}">{e(c["status"])}</span></div>'
            f'<div class="title">{e(c["title"])}</div><div class="value">{e(c["value"])}</div>'
            f'<div class="interval">{e(c["interval"]) or "&nbsp;"}</div>'
            f'<div class="threshold">Threshold: {e(c["threshold"])}</div>'
            f'<div class="detail">{e(c["detail"])}</div></section>')
    parts.append("</div>")
    for title, note, headers, rows in tables:
        parts += [f"<h2>{e(title)}</h2>", f'<p class="note">{e(note)}</p>']
        if not rows:
            parts.append('<p class="empty">no data</p>')
            continue
        parts.append('<div class="tablewrap"><table><thead><tr>'
                     + "".join(f"<th>{e(h)}</th>" for h in headers) + "</tr></thead><tbody>")
        parts += ["<tr>" + "".join(f"<td>{e(v)}</td>" for v in r) + "</tr>" for r in rows]
        parts.append("</tbody></table></div>")
    parts.append("<h2>Token flow</h2>")
    if not flows:
        parts.append('<p class="empty">no data</p>')
    for label, steps in flows:
        top = max((v for _n, v in steps), default=0) or 1
        parts.append(f'<div class="flow"><strong>{e(label)}</strong>')
        for name, v in steps:
            parts.append(f'<div class="flowrow"><span>{e(name)}</span>'
                         f'<div class="bar" style="width:{100 * v / top:.1f}%"></div>'
                         f'<span>{e(_num(v, 0))} tok</span></div>')
        parts.append("</div>")
    parts.append("</main></body></html>")
    return "\n".join(parts) + "\n"
