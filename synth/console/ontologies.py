"""Ontologies in the console: list, edit, paste in and save extraction vocabularies.

Purpose: let an operator define what Synth extracts — entity classes and their
subtypes, the GLiNER labels that find them, relations with their allowed ends,
aliases, functional/critical relations, claim statuses, logic links, time and
name rules — as a saved template, without hand-editing YAML. Built-in packs
(``configs/synth/ontology``) are read-only templates; the user's own live in
``configs/synth/ontology.local`` (gitignored). A pack is validated by the same
loader the pipeline uses before it is saved. A run picks its pack by name
(``synth run --ontology``); the run manifest records which one.
Contract: :func:`list_ontologies`, :func:`get_ontology`, :func:`save_ontology`,
:func:`copy_ontology`, :func:`delete_ontology`, :func:`validate`,
:func:`parse_paste`, :func:`add_routes`. Stores written: none (files only).
Token caps: none. Spec: P1 §8 (ontology pack).
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, FastAPI, HTTPException

from synth.config import ONTOLOGY_DIR, load_config, local_ontology_dir
from synth.ontology import ROLE_LABELS, TIME_LABELS, load_ontology

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
IDENT = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
KEYS = ("pack", "version", "gliner_labels", "entity_types", "relations", "relation_aliases", "functional_relations",
        "critical_relations", "proposition_status", "link_types", "time", "honorifics_strip", "honorifics_keep")


DEFAULT_STATUS = ("held", "found", "resolved", "ordered", "declared", "argued", "alleged", "admitted", "denied",
                  "proposed", "reported", "stated")
DEFAULT_LINKS = ("because", "unless", "provided_that", "notwithstanding", "therefore", "contradicts", "supports",
                 "conditional_on")


class OntologyError(ValueError):
    pass


def snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")


def slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9._-]+", "-", str(text or "").strip().lower()).strip("-._")
    return out[:48]


# ------------------------------------------------------------------ files
def _builtin(name: str) -> Path:
    return ONTOLOGY_DIR / f"{name}.yaml"


def _local(name: str) -> Path:
    return local_ontology_dir() / f"{name}.yaml"


def _check_name(name: str) -> str:
    if not NAME.match(name or ""):
        raise OntologyError("ontology names use a-z, 0-9, '.', '_' or '-' (max 48 characters)")
    return name


def _read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _default_name() -> str:
    return Path(load_config().ontology).stem


def _summary(name: str, path: Path, builtin: bool) -> dict[str, Any]:
    data = _read(path)
    relations = sum(len(v or {}) for v in (data.get("relations") or {}).values())
    return {"name": name, "pack": data.get("pack", name), "version": data.get("version", 1), "builtin": builtin,
            "editable": not builtin, "default": name == _default_name(),
            "entity_classes": len(data.get("entity_types") or {}),
            "entity_types": sum(len(v or []) for v in (data.get("entity_types") or {}).values()),
            "relations": relations, "gliner_labels": len(data.get("gliner_labels") or [])}


def list_ontologies() -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(ONTOLOGY_DIR.glob("*.yaml")):
        out[path.stem] = _summary(path.stem, path, True)
    local = local_ontology_dir()
    if local.exists():
        for path in sorted(local.glob("*.yaml")):
            out[path.stem] = _summary(path.stem, path, False)          # a local pack of the same name wins
    return list(out.values())


def get_ontology(name: str) -> dict[str, Any]:
    _check_name(name)
    local, builtin = _local(name), _builtin(name)
    path = local if local.exists() else builtin
    if not path.exists():
        raise KeyError(name)
    data = _read(path)
    return {**_summary(name, path, path == builtin), "data": data, "yaml": path.read_text(encoding="utf-8")}


# ------------------------------------------------------------------ validation
def normalise(data: dict[str, Any]) -> dict[str, Any]:
    """Tidy what the editor or a paste-in sends: snake_case names, lists de-duplicated, empty bits dropped."""
    d = {k: data.get(k) for k in KEYS if data.get(k) not in (None, "", [], {})}
    d["pack"] = snake(d.get("pack") or "custom") or "custom"
    d["version"] = int(d.get("version") or 1)

    def uniq(items, fn=lambda x: x):
        seen, out = set(), []
        for x in items or []:
            v = fn(str(x).strip())
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    d["entity_types"] = {snake(c): uniq(subs or [], snake) for c, subs in (d.get("entity_types") or {}).items()
                         if snake(c)}
    d["relations"] = {snake(g) or "general": {snake(r): [snake(e[0]) or "any", snake(e[1]) or "any"]
                                              for r, e in (rels or {}).items() if snake(r)}
                      for g, rels in (d.get("relations") or {}).items()}
    d["relations"] = {g: rels for g, rels in d["relations"].items() if rels}
    labels = uniq(d.get("gliner_labels") or [], lambda x: x.lower())
    if not labels:                                             # derive: every class and type, as words
        labels = uniq([w.replace("_", " ") for c, subs in d["entity_types"].items() for w in [c, *subs]])
    d["gliner_labels"] = labels
    d["relation_aliases"] = {str(k).strip().lower(): snake(v) for k, v in (d.get("relation_aliases") or {}).items()
                             if str(k).strip() and snake(v)}
    for key in ("functional_relations", "critical_relations", "proposition_status", "link_types"):
        if d.get(key):
            d[key] = uniq(d[key], snake)
    d.setdefault("proposition_status", list(DEFAULT_STATUS))   # claims and logic work out of the box
    d.setdefault("link_types", list(DEFAULT_LINKS))
    time = d.get("time") or {}
    d["time"] = {"fiscal_year_start_month": int(time.get("fiscal_year_start_month") or 1),
                 "time_roles": uniq(time.get("time_roles") or ["occurred_at", "valid_from", "valid_to", "as_of"],
                                    snake)}
    for key in ("honorifics_strip", "honorifics_keep"):
        if d.get(key):
            d[key] = uniq(d[key], lambda x: x.lower().rstrip("."))
    return {k: d[k] for k in KEYS if k in d}


def validate(data: dict[str, Any]) -> list[str]:
    """Problems that would break a run (empty list = valid). Runs the pipeline's own loader too."""
    problems = []
    types = data.get("entity_types") or {}
    if not types:
        problems.append("add at least one entity class")
    classes = set(types)
    subtypes = {s for subs in types.values() for s in subs}
    for cls in classes | subtypes:
        if not IDENT.match(cls):
            problems.append(f"entity name {cls!r}: use letters, digits and _ (start with a letter)")
    clash = classes & {s for c, subs in types.items() for s in subs if s != c}
    if clash:
        problems.append(f"names used both as a class and as another class's subtype: {', '.join(sorted(clash))}")
    rel_names = []
    for rels in (data.get("relations") or {}).values():
        for rel, (src, dst) in rels.items():
            rel_names.append(rel)
            if not IDENT.match(rel):
                problems.append(f"relation {rel!r}: use letters, digits and _ (start with a letter)")
            for end in (src, dst):
                if end != "any" and end not in classes:
                    owner = next((c for c, subs in types.items() if end in subs), None)
                    problems.append(f"relation {rel}: {end!r} is a type of {owner}; relation ends are classes, "
                                    f"so use {owner!r}" if owner else
                                    f"relation {rel}: end {end!r} is not an entity class (or 'any')")
    if not rel_names:
        problems.append("add at least one relation")
    dupes = {r for r in rel_names if rel_names.count(r) > 1}
    if dupes:
        problems.append(f"relations defined twice: {', '.join(sorted(dupes))}")
    known = set(rel_names)
    for alias, target in (data.get("relation_aliases") or {}).items():
        if target not in known:
            problems.append(f"alias {alias!r} points to unknown relation {target!r}")
    for key in ("functional_relations", "critical_relations"):
        for rel in data.get(key) or []:
            if rel not in known:
                problems.append(f"{key.replace('_', ' ')}: unknown relation {rel!r}")
    for label in data.get("gliner_labels") or []:
        if label in TIME_LABELS or label in ROLE_LABELS or label == "money amount":
            continue
        key = snake(label)
        if key not in classes and key not in subtypes:
            problems.append(f"GLiNER label {label!r} does not name an entity class or type "
                            "(labels find entities: each must match one, with spaces for _)")
    month = (data.get("time") or {}).get("fiscal_year_start_month", 1)
    if not 1 <= int(month) <= 12:
        problems.append("fiscal year start month must be 1–12")
    if not problems:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "check.yaml"
            path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
            try:
                load_ontology(path)
            except (ValueError, TypeError, KeyError, IndexError) as exc:   # the pipeline's own loader decides
                problems.append(f"the pipeline could not load it: {exc}")
    return problems


