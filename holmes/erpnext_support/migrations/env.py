"""Alembic environment accepts an already-bound connection, never an env URL."""

from alembic import context

from holmes.erpnext_support.persistence.schema import metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use the server-bound persistence migration command")
context.configure(connection=connection, target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
