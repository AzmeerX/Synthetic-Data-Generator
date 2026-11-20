"""P1 initial schema: S1–S5, L1–L8, append-only guards.

Revision 0001 builds the schema from ``synth/store/tables.py`` as it stood at
P1-M0 (decision recorded in docs/DECISIONS.md). Any later schema change gets its
own revision with explicit ``op.*`` calls; this file is never edited.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from synth.store.db import install_guards
    from synth.store.tables import metadata

    bind = op.get_bind()
    metadata.create_all(bind)
    install_guards(bind)


def downgrade() -> None:  # pragma: no cover - append-only by design
    raise RuntimeError("Synth stores are append-only; there is no downgrade from 0001.")
