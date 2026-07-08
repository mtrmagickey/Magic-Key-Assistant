"""
core — Discord-free business logic layer.

This package contains entities, services, and workflows that have
**zero dependency on Discord**.  Everything here should be testable
with plain pytest and an in-memory SQLite database.

Milestone M1 will progressively migrate logic out of the cogs
and into this package.
"""
