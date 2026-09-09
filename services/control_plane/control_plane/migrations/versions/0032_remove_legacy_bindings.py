"""Remove retired user-bound API credentials and template bindings.

0029 now installs only container ownership on fresh databases. Existing installs
keep their old schema until this revision removes it. Delete user-bound keys
BEFORE dropping their ownership column: they must never become workspace keys.
"""

from alembic import op

revision = "0032_remove_legacy_bindings"
down_revision = "0031_credential_base_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'api_keys'
              AND column_name = 'owner_user_id'
          ) THEN
            DELETE FROM api_keys WHERE owner_user_id IS NOT NULL;
            ALTER TABLE api_keys DROP COLUMN owner_user_id;
          END IF;
        END $$
    """)
    # DROP TABLE also removes its indexes and outgoing constraints. No CASCADE:
    # an unexpected external dependency must not delete unrelated data.
    op.execute("DROP TABLE IF EXISTS user_agent_bindings")


def downgrade() -> None:
    # Revised historical migrations do not define these retired structures.
    # Deliberately do not recreate deleted credentials or bindings. Restoring an
    # older application binary requires a pre-upgrade backup, not this downgrade.
    pass
