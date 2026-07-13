# -*- coding: utf-8 -*-
"""
Tests for ``botbot.apps.logs``.

Structure
---------
BaseTestCase           – Shared fixture helper used by most test classes.
SmokeTests             – HTTP-level smoke tests to catch view regressions.
SearchIndexUnitTests   – Unit tests for Log.update_search_field() and
                         Log.save() auto-indexing behaviour.
LogManagerSearchTests  – Integration tests for LogManager.search().
SearchViewTests        – End-to-end HTTP tests for SearchLogViewer,
                         including the ``nick:`` qualifier.
RedactTests            – Verify that redacted log lines are excluded from
                         the search index and that the redact command works.
TemplateTestCase       – Template rendering safety (XSS).
TemplateTagTestCase    – urlize_impl helper tests.
"""

from datetime import datetime, timedelta

from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from django.template.response import SimpleTemplateResponse
import pytz

from botbot.apps.bots import models as bot_models
from botbot.apps.logs import models as log_models
from botbot.apps.logs.models import Log, REDACTED_TEXT
from .management.commands import redact as redact_cmd
from botbot.apps.bots.models import ChatBot, Channel
from botbot.apps.bots.utils import reverse_channel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class BaseTestCase(TestCase):
    """Create a minimal but realistic fixture used across the test suite."""

    def setUp(self):
        self.chatbot = ChatBot.objects.create(
            server="testserver",
            nick="botbot",
            slug="botbot",
        )
        self.public_channel = Channel.objects.create(
            chatbot=self.chatbot,
            name="#Test",
            slug="test",
            status=bot_models.Channel.ACTIVE,
            is_public=True,
        )
        # Baseline log entry – used by smoke tests that don't need search.
        self.log = log_models.Log.objects.create(
            bot=self.chatbot,
            channel=self.public_channel,
            command="PRIVMSG",
            nick="baseuser",
            text="baseline message",
            timestamp=timezone.now(),
        )


# ---------------------------------------------------------------------------
# HTTP smoke tests
# ---------------------------------------------------------------------------

class SmokeTests(BaseTestCase):
    """Verify that the core log views return HTTP 200 under normal conditions."""

    def test_current_viewer(self):
        url = reverse_channel(self.public_channel, "log_current")
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_day_log_viewer(self):
        url = reverse_channel(
            self.public_channel,
            "log_day",
            kwargs=dict(
                year=self.log.timestamp.year,
                month="%02d" % self.log.timestamp.month,
                day="%02d" % self.log.timestamp.day,
            ),
        )
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_day_log_text_viewer(self):
        url = reverse_channel(
            self.public_channel,
            "log_day_text",
            kwargs=dict(
                year=self.log.timestamp.year,
                month="%02d" % self.log.timestamp.month,
                day="%02d" % self.log.timestamp.day,
            ),
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.get("Content-Type", ""))

    def test_missed_log(self):
        now = timezone.now()
        log_models.Log.objects.create(
            channel=self.public_channel,
            command="QUIT",
            nick="wanderer",
            timestamp=now,
        )
        log_models.Log.objects.create(
            channel=self.public_channel,
            nick="wanderer",
            command="PRIVMSG",
            text="I am back",
            timestamp=now + timedelta(seconds=1),
        )
        url = "%smissed/wanderer/" % reverse_channel(self.public_channel, "log_current")
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_search_view_returns_200(self):
        url = "%ssearch/?q=baseline" % reverse_channel(self.public_channel, "log_current")
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_search_view_empty_query_returns_200(self):
        url = "%ssearch/?q=" % reverse_channel(self.public_channel, "log_current")
        self.assertEqual(self.client.get(url).status_code, 200)


# ---------------------------------------------------------------------------
# Unit tests – search field population
# ---------------------------------------------------------------------------

