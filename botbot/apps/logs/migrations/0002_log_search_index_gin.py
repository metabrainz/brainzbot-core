# Generated manually for feature/populate-search-index

from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    """Add a GIN index on ``logs_log.search_index`` for fast full-text search.

    PostgreSQL's Generalized Inverted Index (GIN) is the recommended index
    type for ``tsvector`` columns.  Without it every full-text query would
    perform a sequential scan of the entire ``logs_log`` table.
    """

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
