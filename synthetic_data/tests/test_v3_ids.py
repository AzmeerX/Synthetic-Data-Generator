"""Phase A — deterministic identifiers, stability and collision policy."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys

import pytest

from synthetic_data.model import IdCollisionError, IdRegistry, derive_id, seed_key
from synthetic_data.model.values import CANONICAL_ID_PATTERN, PARSER_SENTENCE_PATTERN

SPECS = [
    ("doc", "00001"),
    ("sec", "00001/01"),
    ("blk", "00001/01.01"),
    ("ent:company", "company:0001"),
    ("fact:edge", "00001:01"),
]


def _registry(**kwargs) -> IdRegistry:
    reg = IdRegistry(seed=7, corpus_name="v3_test", **kwargs)
    reg.register_many(SPECS)
    return reg


def test_same_seed_and_corpus_produce_identical_ids():
    assert _registry().mapping() == _registry().mapping()


def test_seed_changes_ids():
    other = IdRegistry(seed=8, corpus_name="v3_test")
    other.register_many(SPECS)
    assert other.mapping() != _registry().mapping()


def test_corpus_name_changes_ids():
    other = IdRegistry(seed=7, corpus_name="v3_other")
    other.register_many(SPECS)
    assert other.mapping() != _registry().mapping()


def test_registration_order_does_not_change_ids():
    forward = _registry()
    backward = IdRegistry(seed=7, corpus_name="v3_test")
    backward.register_many(list(reversed(SPECS)))
    assert forward.mapping() == backward.mapping()


def test_ids_are_readable_and_carry_a_hash_tag():
    mapping = _registry().mapping()
    for key, value in mapping.items():
        assert value.startswith(f"{key}:")          # readable path is embedded
        tag = value.rsplit(":", 1)[1]
        assert len(tag) == 13 and re.fullmatch(r"[a-z2-7]+", tag)


def test_unregistered_scope_raises():
    reg = _registry()
    reg.build()
    with pytest.raises(KeyError):
        reg.id_for("doc", "99999")


def test_register_after_build_is_refused():
    reg = _registry()
    reg.build()
    with pytest.raises(RuntimeError):
        reg.register("doc", "99999")


def test_deterministic_across_processes():
    code = (
        "import hashlib,json;"
        "from synthetic_data.model import IdRegistry;"
        "r=IdRegistry(seed=7,corpus_name='v3_test');"
        "r.register_many([('doc','00001'),('ent:company','company:0001')]);"
        "print(hashlib.sha256(json.dumps(r.mapping(),sort_keys=True).encode()).hexdigest())"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]))
    assert out.returncode == 0, out.stderr
    in_process = hashlib.sha256(
        json.dumps(_two_spec_mapping(), sort_keys=True).encode()).hexdigest()
    assert out.stdout.strip() == in_process


def _two_spec_mapping() -> dict:
    reg = IdRegistry(seed=7, corpus_name="v3_test")
    reg.register_many([("doc", "00001"), ("ent:company", "company:0001")])
    return reg.mapping()


def _colliding_pair(digest_size: int = 1):
    """Find two scope paths that collide under a deliberately small digest."""
    key = seed_key(7, "v3_test")
    seen: dict[str, str] = {}
    for n in range(10_000):
        scope = f"s{n}"
        tag = derive_id("x", scope, key, digest_size=digest_size).rsplit(":", 1)[1]
        if tag in seen:
            return ("x", seen[tag]), ("x", scope)
        seen[tag] = scope
    raise AssertionError("no collision found")


def test_collision_is_detected_and_resolved_deterministically():
    pair = _colliding_pair()
    first = IdRegistry(seed=7, corpus_name="v3_test", digest_size=1)
    first.register_many([pair[0], pair[1], *SPECS])
    second = IdRegistry(seed=7, corpus_name="v3_test", digest_size=1)
    second.register_many([pair[1], pair[0], *SPECS])
    assert first.collisions, "expected the crafted pair to collide"
    mapping = first.mapping()
    assert len(set(mapping.values())) == len(mapping)
    assert len({v.rsplit(":", 1)[1] for v in mapping.values()}) == len(mapping)
    assert mapping == second.mapping()


def test_collision_resolution_is_order_independent():
    pair = _colliding_pair()
    forward = IdRegistry(seed=7, corpus_name="v3_test", digest_size=1)
    forward.register_many([pair[0], pair[1]])
    backward = IdRegistry(seed=7, corpus_name="v3_test", digest_size=1)
    backward.register_many([pair[1], pair[0]])
    assert forward.mapping() == backward.mapping()
    assert [c.as_dict() for c in forward.collisions] == [c.as_dict() for c in backward.collisions]


def test_collision_record_names_the_scopes():
    pair = _colliding_pair()
    reg = IdRegistry(seed=7, corpus_name="v3_test", digest_size=1)
    reg.register_many([pair[0], pair[1]])
    assert reg.collisions
    collision = reg.collisions[0]
    assert len(collision.scopes) == 2
    assert set(collision.resolved) == set(collision.scopes)
    assert len(set(collision.resolved.values())) == 2


def test_canonical_and_parser_identifiers_are_disjoint():
    canonical = re.compile(CANONICAL_ID_PATTERN)
    parser = re.compile(PARSER_SENTENCE_PATTERN)
    assert canonical.match("blk:00001:01.01:abcdef") is not None
    assert canonical.match("s19") is None
    assert parser.match("s19") is not None
    assert parser.match("blk:00001:01.01:abcdef") is None


def test_derive_id_is_pure():
    key = seed_key(7, "v3_test")
    assert derive_id("doc", "00001", key) == derive_id("doc", "00001", key)
    assert derive_id("doc", "00001", key) != derive_id("doc", "00002", key)


def test_ids_are_stable_when_a_sibling_is_added_or_removed():
    """Dropping an optional section must not renumber the survivors."""
    base = [("doc", "00001"), ("sec", "00001/01"), ("blk", "00001/01.01")]
    with_optional = base + [("sec", "00001/02"), ("blk", "00001/02.01")]
    small = IdRegistry(seed=7, corpus_name="v3_test")
    small.register_many(base)
    large = IdRegistry(seed=7, corpus_name="v3_test")
    large.register_many(with_optional)
    for key in small.mapping():
        assert small.mapping()[key] == large.mapping()[key]


def test_ids_do_not_depend_on_renderer_output():
    """The registry knows nothing about formats; adding one changes no canonical id."""
    before = _registry().mapping()
    after = _registry().mapping()
    assert before == after
    assert not any(value.startswith("rnd:") for value in before.values())


def test_bad_inputs_are_rejected():
    with pytest.raises(ValueError):
        seed_key(7, "")
    with pytest.raises(ValueError):
        derive_id("", "x", seed_key(7, "c"))
    with pytest.raises(ValueError):
        derive_id("doc", "", seed_key(7, "c"))
    with pytest.raises(ValueError):
        derive_id("doc", "x", seed_key(7, "c"), attempt=-1)
