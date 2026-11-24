"""Phase G: parser-grounded gold annotations.

``build``     turns a rendered and ingested document into v1 + v2 + alignment.
``validate``  checks the v1 file with the vendored validator and the V3 files here.
``model``     the v2 artifact and the alignment record.
``defects``   the §8.5 detectors that make a bad alignment a build failure.
`report`      per-document and per-corpus numbers for the Phase G report.
"""
from .build import (GoldBuildError, GoldInputs, GoldPair, build_gold, gold_paths,
                    value_kind_of, write_gold_pair)
from .model import AlignmentArtifact, AlignmentUnit, GoldV2
from .validate import (GoldReport, validate_alignment, validate_pair, validate_v1,
                       validate_v2)

__all__ = [
    "GoldBuildError", "GoldInputs", "GoldPair", "build_gold", "gold_paths",
    "write_gold_pair", "value_kind_of",
    "AlignmentArtifact", "AlignmentUnit", "GoldV2",
    "GoldReport", "validate_alignment", "validate_pair", "validate_v1", "validate_v2",
]
