"""Private container ownership."""

from alembic import op

revision = "0029_private_instances"
down_revision = "0028_env_vars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SET NULL would accidentally publish a deleted user's private instance.
    op.execute("ALTER TABLE containers ADD COLUMN owner_user_id TEXT REFERENCES users(id)")
    op.execute("CREATE INDEX idx_containers_owner ON containers(tenant_id, owner_user_id)")


def downgrade() -> None:
    # Never turn private containers into shared containers during rollback.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM containers WHERE owner_user_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Private data exists; restore a pre-migration backup to roll back';
          END IF;
        END $$
    """)
    op.execute("DROP INDEX idx_containers_owner")
    op.execute("ALTER TABLE containers DROP COLUMN owner_user_id")
