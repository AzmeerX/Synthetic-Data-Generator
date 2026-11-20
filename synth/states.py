"""State names, stage names and coverage outcomes — spelled once (P1 §7, §11).

Every transition is written to the change log (L4) by ``Tx.update``.
"""

# ---- stages (J0 execution order, P1 §2)
INGEST, GLINER, TIME, MICRO, CONTRACT, T2, RESOLVE, GATE, FRONTIER, COMMIT, FINALIZE = (
    "INGEST", "GLINER", "TIME", "MICRO", "CONTRACT", "T2", "RESOLVE", "GATE",
    "FRONTIER", "COMMIT", "FINALIZE")
ARM_EXTRACT = "ARM_EXTRACT"          # t2_only / t3_only extraction stage
LOCAL = "LOCAL"                      # minus0: the local model draws the standard graph
STAGES = (INGEST, GLINER, TIME, MICRO, CONTRACT, T2, RESOLVE, GATE, FRONTIER, COMMIT, FINALIZE)

ARMS = ("minus0", "full", "t2_only", "t3_only")   # minus0 = GLiNER -> local graph -> frontier review
DERIVED_ARMS = ("t1_only", "t1_t2")

# ---- micro edge
CANDIDATE, CONTRACTED, REJECTED = "CANDIDATE", "CONTRACTED", "REJECTED"
# ---- macro edge
VERIFIED, ESCALATED, COMMITTED, HELD_FOR_HUMAN = "VERIFIED", "ESCALATED", "COMMITTED", "HELD_FOR_HUMAN"
# ---- conflicts (flags of kind 'conflict' / 'inconsistency')
POTENTIAL, FINAL, RESOLVED, IRREDUCIBLE, DISMISSED = "POTENTIAL", "FINAL", "RESOLVED", "IRREDUCIBLE", "DISMISSED"
# ---- entities / entity pairs
ACTIVE, MERGED, APART = "ACTIVE", "MERGED", "APART"
# ---- mentions
OPEN, DISCARDED = "OPEN", "DISCARDED"            # + RESOLVED
# ---- units (J0)
PENDING, DONE, FAILED, DEAD_LETTER = "PENDING", "DONE", "FAILED", "DEAD_LETTER"

# ---- flag kinds
FLAG_INCONSISTENCY = "inconsistency"
FLAG_IDENTITY = "identity_ambiguity"
FLAG_CONFLICT = "conflict"
FLAG_MERGE_BLOCKED = "merge_blocked"

# ---- coverage events (L3, P1 §7)
SEGMENTED = "SEGMENTED"
SKIPPED_FILTER = "SKIPPED_FILTER"
GLINER_SCANNED = "GLINER_SCANNED"
MICRO_READ = "MICRO_READ"
FACT_SENTENCE = "FACT_SENTENCE"
NESTED_SENTENCE = "NESTED_SENTENCE"
T2_READ = "T2_READ"
T3_READ = "T3_READ"
ESCALATION_PENDING = "ESCALATION_PENDING"
UNEXPRESSIBLE = "UNEXPRESSIBLE"
SKIPPED_BUDGET = "SKIPPED_BUDGET"
COVERAGE_FAILED = "FAILED"

# ---- coverage terminal outcomes, in finalizer priority order
FACT_COMMITTED = "FACT_COMMITTED"
FACTS_REJECTED = "FACTS_REJECTED"
READ_NO_FACT = "READ_NO_FACT"
SILENT_DROP = "SILENT_DROP"
TERMINAL_ORDER = (FACT_COMMITTED, HELD_FOR_HUMAN, FACTS_REJECTED, UNEXPRESSIBLE, READ_NO_FACT,
                  SKIPPED_FILTER, SKIPPED_BUDGET, COVERAGE_FAILED, SILENT_DROP)

# ---- gate reason codes (U9)
GATE_REASONS = ("FINAL_CONFLICT", "ER_MIDDLE_BAND", "GROUNDING_FUZZY_LOW", "NESTED_DISAGREEMENT",
                "LOW_CONF_CRITICAL", "INFERRED_CRITICAL", "IDENTITY_UNRESOLVED",
                "T2_INSUFFICIENT_CONTEXT")

TIME_ROLES = ("occurred_at", "valid_from", "valid_to", "as_of", "deadline", "reported_on")
