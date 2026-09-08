"""Optional Nanobot API endpoint on provider credentials."""
from alembic import op

revision = "0031_credential_base_url"
down_revision = "0030_task_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE credentials ADD COLUMN base_url TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE credentials DROP COLUMN base_url")
