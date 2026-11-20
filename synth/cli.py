"""``py -m synth`` — the J0 command line (P1 §3 J0, CLAUDE.md "Commands").

Each command is a thin wrapper: the work lives in ``synth.orchestrator`` and the
modules, so tests call the same functions the CLI does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

from synth.config import load_config

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Synth: GLiNER + micro LLM -> micro graphs; existing 14B verifies; frontier on residue.")
db_app = typer.Typer(no_args_is_help=True, help="Database schema.")
gold_app = typer.Typer(no_args_is_help=True, help="Gold annotation workflow.")
app.add_typer(db_app, name="db")
app.add_typer(gold_app, name="gold")

ConfigOpt = typer.Option(None, "--config", "-c", help="Run config (default configs/synth/p1.default.yaml)")


def _orchestrator():
    from synth.orchestrator import runner
    return runner


@db_app.command("upgrade")
def db_upgrade(url: Optional[str] = typer.Option(None, help="Database URL (default SYNTH_DB_URL)"),
               config: Optional[Path] = ConfigOpt) -> None:
    """Create or migrate every store and ledger table (Alembic)."""
    from synth.store.db import upgrade

    cfg = load_config(config)
    resolved = upgrade(url or cfg.db_url())
    typer.echo(f"database at head: {resolved.split('@')[-1]}")


@app.command()
def ingest(path: Path = typer.Argument(..., help="File or folder of documents"),
           config: Optional[Path] = ConfigOpt,
           sovereign: bool = typer.Option(False, help="Mark these documents sovereign (never leave the machine)")) -> None:
    """U1: parse documents into canonical text, sentences, micro chunks and sections."""
    result = _orchestrator().ingest(path, config_path=config, sovereign=sovereign)
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def run(arm: str = typer.Option("minus0", help="minus0 (GLiNER -> local graph -> frontier review) | full | t2_only | t3_only"),
        config: Optional[Path] = ConfigOpt,
        docs: Optional[List[str]] = typer.Option(None, "--doc", help="Restrict to these doc ids"),
        resume: Optional[str] = typer.Option(None, help="Resume this run id"),
        relation_mode: Optional[str] = typer.Option(None, help="E1: micro | gliner | gliner+micro"),
        scripted: bool = typer.Option(False, help="Use the scripted fake models (no servers)"),
        gliner: Optional[str] = typer.Option(None, help="gliner2 | heuristic (regex stand-in for demos)"),
        ontology: Optional[str] = typer.Option(None, help="Ontology pack name (configs/synth/ontology[.local])")) -> None:
    """J0: run one arm over the ingested corpus, stage by stage."""
    result = _orchestrator().run(arm=arm, config_path=config, doc_ids=docs, resume=resume,
                                 relation_mode=relation_mode, scripted=scripted, gliner_mode=gliner,
                                 ontology=ontology)
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def smoke(config: Optional[Path] = ConfigOpt,
          endpoint: Optional[List[str]] = typer.Option(None, help="Endpoints to test (default t1_micro, t2)")) -> None:
    """P1-M2: live smoke test of the local model endpoints; recommends a structured mode."""
    from synth.gateway.smoke import smoke as run_smoke
    from synth.store import open_store

    cfg = load_config(config)
    try:
        store = open_store(cfg.db_url())
        store.table_names()
    except Exception:
        store = None
    result = run_smoke(cfg, store, endpoints=endpoint or ["t1_micro", "t2"])
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def audit(run_id: str = typer.Option(..., "--run"), config: Optional[Path] = ConfigOpt) -> None:
    """Provenance, coverage and budget audits (P1 §13). Exit 1 on any failure."""
    report = _orchestrator().audit(run_id, config_path=config)
    typer.echo(json.dumps(report, indent=2, default=str))
    raise typer.Exit(0 if report.get("passed") else 1)


@app.command("eval")
def eval_cmd(run_id: str = typer.Option(..., "--run"), config: Optional[Path] = ConfigOpt) -> None:
    """U12: score a run against the promoted gold set."""
    typer.echo(json.dumps(_orchestrator().evaluate(run_id, config_path=config), indent=2, default=str))


@app.command()
def report(runs: List[str] = typer.Option(..., "--runs"), config: Optional[Path] = ConfigOpt,
           out: Optional[Path] = typer.Option(None)) -> None:
    """U12: scorecard (.md + .html) across arms."""
    typer.echo(json.dumps(_orchestrator().report(runs, config_path=config, out=out), indent=2, default=str))


@app.command()
def view(run_id: str = typer.Option(..., "--run"), config: Optional[Path] = ConfigOpt,
         out: Optional[Path] = typer.Option(None)) -> None:
    """J2: static source viewer for a run."""
    typer.echo(str(_orchestrator().view(run_id, config_path=config, out=out)))


@app.command()
def export(run_id: str = typer.Option(..., "--run"), config: Optional[Path] = ConfigOpt,
           out: Optional[Path] = typer.Option(None)) -> None:
    """J3: graph export contract (nodes.json, edges.json, measures.json, GraphML)."""
    typer.echo(str(_orchestrator().export(run_id, config_path=config, out=out)))


@app.command()
def serve(config: Optional[Path] = ConfigOpt, host: str = "127.0.0.1", port: int = 8765) -> None:
    """J1: read-only Store Query API over HTTP."""
    _orchestrator().serve(config_path=config, host=host, port=port)


@app.command()
def speedtest(base_url: str = typer.Option(..., "--base-url"), model: str = typer.Option(..., "--model"),
              api_key_env: Optional[str] = typer.Option(None, "--api-key-env"),
              config: Optional[Path] = ConfigOpt) -> None:
    """Time one short structured reply from a model server (latency, tokens/s), through the gateway."""
    import time

    from synth.config import EndpointConfig
    from synth.gateway import Gateway, GatewayError
    from synth.gateway.smoke import QUESTION, SmokeOut
    from synth.prompts import load_prompt

    cfg = load_config(config)
    out: dict = {"model": model, "base_url": base_url, "ok": False}
    for mode in ("strict_schema", "json_then_validate"):
        trial = cfg.model_copy(deep=True)
        trial.model_settings.endpoints["probe"] = EndpointConfig(base_url=base_url, model=model,
                                                                 api_key_env=api_key_env, structured_mode=mode)
        started = time.monotonic()
        try:
            res = Gateway(trial).call(endpoint="probe", prompt=load_prompt("smoke.v1"), user=QUESTION,
                                      output_model=SmokeOut, cap_in=200, cap_out=400, stage="SPEEDTEST",
                                      module="CONSOLE", unit_id=model)
        except GatewayError as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        seconds = time.monotonic() - started
        out.update(ok=True, structured_mode=mode, latency_ms=int(seconds * 1000), tokens_in=res.tokens_in,
                   tokens_out=res.tokens_out, tokens_per_s=round(res.tokens_out / seconds, 1) if seconds else None,
                   error=None)
        break
    typer.echo(json.dumps(out, indent=2))
    raise typer.Exit(0 if out["ok"] else 1)


@app.command()
def engines() -> None:
    """List the registered engines (pipelines) and the model slots each needs."""
    from synth import engines as registry

    typer.echo(json.dumps([{k: e.as_dict()[k] for k in ("id", "name", "status", "default", "tagline", "slots")}
                           for e in registry.ENGINES.values()], indent=2))


@app.command()
def console(config: Optional[Path] = ConfigOpt, host: str = "127.0.0.1", port: int = 8765,
            browser: bool = typer.Option(True, help="Open the console in the default browser"),
            demo: bool = typer.Option(False, help="In-memory demo run of the sample corpus (no models, no database)")) -> None:
    """Synth Console: graph explorer, documents, review queue, metrics and run control (local UI)."""
    import threading
    import webbrowser

    import uvicorn

    from synth.console.server import create_console, demo_store
    from synth.store import open_store

    cfg = load_config(config)
    if demo:
        typer.echo("Demo mode: building an in-memory scripted run of the sample corpus …")
        app_ = create_console(demo_store(), cfg, read_only=True)
    else:
        app_ = create_console(open_store(cfg.db_url()), cfg)
    url = f"http://{host}:{port}/"
    typer.echo(f"Synth Console on {url}  (Ctrl+C to stop)")
    if browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app_, host=host, port=port, log_level="warning")


@gold_app.command("prefill")
def gold_prefill(doc_id: str = typer.Option(..., "--doc"), first: int = typer.Option(...),
                 last: int = typer.Option(...), config: Optional[Path] = ConfigOpt) -> None:
    """Draft a gold passage with t3_only, for a human to edit."""
    typer.echo(str(_orchestrator().gold("prefill", config_path=config, doc_id=doc_id, first=first, last=last)))


@gold_app.command("frame")
def gold_frame(out: Path = typer.Option(Path("docs/data-frame"), help="Folder to write frame.json and gold.schema.json")) -> None:
    """Export the data frame (what Synth extracts) and the gold-file JSON Schema, for test-data authors."""
    from synth.eval.frame import export_frame

    typer.echo(json.dumps({k: str(v) for k, v in export_frame(out).items()}, indent=2))


@gold_app.command("validate")
def gold_validate(path: Path, config: Optional[Path] = ConfigOpt) -> None:
    """Locate every quote of a gold file; exit 1 if any is not found."""
    result = _orchestrator().gold("validate", config_path=config, path=path)
    typer.echo(json.dumps(result, indent=2, default=str))
    raise typer.Exit(0 if result.get("ok") else 1)


@gold_app.command("promote")
def gold_promote(path: Path, config: Optional[Path] = ConfigOpt,
                 annotator: str = typer.Option("unknown"), blind: bool = typer.Option(False)) -> None:
    """Store a validated gold file in L8."""
    typer.echo(str(_orchestrator().gold("promote", config_path=config, path=path,
                                        annotator=annotator, blind=blind)))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
