import datetime
import math
import re

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.template.loader import render_to_string
from django.shortcuts import redirect, get_object_or_404
from django.utils.functional import cached_property
from django.utils.translation import gettext as _
from django.views.generic import ListView, TemplateView, View
from django.views.decorators.cache import patch_cache_control
import pytz

from botbot.apps.bots.utils import reverse_channel
from botbot.apps.bots.views import ChannelMixin
from . import forms
from botbot.apps.logs.models import Log


class Help(ChannelMixin, TemplateView):

    """
    Help page for a channel.
    """
    template_name = 'logs/help.html'


class PaginatorPageLinksMixin(object):

    def paginate_queryset(self, queryset, page_size):
        paginator, page, object_list, has_other_pages = super(
            PaginatorPageLinksMixin, self).paginate_queryset(
                queryset, page_size)

        self.next_page = self.get_next_page_link(page)
        self.prev_page = self.get_previous_page_link(page)
        self.current_page = self.get_current_page_link(page)

        return paginator, page, object_list, has_other_pages

    def get_next_page_link(self, page):
        url = self.request.path
        params = self.request.GET.copy()

        if not page.has_next():
            return ""
        else:
            params['page'] = page.next_page_number()

        return '{0}?{1}'.format(url, params.urlencode())

    def get_previous_page_link(self, page):
        url = self.request.path
        params = self.request.GET.copy()

        if not page.has_previous():
            return ""
        else:
            params['page'] = page.previous_page_number()

        return '{0}?{1}'.format(url, params.urlencode())

    def get_current_page_link(self, page):
        url = self.request.path
        params = self.request.GET.copy()
        params['page'] = page.number
        return '{0}?{1}'.format(url, params.urlencode())


class LogDateMixin(object):

    def _get_base_queryset(self):
        return self.channel.filtered_logs()

    def channel_date_url(self, date=None):
        if not date:
            date = self.date
        viewname = self.format == 'text' and 'log_day_text' or 'log_day'
        return reverse_channel(
            self.channel, viewname, kwargs=self._kwargs_with_date(date))

    def _kwargs_with_date(self, date):
        kwargs = {
            'year': date.year,
            'month': "%02d" % date.month,
            'day': "%02d" % date.day
        }
        return kwargs

    def _local_date_at_midnight(self, timestamp):
        # cast timestamp into local timezone
        localized = timestamp.astimezone(self.request_timezone)
        # create a new date object starting at midnight in that timezone
        return datetime.datetime(localized.year,
                                 localized.month,
                                 localized.day,
                                 tzinfo=localized.tzinfo)

    def _get_previous_date(self):
        """
        Find the previous day, that has content.
        """
        date = None
        try:
            ts = (self._get_base_queryset()
                      .filter(timestamp__lt=self.date)[0].timestamp)
            date = self._local_date_at_midnight(ts)
        except IndexError:
            pass
        return date

    def _get_next_date(self):
        """
        Find the next day, that has content.
        """
        date = None
        try:
            ts = (self._get_base_queryset()
                .filter(timestamp__gte=datetime.timedelta(days=1) + self.date)
                .order_by('timestamp')[0].timestamp)
            date = self._local_date_at_midnight(ts)
        except IndexError:
            pass
        return date

    def _date_query_set(self, date):
        qs = self._get_base_queryset()
        return qs.filter(timestamp__gte=date,
                         timestamp__lt=date + datetime.timedelta(days=1))

def _utc_now():
    return datetime.datetime.now(tz=pytz.timezone('UTC'))

def _find_pk(pk, queryset):
    """Find a PK in a queryset in memory"""
    found = None
    try:
        pk = int(pk)
        found = next(obj for obj in queryset if obj.pk == pk)
    except (ValueError, StopIteration):
        pass
    return found

def _timeline_context(timeline):
    """
    Context (template) vars needed for timeline display.
    """

    if not timeline:
        return {}

    today = _utc_now().date()
    last_monday = today - datetime.timedelta(days=today.weekday())
    last_week = last_monday - datetime.timedelta(days=7)

    # the last month in the timeline needs special treatment so it
    # doesn't get ordered ahead of the last/current weeks
    last_year_key = next(reversed(timeline))
    last_month = timeline[last_year_key].pop()
    if not timeline[last_year_key]:
        del timeline[last_year_key]
    if last_month >= last_week:
        last_month_adjusted = (last_week -
                               datetime.timedelta(days=1))
    elif last_month >= last_monday:
        last_month_adjusted = (last_monday -
                               datetime.timedelta(days=1))
    else:
        last_month_adjusted = last_month

    result = {
        'timeline': timeline,
        'this_week': last_monday,
        'last_week': last_week,
        'last_month': {'real': last_month,
                       'adjusted': last_month_adjusted},
    }
    return result