class SearchIndexUnitTests(BaseTestCase):
    """Unit tests for ``Log.update_search_field`` and auto-indexing on save."""

    def test_update_search_field_populates_non_null_value(self):
        """After calling update_search_field the search_index must not be NULL."""
        self.log.update_search_field()
        self.log.refresh_from_db()
        self.assertIsNotNone(
            self.log.search_index,
            "search_index should be a non-NULL tsvector after update_search_field()",
        )

    def test_save_auto_populates_search_index(self):
        """A freshly saved Log row should have its search_index populated."""
        log = Log.objects.create(
            bot=self.chatbot,
            channel=self.public_channel,
            command="PRIVMSG",
            nick="autouser",
            text="automatic indexing check",
            timestamp=timezone.now(),
        )
        log.refresh_from_db()
        self.assertIsNotNone(
            log.search_index,
            "search_index should be auto-populated immediately after Log.save()",
        )

    def test_update_search_field_is_noop_for_unsaved_instance(self):
        """Calling update_search_field on an unsaved instance must not raise."""
        unsaved = Log(
            channel=self.public_channel,
            nick="ghost",
            text="never saved",
            timestamp=timezone.now(),
        )
        # Must not raise any exception.
        try:
            unsaved.update_search_field()
        except Exception as exc:  # pragma: no cover
            self.fail(f"update_search_field() raised on unsaved instance: {exc}")

    @override_settings(EXCLUDE_NICKS=["redacted_nick"])
    def test_redacted_nick_does_not_populate_search_index(self):
        """Logs for excluded nicks must NOT be indexed (privacy guarantee)."""
        log = Log.objects.create(
            bot=self.chatbot,
            channel=self.public_channel,
            command="PRIVMSG",
            nick="redacted_nick",
            text="sensitive content",
            timestamp=timezone.now(),
        )
        log.refresh_from_db()
        self.assertIsNone(
            log.search_index,
            "search_index must remain NULL for redacted (excluded) nicks",
        )

    @override_settings(EXCLUDE_NICKS=["redacted_nick"])
    def test_redacted_nick_text_is_replaced(self):
        """The text of a redacted log line must be the REDACTED_TEXT constant."""
        log = Log.objects.create(
            bot=self.chatbot,
            channel=self.public_channel,
            command="PRIVMSG",
            nick="redacted_nick",
            text="sensitive content",
            timestamp=timezone.now(),
        )
        self.assertEqual(log.text, REDACTED_TEXT)


# ---------------------------------------------------------------------------
# Integration tests – LogManager.search()
# ---------------------------------------------------------------------------

