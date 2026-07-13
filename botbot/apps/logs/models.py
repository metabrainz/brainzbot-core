"""
Models for the IRC log storage layer.

Design notes
------------
* ``Log`` stores every IRC event received by the Go bot daemon.
* Full-text search is backed by a PostgreSQL ``tsvector`` column
  (``search_index``) that is updated automatically on every save via
  ``Log.update_search_field()``.  A GIN index on that column
  (added in migration 0002) keeps search fast even on large tables.
* Redacted log lines (nicks listed in ``settings.EXCLUDE_NICKS``) are
  intentionally excluded from the search index so they can never be
  surfaced through a query.
"""

from django.conf import settings
from django.contrib.postgres.search import (
    SearchQuery,
    SearchRank,
    SearchVector,
    SearchVectorField,
)
from django.db import models
from django.template.loader import render_to_string
from django.urls import reverse

from botbot.apps.bots.utils import channel_url_kwargs


REDACTED_TEXT = "[redacted]"

MSG_TMPL = {
    "JOIN": "{nick} joined the channel",
    "NICK": "{nick} is now known as {text}",
    "QUIT": "{nick} has quit",
    "PART": "{nick} has left the channel",
    "ACTION": "{nick} {text}",
    "SHUTDOWN": "-- BotBot disconnected, possible missing messages --",
}


class LogManager(models.Manager):
    """Custom manager that exposes full-text search on top of the ORM.

    Usage::

        Log.objects.search("django release")
        Log.objects.search("django release").filter(channel=channel)
    """

    def search(self, query: str):
        """Return logs that match *query* using the pre-built ``search_index``.

        Results are ordered by relevance (highest ``SearchRank`` first) and
        then by recency so that equally-ranked hits show the newest messages
        at the top.

        Args:
            query: A plain-English search string.  Passed to
                ``SearchQuery`` with the default ``plain`` search type so
                that multi-word queries are implicitly ANDed.

        Returns:
            A queryset of ``Log`` instances annotated with a ``rank`` field.
        """
        search_query = SearchQuery(query, config="english")
        rank = SearchRank(models.F("search_index"), search_query)
        return (
            self.get_queryset()
            .filter(search_index=search_query)
            .annotate(rank=rank)
            .order_by("-rank", "-timestamp")
        )


class Log(models.Model):
    """A single IRC event (message, join, quit, etc.) received by the bot.

    Attributes:
        bot: The ``ChatBot`` instance that received this event.
        channel: The ``Channel`` this event belongs to.
        timestamp: UTC timestamp of when the event was received.
        nick: IRC nickname of the user who triggered the event.
        text: Message body (or relevant payload for non-PRIVMSG commands).
        action: ``True`` when the message was sent via ``/me``.
        command: Raw IRC command (``PRIVMSG``, ``JOIN``, ``QUIT``, …).
        host: Full IRC hostmask of the sender.
        raw: The unparsed IRC line as received from the server.
        room: Legacy field – channel name at insertion time.
        search_index: PostgreSQL ``tsvector`` updated automatically on save.
            Do **not** write to this field directly; use
            :meth:`update_search_field` instead.
    """

    objects = LogManager()

    bot = models.ForeignKey("bots.ChatBot", null=True, on_delete=models.CASCADE)
    channel = models.ForeignKey("bots.Channel", null=True, on_delete=models.CASCADE)
    timestamp = models.DateTimeField(db_index=True)
    nick = models.CharField(max_length=255)
    text = models.TextField()
    action = models.BooleanField(default=False)

    command = models.CharField(max_length=50, null=True, blank=True)
    host = models.TextField(null=True, blank=True)
    raw = models.TextField(null=True, blank=True)

    # freenode channel-name length limit is 50 chars; Campfire room IDs are
    # integers – 100 chars should be a safe upper bound for both.
    room = models.CharField(max_length=100, null=True, blank=True)

    search_index = SearchVectorField(null=True)

    class Meta:
        ordering = ("-timestamp",)
        indexes = [
            models.Index(fields=("channel", "timestamp")),
            # GIN index on search_index is declared in migration 0002 so it
            # appears in the migration history rather than being recreated on
            # every ``migrate`` run.
        ]

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_absolute_url(self) -> str:
        kwargs = channel_url_kwargs(self.channel)
        kwargs["msg_pk"] = self.pk
        return reverse("log_message_permalink", kwargs=kwargs)

    def as_html(self) -> str:
        return render_to_string("logs/log_display.html", {"message_list": [self]})

    def get_cleaned_host(self) -> str | None:
        """Return only the hostname part of the IRC hostmask (after the ``@``)."""
        if self.host:
            return self.host.split("@")[-1] if "@" in self.host else self.host
        return None

    def get_nick_color(self) -> int:
        """Stable per-nick colour index in the range [0, 31]."""
        return hash(self.nick) % 32

    # ------------------------------------------------------------------
    # Full-text search index population
    # ------------------------------------------------------------------

    def update_search_field(self) -> None:
        """Compute and persist the ``tsvector`` for this log line.

        Builds a combined ``SearchVector`` that gives higher weight to
        message text (weight ``A``) than to the author's nick (weight
        ``B``), so that body matches outrank nick matches when both exist.

        Uses a targeted ``queryset.update()`` rather than ``self.save()`` to
        avoid triggering a recursive save cycle and to minimise the columns
        written back to the database.

        This method is a no-op if the instance has not been saved yet (i.e.
        ``self.pk`` is ``None``).
        """
        if self.pk is None:
            return

        vector = SearchVector("text", weight="A", config="english") + SearchVector(
            "nick", weight="B", config="english"
        )
        Log.objects.filter(pk=self.pk).update(search_index=vector)

    # ------------------------------------------------------------------
    # ORM hooks
    # ------------------------------------------------------------------

    def save(self, *args, **kwargs) -> None:
        """Persist the log line and refresh its full-text search index.

        Redacted lines (where ``nick`` is in ``settings.EXCLUDE_NICKS``) have
        their text replaced with the placeholder and are *excluded* from the
        search index so they cannot be surfaced by a query.
        """
        is_redacted = self.nick in settings.EXCLUDE_NICKS
        if is_redacted:
            self.text = REDACTED_TEXT

        super().save(*args, **kwargs)

        # Populate the search index for every non-redacted message so that
        # new log lines are immediately searchable after insertion.
        if not is_redacted:
            self.update_search_field()

    # ------------------------------------------------------------------
    # String representations
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        if self.command == "PRIVMSG":
            prefix = f"{self.nick}: " if self.nick else ""
            return f"{prefix}{self.text[:20]}"
        try:
            return MSG_TMPL[self.command].format(nick=self.nick, text=self.text)
        except KeyError:
            return f"{self.command}: {self.text}"
