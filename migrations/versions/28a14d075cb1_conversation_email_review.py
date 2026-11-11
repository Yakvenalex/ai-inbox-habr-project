"""Bind chat confirmation to a reviewed outgoing email version."""

import sqlalchemy as sa
from alembic import op

revision = "28a14d075cb1"
down_revision = "bdf0d9df1ef2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("pending_email", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("pending_email")