class LogManagerSearchTests(TestCase):
    """Integration tests for ``LogManager.search()``.

    These tests require a running PostgreSQL instance with the ``search_index``
    GIN index in place (i.e. migration 0002 applied).
    """

    def setUp(self):
        self.chatbot = ChatBot.objects.create(
            server="testserver", nick="botbot", slug="botbot"
        )
        self.channel = Channel.objects.create(
            chatbot=self.chatbot,
            name="#searchtest",
            slug="searchtest",
            status=bot_models.Channel.ACTIVE,
            is_public=True,
        )

    def _make_log(self, text, nick="TestUser", **kwargs):
        """Helper: create a Log row and immediately populate its search index."""
        log = Log.objects.create(
            bot=self.chatbot,
            channel=self.channel,
            command="PRIVMSG",
            nick=nick,
            text=text,
            timestamp=timezone.now(),
            **kwargs,
        )
        log.update_search_field()
        return log

    # -- Basic search --

    def test_exact_word_match_returns_correct_row(self):
        match = self._make_log("the quick brown fox")
        miss = self._make_log("the lazy dog slept")

        results = Log.objects.search("fox")
        self.assertIn(match, results)
        self.assertNotIn(miss, results)

    def test_common_word_returns_multiple_rows(self):
        a = self._make_log("Hello World")
        b = self._make_log("Hello Europe")

        results = Log.objects.search("Hello")
        self.assertIn(a, results)
        self.assertIn(b, results)

    def test_no_match_returns_empty_queryset(self):
        self._make_log("Django is great")
        results = Log.objects.search("nonexistentword12345")
        self.assertEqual(results.count(), 0)

    def test_search_is_case_insensitive(self):
        log = self._make_log("Django Release Notes")
        results = Log.objects.search("django")
        self.assertIn(log, results)

    # -- Stemming (PostgreSQL English config) --

    def test_stemmed_search_matches_variants(self):
        """'running' should match a document containing 'run' (stemming)."""
        log = self._make_log("The bot is now run daily")
        results = Log.objects.search("running")
        self.assertIn(log, results)

    # -- Relevance ordering --

    def test_results_ordered_by_relevance(self):
        """A message that mentions the query word more often should rank higher."""
        high = self._make_log("python python python is the best language")
        low = self._make_log("I occasionally write python")

        results = list(Log.objects.search("python"))
        self.assertLess(
            results.index(high),
            results.index(low),
            "Higher-relevance result should appear before lower-relevance result",
        )

    # -- nick weight (weight B < weight A for text) --

    def test_nick_is_searchable(self):
        """Messages authored by a nick should be findable by searching that nick."""
        log = self._make_log("some message content", nick="AliceIRC")
        results = Log.objects.search("AliceIRC")
        self.assertIn(log, results)

    # -- Cross-channel isolation --

    def test_search_can_be_scoped_to_channel(self):
        """Results can be filtered by channel after calling .search()."""
        other_channel = Channel.objects.create(
            chatbot=self.chatbot,
            name="#other",
            slug="other",
            status=bot_models.Channel.ACTIVE,
            is_public=True,
        )
        in_channel = self._make_log("target message")
        out_of_channel = Log.objects.create(
            bot=self.chatbot,
            channel=other_channel,
            command="PRIVMSG",
            nick="Bob",
            text="target message",
            timestamp=timezone.now(),
        )
        out_of_channel.update_search_field()

        results = Log.objects.search("target").filter(channel=self.channel)
        self.assertIn(in_channel, results)
        self.assertNotIn(out_of_channel, results)

    # -- Redacted rows --

    @override_settings(EXCLUDE_NICKS=["secret_nick"])
    def test_redacted_rows_are_not_returned_by_search(self):
        """Rows for excluded nicks must never appear in search results."""
        Log.objects.create(
            bot=self.chatbot,
            channel=self.channel,
            command="PRIVMSG",
            nick="secret_nick",
            text="confidential information",
            timestamp=timezone.now(),
        )
        results = Log.objects.search("confidential")
        for log in results:
            self.assertNotEqual(
                log.nick,
                "secret_nick",
                "Redacted nick must not appear in search results",
            )


# ---------------------------------------------------------------------------
# HTTP end-to-end tests – SearchLogViewer
# ---------------------------------------------------------------------------

