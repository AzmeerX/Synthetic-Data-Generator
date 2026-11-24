"""Phase I: benchmark prediction contract, reader and evaluator.

Importable without the generator: the evaluator reads published gold and QA
artifacts and scores prediction bundles submitted through
:mod:`synthetic_data.benchmark.schema`.
"""

from synthetic_data.benchmark.adapter import (JsonPredictionAdapter, MappingAdapter,
                                             PredictionAdapter, load_bundle)
from synthetic_data.benchmark.evaluate import (DocumentScore, aggregate_by_question,
                                              evaluate_corpus, evaluate_document)
from synthetic_data.benchmark.gold import GoldDocument, load_gold_corpus
from synthetic_data.benchmark.schema import (PREDICTION_SCHEMA_VERSION,
                                             PredictedAnswer, PredictedDocument,
                                             PredictedEntity, PredictedEvidence,
                                             PredictedRelation, PredictionBundle)

__all__ = ["PREDICTION_SCHEMA_VERSION", "PredictionBundle", "PredictedDocument",
           "PredictedEntity", "PredictedRelation", "PredictedEvidence",
           "PredictedAnswer", "PredictionAdapter", "JsonPredictionAdapter",
           "MappingAdapter", "load_bundle", "GoldDocument", "load_gold_corpus",
           "DocumentScore", "evaluate_document", "evaluate_corpus",
           "aggregate_by_question"]