def save_ontology(name: str, data: dict[str, Any]) -> dict[str, Any]:
    _check_name(name)
    if _builtin(name).exists() and not _local(name).exists():
        raise OntologyError(f"{name} is a built-in template (read-only): save it under a new name")
    clean = normalise(data)
    if clean["pack"] == "custom":                             # an unnamed pack takes the name it is saved under
        clean["pack"] = snake(name.split(".")[0]) or "custom"
    problems = validate(clean)
    if problems:
        raise OntologyError("; ".join(problems))
    path = _local(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"# Ontology pack '{name}', saved from the Synth Console. Edit here or in the console.\n"
    path.write_text(header + yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return get_ontology(name)


def copy_ontology(source: str, name: str) -> dict[str, Any]:
    src = get_ontology(source)
    name = _check_name(slug(name))
    if _local(name).exists() or _builtin(name).exists():
        raise OntologyError(f"an ontology named {name} already exists")
    return save_ontology(name, {**src["data"], "pack": snake(name.split(".")[0])})


def delete_ontology(name: str) -> None:
    _check_name(name)
    path = _local(name)
    if not path.exists():
        raise OntologyError("only your own ontologies can be deleted (built-in templates stay)")
    path.unlink()


# ------------------------------------------------------------------ paste-ins
_ARROW = re.compile(r"^\s*(?:relation\s+)?([\w ./-]+?)\s*:\s*([\w ]+?)\s*(?:->|→|=>|to)\s*([\w ]+?)\s*(\*?!?|!?\*?)\s*$",
                    re.IGNORECASE)


def parse_paste(text: str) -> dict[str, Any]:
    """A full pack as YAML or JSON, or the quick format::

        entity person: judge, lawyer          (a class and its types; "entity company" for no types)
        relation founded: person -> organization    (append * for critical, ! for functional)
        alias board member of: director_of
        label: money amount                    (extra GLiNER labels; each names a class or type)
        status: alleged, denied                (claim statuses)
        link: because, unless                  (logic link types)
    """
    text = (text or "").strip()
    if not text:
        raise OntologyError("nothing to read: paste YAML, JSON or the quick format")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise OntologyError(f"not valid JSON: {exc}") from None
        return normalise(data)
    quick = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if quick and all(re.match(r"^\s*(entity|relation|alias|label|labels|status|link|pack)\b", ln, re.IGNORECASE)
                     for ln in quick):
        return normalise(_quick(quick))
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise OntologyError(f"not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise OntologyError("expected a pack (entity_types, relations, …) or the quick format "
                            "(lines starting with entity / relation / alias / label / status / link)")
    return normalise(data)


def _items(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;]", text or "") if x.strip()]


def _quick(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {"entity_types": {}, "relations": {"general": {}}, "relation_aliases": {},
                            "gliner_labels": [], "functional_relations": [], "critical_relations": [],
                            "proposition_status": [], "link_types": []}
    for raw in lines:
        m = re.match(r"\s*([A-Za-z]+)\s*:?\s*(.*)$", raw)
        kind, rest = m.group(1).lower(), m.group(2)
        if kind == "entity":
            name, _, subs = rest.partition(":")
            data["entity_types"][snake(name)] = [snake(s) for s in _items(subs)]
        elif kind == "relation":
            m = _ARROW.match(raw)
            if not m:
                raise OntologyError(f"cannot read {raw.strip()!r}: write  relation name: source -> target")
            rel, src, dst, marks = snake(m.group(1)), snake(m.group(2)), snake(m.group(3)), m.group(4) or ""
            data["relations"]["general"][rel] = [src, dst]
            if "*" in marks:
                data["critical_relations"].append(rel)
            if "!" in marks:
                data["functional_relations"].append(rel)
        elif kind == "alias":
            phrase, _, target = rest.rpartition(":")
            data["relation_aliases"][phrase.strip().lower()] = snake(target)
        elif kind in ("label", "labels"):
            data["gliner_labels"] += [x.lower() for x in _items(rest)]
        elif kind == "status":
            data["proposition_status"] += [snake(x) for x in _items(rest)]
        elif kind == "link":
            data["link_types"] += [snake(x) for x in _items(rest)]
        elif kind == "pack":
            data["pack"] = snake(rest)
    if data["gliner_labels"]:                                    # label lines add to the derived labels
        derived = [w.replace("_", " ") for c, subs in data["entity_types"].items() for w in [c, *subs]]
        data["gliner_labels"] = derived + data["gliner_labels"]
    return data


# ------------------------------------------------------------------ routes
def add_routes(app: FastAPI, read_only: bool) -> None:
    def guard() -> None:
        if read_only:
            raise HTTPException(status_code=403, detail="demo mode: changes are disabled")

    def bad(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/ontologies")
    def ontologies() -> list[dict[str, Any]]:
        return list_ontologies()

    @app.get("/api/ontologies/{name}")
    def ontology(name: str) -> dict[str, Any]:
        try:
            return get_ontology(name)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no ontology named {name}") from None
        except OntologyError as exc:
            raise bad(exc) from None

    @app.put("/api/ontologies/{name}")
    def put_ontology(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        guard()
        try:
            data = parse_paste(body["yaml"]) if isinstance(body.get("yaml"), str) else body.get("data") or {}
            return save_ontology(name, data)
        except OntologyError as exc:
            raise bad(exc) from None

    @app.post("/api/ontologies/{name}/copy")
    def copy(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        guard()
        try:
            return copy_ontology(name, body.get("name") or "")
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no ontology named {name}") from None
        except OntologyError as exc:
            raise bad(exc) from None

    @app.delete("/api/ontologies/{name}")
    def delete(name: str) -> dict[str, Any]:
        guard()
        try:
            delete_ontology(name)
        except OntologyError as exc:
            raise bad(exc) from None
        return {"deleted": name}

    @app.post("/api/ontologies/parse")
    def parse(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Read a paste-in (YAML, JSON or the quick format) without saving; returns the pack and its problems."""
        try:
            data = parse_paste(body.get("text") or "")
        except OntologyError as exc:
            raise bad(exc) from None
        return {"data": data, "problems": validate(data)}

    @app.post("/api/ontologies/validate")
    def check(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        data = normalise(body.get("data") or {})
        return {"data": data, "problems": validate(data)}
