"""U12 (audit half): the coverage finalizer and the three run audits (P1 §7, §13).

``synth.audit.coverage.finalize(ctx)`` gives every sentence of the run its terminal
coverage outcome; ``synth.audit.audits.audit_run(store, run_id)`` checks provenance,
coverage and budgets and says ``passed`` or not.
"""
