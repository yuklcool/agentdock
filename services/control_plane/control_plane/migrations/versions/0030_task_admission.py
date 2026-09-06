"""Record task actors for personal budgets and audit."""
from alembic import op

revision = "0030_task_admission"
down_revision = "0029_private_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workflow_runs ADD COLUMN run_as_role TEXT NOT NULL DEFAULT 'member'")
    op.execute("ALTER TABLE scheduled_tasks ADD COLUMN run_as_role TEXT NOT NULL DEFAULT 'member'")
    op.execute("ALTER TABLE workflow_runs ADD COLUMN run_as_user_id TEXT REFERENCES users(id)")
    op.execute("ALTER TABLE scheduled_tasks ADD COLUMN run_as_user_id TEXT REFERENCES users(id)")
    op.execute("ALTER TABLE tasks ADD COLUMN submitted_by TEXT REFERENCES users(id)")
    op.execute("CREATE INDEX idx_tasks_actor_date ON tasks(tenant_id, submitted_by, created_at)")


def downgrade() -> None:
    op.execute("ALTER TABLE workflow_runs DROP COLUMN run_as_role")
    op.execute("ALTER TABLE scheduled_tasks DROP COLUMN run_as_role")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN run_as_user_id")
    op.execute("ALTER TABLE scheduled_tasks DROP COLUMN run_as_user_id")
    op.execute("DROP INDEX idx_tasks_actor_date")
    op.execute("ALTER TABLE tasks DROP COLUMN submitted_by")
