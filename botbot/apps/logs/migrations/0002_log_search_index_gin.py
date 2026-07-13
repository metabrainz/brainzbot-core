"""
Migration 0002: Add a GIN index on ``logs_log.search_index``.

Why GIN?
--------
PostgreSQL's Generalized Inverted Index (GIN) is the canonical index type
for ``tsvector`` columns.  It stores a mapping from each lexeme to the set
of rows that contain it, making full-text queries orders of magnitude faster
than a sequential scan on large tables (millions of rows).

Without this index every ``search_index = <tsquery>`` filter would require
reading every row in the ``logs_log`` table, which is unacceptable for a
production IRC log archive.

The index is declared here (rather than in ``Log.Meta.indexes``) so that it
appears explicitly in the migration history and can be managed independently
of any future model changes.
"""

from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    """Add a GIN index on ``logs_log.search_index`` for fast full-text search."""

    dependencies = [
        ("logs", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="log",
            index=GinIndex(
                fields=["search_index"],
                name="logs_log_search_idx",
            ),
        ),
    ]
