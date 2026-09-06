"""Personal instances, personal API keys and idempotent template provisioning."""

from alembic import op

revision = "0029_private_instances"
down_revision = "0028_env_vars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SET NULL would accidentally publish a deleted user's private instance.
    op.execute("ALTER TABLE containers ADD COLUMN owner_user_id TEXT REFERENCES users(id)")
    op.execute("CREATE INDEX idx_containers_owner ON containers(tenant_id, owner_user_id)")
    op.execute("ALTER TABLE api_keys ADD COLUMN owner_user_id TEXT REFERENCES users(id)")
    op.execute("""
        CREATE TABLE user_agent_bindings (
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            user_id TEXT NOT NULL REFERENCES users(id),
            template_id TEXT NOT NULL REFERENCES templates(id),
            container_id TEXT REFERENCES containers(id),
            lease_id TEXT NOT NULL,
            lease_until TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('provisioning', 'ready', 'error')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, user_id, template_id)
        )
    """)


def downgrade() -> None:
    # Never turn private containers into shared containers during rollback.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM containers WHERE owner_user_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM api_keys WHERE owner_user_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Private data exists; restore a pre-migration backup to roll back';
          END IF;
        END $$
    """)
    op.execute("DROP TABLE user_agent_bindings")
    op.execute("ALTER TABLE api_keys DROP COLUMN owner_user_id")
    op.execute("DROP INDEX idx_containers_owner")
    op.execute("ALTER TABLE containers DROP COLUMN owner_user_id")