class LogViewer(ChannelMixin, object):
    context_object_name = "message_list"
    newest_first = False
    show_first_header = False   # Display date header above first line
    paginate_by = 150
    format = ''

    def __init__(self, *args, **kwargs):
        super(LogViewer, self).__init__(*args, **kwargs)
        self.next_page = ""
        self.prev_page = ""
        self.current_page = ""

    def dispatch(self, request, *args, **kwargs):
        self._setup_response_format()
        return super(LogViewer, self).dispatch(request, *args, **kwargs)

    def _setup_response_format(self):
        if self.format == 'text':
            self.include_timeline = False
            self.template_name = 'logs/logs.txt'
            self.content_type = 'text/plain; charset=utf-8'
        elif self.request.headers.get('x-requested-with') == 'XMLHttpRequest':
            self.format = 'ajax'
            self.include_timeline = False
            self.template_name = 'logs/log_display.html'
        # Default to HTML view
        else:
            self.format = 'html'
            self.include_timeline = True
            self.template_name = "logs/logs.html"

    def get_ordered_queryset(self, queryset):
        order = 'timestamp'
        if self.newest_first:
            order = '-timestamp'

        return queryset.order_by(order)

    def get_context_data(self, **kwargs):
        context = super(LogViewer, self).get_context_data(**kwargs)

        if self.include_timeline:
            context.update(
                _timeline_context(self.channel.get_months_active()))

        if self.format == 'html':
            context.update({
                'is_current': getattr(self, 'is_current', False),
                'search_form': forms.SearchForm(),
                'show_first_header': self.show_first_header,
                'newest_first': self.newest_first,
            })

        size = self.channel.current_size()
        context.update({
            'size': size,
            'big': (size is not None and size >= settings.BIG_CHANNEL) if hasattr(settings, 'BIG_CHANNEL') else False,
            'prev_page': self.prev_page,
            'next_page': self.next_page,
            'current_page': self.current_page,
        })

        return context

    def render_to_response(self, context, **response_kwargs):
        response = super(LogViewer, self).render_to_response(
            context, **response_kwargs)

        has_next_page = False
        if self.format == 'html':
            # Official SEO header
            links = []
            if self.next_page:
                links.append('{0}; rel="next"'.format(self.next_page))
                has_next_page = True

            if self.prev_page:
                links.append('{0}; rel="prev"'.format(self.prev_page))
            response['Link'] = ','.join(links)
        else:
            # No HTML, pass page info in easily parseable headers
            if self.next_page:
                response['X-NextPage'] = self.next_page
                has_next_page = True

            if self.prev_page:
                response['X-PrevPage'] = self.prev_page

        if has_next_page:
            patch_cache_control(
                response, public=True,
                max_age=settings.CACHE_MIDDLEWARE_SECONDS)
        return response

    def _pages_for_queryset(self, queryset):
        return int(math.ceil(queryset.count() / float(self.paginate_by)))