class SearchViewTests(TestCase):
    """End-to-end tests for the ``/search/`` view, including the nick: qualifier."""

    def setUp(self):
        self.chatbot = ChatBot.objects.create(
            server="testserver", nick="botbot", slug="botbot"
        )
        self.channel = Channel.objects.create(
            chatbot=self.chatbot,
            name="#viewtest",
            slug="viewtest",
            status=bot_models.Channel.ACTIVE,
            is_public=True,
        )

    def _search_url(self, query):
        base = reverse(
            "log_search",
            kwargs={
                "bot_slug": self.chatbot.slug,
                "channel_slug": self.channel.slug,
            },
        )
        return f"{base}?q={query}"

    def _make_log(self, text, nick="User"):
        log = Log.objects.create(
            bot=self.chatbot,
            channel=self.channel,
            command="PRIVMSG",
            nick=nick,
            text=text,
            timestamp=timezone.now(),
        )
        log.update_search_field()
        return log

    def test_search_returns_matching_results(self):
        match = self._make_log("python is awesome")
        miss = self._make_log("java is also fine")

        response = self.client.get(self._search_url("python"))
        self.assertEqual(response.status_code, 200)

        object_list = response.context_data["object_list"]
        self.assertIn(match, object_list)
        self.assertNotIn(miss, object_list)

    def test_nick_qualifier_filters_by_author(self):
        """``nick:Foo`` qualifier must restrict results to that author only."""
        a = self._make_log("Hello World", nick="Foo")
        b = self._make_log("Hello World", nick="Bar")

        response = self.client.get(self._search_url("nick:Foo+Hello"))
        self.assertEqual(response.status_code, 200)

        object_list = response.context_data["object_list"]
        self.assertIn(a, object_list)
        self.assertNotIn(b, object_list)

    def test_nick_qualifier_at_end_of_query(self):
        """The nick: qualifier should work regardless of its position in the query."""
        a = self._make_log("Hello World", nick="Foo")
        b = self._make_log("Hello World", nick="Bar")

        response = self.client.get(self._search_url("World+nick:Foo"))
        object_list = response.context_data["object_list"]
        self.assertIn(a, object_list)
        self.assertNotIn(b, object_list)

    def test_nick_qualifier_combined_with_body_term(self):
        """nick: qualifier combined with body terms must AND both conditions."""
        a = self._make_log("Hello World", nick="Foo")
        b = self._make_log("Hello World", nick="Bar")

        response = self.client.get(self._search_url("World+nick:Foo+Hello"))
        object_list = response.context_data["object_list"]
        self.assertIn(a, object_list)
        self.assertNotIn(b, object_list)

    def test_empty_search_returns_empty_list(self):
        """An empty query string must return an empty result set (not an error)."""
        response = self.client.get(self._search_url(""))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context_data["object_list"]), 0)


# ---------------------------------------------------------------------------
# Redact command tests
# ---------------------------------------------------------------------------

class RedactTests(TestCase):
    """Verify the redact management command and the EXCLUDE_NICKS auto-redaction."""

    def setUp(self):
        self.chatbot = ChatBot.objects.create(server="testserver", nick="botbot")
        self.channel = Channel.objects.create(
            chatbot=self.chatbot,
            slug="test",
            name="#Test",
            is_public=True,
        )

    def test_redact_command_replaces_text(self):
        Log.objects.create(
            channel=self.channel,
            command="PRIVMSG",
            text="this is a test",
            nick="not-redact",
            timestamp=timezone.now(),
        )
        Log.objects.create(
            channel=self.channel,
            command="PRIVMSG",
            text="this is a test",
            nick="redact",
            timestamp=timezone.now(),
        )
        redacted_qs = Log.objects.filter(text=REDACTED_TEXT)
        self.assertFalse(redacted_qs.exists())

        count = redact_cmd._redact_logs_for_nick("redact")
        self.assertEqual(count, 1)
        self.assertEqual(redacted_qs.count(), 1)
        self.assertEqual(redacted_qs.first().text, REDACTED_TEXT)

    @override_settings(EXCLUDE_NICKS=["redact"])
    def test_exclude_nicks_setting_triggers_auto_redaction_on_save(self):
        normal = Log.objects.create(
            channel=self.channel,
            command="PRIVMSG",
            text="this is a test",
            nick="not-redact",
            timestamp=timezone.now(),
        )
        self.assertNotEqual(normal.text, REDACTED_TEXT)

        redacted = Log.objects.create(
            channel=self.channel,
            command="PRIVMSG",
            text="this is a test",
            nick="redact",
            timestamp=timezone.now(),
        )
        self.assertEqual(redacted.text, REDACTED_TEXT)


# ---------------------------------------------------------------------------
# Template safety tests
# ---------------------------------------------------------------------------

