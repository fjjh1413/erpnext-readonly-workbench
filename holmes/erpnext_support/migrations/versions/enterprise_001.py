"""Initial enterprise schema; no legacy file is altered by this revision."""

from alembic import op
import sqlalchemy as sa

revision = "enterprise_001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("records", sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False), sa.Column("environment_id", sa.String(32)),
        sa.Column("parent_id", sa.String(32)), sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False), sa.Column("version", sa.Integer(), nullable=False))
    op.create_index("records_kind", "records", ["kind", "created_at"])
    op.create_index("records_parent", "records", ["parent_id", "kind"])
    op.create_table("sessions", sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("csrf", sa.String(128), nullable=False), sa.Column("expires_at", sa.String(64), nullable=False))
    op.create_index("sessions_user", "sessions", ["user_id"])
    op.create_table("audit_events", sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("at", sa.String(64), nullable=False), sa.Column("actor", sa.String(140), nullable=False),
        sa.Column("action", sa.String(140), nullable=False), sa.Column("resource_kind", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(140), nullable=False), sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(140)), sa.Column("detail", sa.JSON(), nullable=False))
    op.create_index("audit_events_at", "audit_events", ["at"])
    op.create_table("job_leases", sa.Column("task_id", sa.String(32), sa.ForeignKey("records.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ticket_id", sa.String(32), nullable=False), sa.Column("state", sa.String(32), nullable=False),
        sa.Column("worker_id", sa.String(140)), sa.Column("lease_until", sa.Float()), sa.Column("last_dispatched_at", sa.Float()),
        sa.Column("created_at", sa.String(64), nullable=False))
    op.create_index("job_leases_state", "job_leases", ["state", "lease_until"])
    op.create_table("store_locks", sa.Column("name", sa.String(32), primary_key=True))
    op.create_table("usernames", sa.Column("user_id", sa.String(32), sa.ForeignKey("records.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True))
    op.create_table("schema_info", sa.Column("revision", sa.String(64), primary_key=True))
    op.bulk_insert(sa.table("store_locks", sa.column("name", sa.String)), [{"name": "scheduler"}, {"name": "accounts"}])
    op.bulk_insert(sa.table("schema_info", sa.column("revision", sa.String)), [{"revision": revision}])
    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE VIEW audit AS SELECT id,at,actor,action,resource_id AS target FROM audit_events")


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore to a separate database instead")
