"""Add environment and unique constraint to Secret

Revision ID: c4620fec7efc
Revises: db946949e584
Create Date: 2024-09-03 16:57:04.031659

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4620fec7efc"
down_revision: str | None = "db946949e584"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "secret",
        sa.Column("environment", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    )
    op.create_unique_constraint(
        "uq_secret_name_env_owner", "secret", ["name", "environment", "owner_id"]
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint("uq_secret_name_env_owner", "secret", type_="unique")
    op.drop_column("secret", "environment")
    # ### end Alembic commands ###