class TemplateTestCase(TestCase):
    """Ensure user-supplied content is properly escaped in templates."""

    def setUp(self):
        self.chatbot = ChatBot.objects.create(
            server="testserver", nick="botbot", slug="botbot"
        )
        self.channel = Channel.objects.create(
            chatbot=self.chatbot,
            name="#Test",
            slug="test",
            is_public=True,
        )

    def test_xss_characters_are_escaped_in_log_display(self):
        log = Log.objects.create(
            bot=self.chatbot,
            channel=self.channel,
            timestamp=timezone.now(),
            nick="nick",
            text='<script>alert("hi")</script>',
            command="PRIVMSG",
        )
        response = SimpleTemplateResponse(
            "logs/log_display.html", {"message_list": [log]}
        ).render()
        self.assertIn(b"&lt;script&gt;alert", response.content)


# ---------------------------------------------------------------------------
# Template-tag tests
# ---------------------------------------------------------------------------

class TemplateTagTestCase(TestCase):
    """Tests for the ``urlize_impl`` template-tag helper."""

    def test_plain_url_is_wrapped_in_anchor(self):
        from botbot.apps.logs.templatetags.logs_tags import urlize_impl

        self.assertEqual(
            urlize_impl("http://www.example.com"),
            '<a href="http://www.example.com">http://www.example.com</a>',
        )

    def test_control_character_stripped_from_url(self):
        from botbot.apps.logs.templatetags.logs_tags import urlize_impl

        # The GitHub IRC bot appends a shift-up (\017) control character.
        self.assertEqual(
            urlize_impl("http://www.example.com\017"),
            '<a href="http://www.example.com">http://www.example.com</a>',
        )

    def test_unicode_url_is_percent_encoded(self):
        from botbot.apps.logs.templatetags.logs_tags import urlize_impl

        self.assertEqual(
            urlize_impl(
                "https://forge.puppetlabs.com/modules?utf-8=✓&sort=latest_release&supported=yes"
            ),
            (
                '<a href="https://forge.puppetlabs.com/modules?utf-8=%E2%9C%93'
                "&sort=latest_release&supported=yes\">"
                "https://forge.puppetlabs.com/modules?utf-8=✓&sort=latest_release&supported=yes</a>"
            ),
        )

    def test_image_url_gets_data_src_attribute(self):
        from botbot.apps.logs.templatetags.logs_tags import urlize_impl

        result = urlize_impl("http://www.example.com/image.png")
        self.assertIn('data-type="image"', result)
        self.assertIn('data-src="http://www.example.com/image.png"', result)

    def test_youtube_url_gets_embed_data_src(self):
        from botbot.apps.logs.templatetags.logs_tags import urlize_impl

        result = urlize_impl("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertIn("data-type=\"youtube\"", result)
        self.assertIn("//www.youtube.com/embed/dQw4w9WgXcQ", result)

    def test_invalid_youtube_url_is_plain_anchor(self):
        from botbot.apps.logs.templatetags.logs_tags import urlize_impl

        result = urlize_impl("https://www.youtube.com/watch")
        self.assertNotIn("data-type", result)

    def test_dropbox_url_gets_direct_download_src(self):
        from botbot.apps.logs.templatetags.logs_tags import urlize_impl

        url = "https://www.dropbox.com/s/1vbeuicgr18ialb/Screenshot%202014-06-09%2015.02.39.png"
        result = urlize_impl(url)
        self.assertIn("dl.dropboxusercontent.com", result)
        self.assertIn('data-type="image"', result)

    def test_cloudapp_image_url_gets_content_src(self):
        from botbot.apps.logs.templatetags.logs_tags import urlize_impl

        result = urlize_impl("http://cl.ly/image/1Y0A1C3l370z")
        self.assertIn("/content", result)
        self.assertIn('data-type="image"', result)

    def test_cloudapp_url_without_image_segment(self):
        from botbot.apps.logs.templatetags.logs_tags import urlize_impl

        result = urlize_impl("http://cl.ly/1Y0A1C3l370z")
        self.assertIn("/content", result)
        self.assertIn('data-type="image"', result)