class DayLogViewer(PaginatorPageLinksMixin, LogDateMixin, LogViewer, ListView):
    show_first_header = False
    allow_empty = True

    def get(self, request, *args, **kwargs):
        # Use cursor-based pagination for AJAX infinite scroll
        after = request.GET.get('after')
        before = request.GET.get('before')
        if self.format == 'ajax' and (after or before):
            return self._cursor_response(after, before)

        self.date = self.set_view_date()
        self.object_list = self.get_queryset()

        # Redirect to nearby logs if this queryset is empty to avoid a 404
        if not self.get_allow_empty() and not self.object_list.exists():
            url = self._nearby_log_url()
            if url:
                return redirect(url)
            raise Http404(_("Empty list and '%(class_name)s.allow_empty' is False.")
                          % {'class_name': self.__class__.__name__})

        context = self.get_context_data()
        return self.render_to_response(context)

    def _nearby_log_url(self):
        """Find a date-based log URL that will not be empty"""
        # First check if there is anything in the past
        closet_qs = self.channel.filtered_logs().order_by(
            "-timestamp").filter(timestamp__lte=self.date)

        # If not go to the future
        if not closet_qs.exists():
            closet_qs = self.channel.filtered_logs().order_by(
                "timestamp").filter(
                timestamp__gte=self.date)

        # Return the URL where the first log line found will be
        try:
            return self.channel_date_url(closet_qs[0].timestamp)
        except IndexError:
            pass
        return None


    def get_context_data(self):
        context = super(DayLogViewer, self).get_context_data()
        try:
            context.update({
                'highlight': int(self.request.GET.get('msg')),
            })
        except (TypeError, ValueError):
            pass
        return context


    def _cursor_response(self, after, before):
        """Handle cursor-based pagination for AJAX infinite scroll.

        Uses timestamp cursors instead of date+page to ensure messages
        never repeat or get skipped when crossing day boundaries.
        """
        qs = self.channel.filtered_logs()
        limit = self.paginate_by

        if after:
            after_ts = datetime.datetime.fromisoformat(after)
            messages = list(
                qs.filter(timestamp__gt=after_ts)
                .order_by('timestamp')[:limit]
            )
        else:
            before_ts = datetime.datetime.fromisoformat(before)
            messages = list(
                qs.filter(timestamp__lt=before_ts)
                .order_by('-timestamp')[:limit]
            )
            messages.reverse()

        context = {
            'message_list': messages,
            'channel': self.channel,
            'show_first_header': True,
        }
        html = render_to_string(
            'logs/log_display.html', context, request=self.request)
        response = HttpResponse(html)

        if messages:
            first_ts = messages[0].timestamp.isoformat()
            last_ts = messages[-1].timestamp.isoformat()
            base_url = self.request.path

            # Build next page cursor
            has_next = qs.filter(
                timestamp__gt=messages[-1].timestamp).exists()
            if has_next:
                params = self.request.GET.copy()
                params['after'] = last_ts
                params.pop('before', None)
                params.pop('page', None)
                response['X-NextPage'] = '{0}?{1}'.format(
                    base_url, params.urlencode())

            # Build previous page cursor
            has_prev = qs.filter(
                timestamp__lt=messages[0].timestamp).exists()
            if has_prev:
                params = self.request.GET.copy()
                params['before'] = first_ts
                params.pop('after', None)
                params.pop('page', None)
                response['X-PrevPage'] = '{0}?{1}'.format(
                    base_url, params.urlencode())

        return response

    def get_queryset(self):
        qs = self.channel.filtered_logs()
        qs = self.get_ordered_queryset(qs)
        start = self.date
        end = start + datetime.timedelta(days=1)
        return qs.filter(timestamp__gte=start, timestamp__lt=end)

    def _date_paginator(self, date):
        qs = self._date_query_set(date)
        return self.get_paginator(qs, self.get_paginate_by(qs))

    def paginate_queryset(self, queryset, page_size):
        paginator, page, object_list, has_other_pages = super(
            DayLogViewer, self).paginate_queryset(queryset, page_size)

        if not self.next_page:
            self.is_current = True

        return paginator, page, object_list, has_other_pages

    def get_previous_page_link(self, page):
        """
        Generate a cursor-based link to the previous page.
        Uses timestamp of the first message on the current page.
        """
        object_list = list(page.object_list)
        if not object_list:
            return None

        first_msg = object_list[0]
        # Check if there are older messages in the channel
        has_prev = self.channel.filtered_logs().filter(
            timestamp__lt=first_msg.timestamp).exists()
        if not has_prev:
            return None

        url = self.request.path
        params = self.request.GET.copy()
        params['before'] = first_msg.timestamp.isoformat()
        params.pop('after', None)
        params.pop('page', None)
        return '{0}?{1}'.format(url, params.urlencode())

    def get_next_page_link(self, page):
        """
        Generate a cursor-based link to the next page.
        Uses timestamp of the last message on the current page.
        """
        object_list = list(page.object_list)
        if not object_list:
            return ""

        last_msg = object_list[-1]
        # Check if there are newer messages in the channel
        has_next = self.channel.filtered_logs().filter(
            timestamp__gt=last_msg.timestamp).exists()
        if not has_next:
            return ""

        url = self.request.path
        params = self.request.GET.copy()
        params['after'] = last_msg.timestamp.isoformat()
        params.pop('before', None)
        params.pop('page', None)
        return '{0}?{1}'.format(url, params.urlencode())

    def get_current_page_link(self, page):
        # copy, to maintain any params that came in to original request.
        params = self.request.GET.copy()
        date = _utc_now()
        url = self.channel_date_url(date)
        params['page'] = page.number
        return '{0}?{1}'.format(url, params.urlencode())

    @cached_property
    def request_timezone(self):
        """
        Read timezone in from GET param otherwise use UTC
        """
        try:
            tz = pytz.timezone(self.request.GET.get('tz', ''))
        except pytz.UnknownTimeZoneError:
            tz = pytz.timezone('UTC')
        return tz

    def set_view_date(self):
        """Determine start date for queryset"""
        if all([field in self.kwargs for field in ['year', 'month', 'day']]):
            # localize date so logs start at local time
            try:
                return datetime.datetime(year=int(self.kwargs['year']),
                                         month=int(self.kwargs['month']),
                                         day=int(self.kwargs['day']),
                                         tzinfo=self.request_timezone)
            except ValueError:
                raise Http404("Invalid date.")

        # Use the last page.
        self.kwargs['page'] = 'last'
        return datetime.datetime.now(tz=pytz.timezone("UTC")).replace(hour=0, minute=0, second=0, microsecond=0)


