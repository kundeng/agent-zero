"""Channel-DB migrations (spec 06 task 1.5).

Sibling ``.sql`` files are applied in lexicographic order. Each filename
must start with a three-digit version (e.g. ``001_initial.sql``,
``002_add_chat_name.sql``) — version numbers are pulled from that prefix
and recorded in a ``schema_migrations`` table to make application
idempotent.

See :class:`Migrator` in :mod:`hyperagent0.channels.migrations.migrator`.
"""
