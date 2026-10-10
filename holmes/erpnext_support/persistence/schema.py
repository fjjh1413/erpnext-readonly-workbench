"""SQLAlchemy 2 schema shared by SQLite and PostgreSQL."""

from sqlalchemy import JSON, Column, Float, ForeignKey, Index, Integer, MetaData, String, Table

SCHEMA_REVISION = "enterprise_001"
metadata = MetaData()
records = Table("records", metadata,
    Column("id", String(32), primary_key=True), Column("kind", String(32), nullable=False),
    Column("environment_id", String(32)), Column("parent_id", String(32)),
    Column("created_at", String(64), nullable=False), Column("payload", JSON, nullable=False),
    Column("version", Integer, nullable=False, default=1))
Index("records_kind", records.c.kind, records.c.created_at)
Index("records_parent", records.c.parent_id, records.c.kind)
sessions = Table("sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(32), ForeignKey("records.id", ondelete="CASCADE"), nullable=False),
    Column("csrf", String(128), nullable=False), Column("expires_at", String(64), nullable=False))
Index("sessions_user", sessions.c.user_id)
audit_events = Table("audit_events", metadata,
    Column("id", String(32), primary_key=True), Column("at", String(64), nullable=False),
    Column("actor", String(140), nullable=False), Column("action", String(140), nullable=False),
    Column("resource_kind", String(64), nullable=False), Column("resource_id", String(140), nullable=False),
    Column("outcome", String(32), nullable=False), Column("request_id", String(140)), Column("detail", JSON, nullable=False))
Index("audit_events_at", audit_events.c.at)
job_leases = Table("job_leases", metadata,
    Column("task_id", String(32), ForeignKey("records.id", ondelete="CASCADE"), primary_key=True),
    Column("ticket_id", String(32), nullable=False), Column("state", String(32), nullable=False),
    Column("worker_id", String(140)), Column("lease_until", Float), Column("last_dispatched_at", Float),
    Column("created_at", String(64), nullable=False))
Index("job_leases_state", job_leases.c.state, job_leases.c.lease_until)
store_locks = Table("store_locks", metadata, Column("name", String(32), primary_key=True))
usernames = Table("usernames", metadata,
    Column("user_id", String(32), ForeignKey("records.id", ondelete="CASCADE"), primary_key=True),
    Column("username", String(64), nullable=False, unique=True))
schema_info = Table("schema_info", metadata, Column("revision", String(64), primary_key=True))