class SearchLogViewer(PaginatorPageLinksMixin, LogViewer, ListView):
    show_first_header = True
    newest_first = True
    allow_empty = True
    include_timeline = False

    def get(self, request, *args, **kwargs):
        self.form = forms.SearchForm(request.GET)
        return super(SearchLogViewer, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        """
        Add the search term to the context data.
        """
        context = super(SearchLogViewer, self).get_context_data(**kwargs)
        context.update({
            'q': self.search_term,
            'search_form': self.form,
        })
        return context

    def get_queryset(self):
        """
        Use search results rather than the standard queryset.
        """
        self.form = forms.SearchForm(self.request.GET)
        if self.form.is_valid():
            self.search_term = self.form.cleaned_data.get("q", "")
        else:
            self.search_term = ""
        self.search_term = self.search_term.replace('%', '%%')

        filter_args = self.channel.visible_commands_filter

        # If a user is mentioned, filter those users first
        matches = re.search(r'(\bnick:([\w\-]+)\b)', self.search_term)
        if matches:
            self.search_term = self.search_term.replace(matches.groups()[0], '')
            filter_args = filter_args & Q(nick__icontains=matches.groups()[1])

        return self.channel.log_set.filter(search_index=self.search_term).filter(filter_args)


class SingleLogViewer(DayLogViewer):
    """
    Find a single log line and redirect to a permalink to it.

    This inherits from DayLogViewer because it needs to use same queryset
    and pagination methods to ensure the page is found in the same place.
    """

    def get(self, request, *args, **kwargs):
        try:
            log = get_object_or_404(Log.objects, pk=self.kwargs['msg_pk'])
        except ValueError:
            raise Http404
        # set date to midnight so get_queryset starts pages correctly
        self.date = datetime.datetime.combine(
            log.timestamp.date(), datetime.time.min, tzinfo=pytz.UTC
        )
        self.object_list = self.get_queryset()
        # Find the page in the queryset the message is located on.
        page_url = self._permalink_to_log(log)
        return redirect(page_url, permanent=True)

    def _permalink_to_log(self, log):
        """Scan pages for a single log. Return to permalink to page"""
        cache_key = "line:{}:permalink".format(log.pk)
        url, params = cache.get(cache_key, [None, {}])
        if not url:
            paginator = self.get_paginator(
                self.object_list, self.get_paginate_by(self.object_list))
            for n in paginator.page_range:
                page = paginator.page(n)
                if log in page.object_list:
                    params = {"msg": log.pk, "page": n}
                    url = self.channel_date_url()
                    cache.set(cache_key, [url, params], None)
                    break  # Found the page.
            # page wasn't found
            if not url:
                raise Http404
        oparams = self.request.GET.copy()
        oparams.update(params)
        return '{0}?{1}'.format(url, oparams.urlencode())


class MissedLogViewer(PaginatorPageLinksMixin, LogViewer, ListView):
    include_timeline = False
    show_first_header = True
    newest_first = False

    def get_context_data(self, **kwargs):
        data = super(MissedLogViewer, self).get_context_data(**kwargs)
        data['use_absolute_url'] = True
        return data

    def get_queryset(self):
        queryset = self.get_ordered_queryset(self.channel.log_set.all())
        nick = self.kwargs['nick']
        try:
            # cover nicks in the form: nick OR nick_ OR nick|<something>
            last_exit = (queryset
                .filter(
                    Q(nick__iexact=nick) |
                    Q(nick__istartswith="{0}|".format(nick)) |
                    Q(nick__iexact="{0}_".format(nick)),
                    Q(command='QUIT') | Q(command='PART'))
                .order_by('-timestamp')[0])
        except IndexError:
            raise Http404("User hasn't left room")
        try:
            last_join = queryset.filter(
                Q(nick__iexact=nick) |
                Q(nick__istartswith="{0}|".format(nick)) |
                Q(nick__iexact="{0}_".format(nick)), Q(command='JOIN'),
                Q(timestamp__gt=last_exit.timestamp)).order_by('timestamp')[0]
            date_filter = {'timestamp__range': (last_exit.timestamp,
                                                last_join.timestamp)}
        except IndexError:
            date_filter = {'timestamp__gte': last_exit.timestamp}
        # Only fetch results from when the user logged out.
        self.fetch_after = (
            last_exit.timestamp - datetime.timedelta(milliseconds=1))
        return queryset.filter(**date_filter)
