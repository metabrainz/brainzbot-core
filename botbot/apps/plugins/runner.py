# pylint: disable=W0212
import json
import logging

import re
from importlib import import_module

import redis
import botbot_plugins.plugins
from django.core.cache import cache
from django.conf import settings
from django.db import close_old_connections

from botbot.apps.bots import models as bots_models
from botbot.apps.plugins.utils import convert_nano_timestamp, log_on_error
from .plugin import RealPluginMixin


CACHE_TIMEOUT_2H = 7200
LOG = logging.getLogger('botbot.plugin_runner')


class Router(object):
    """
    Custom Router object
    """
    def __init__(self, name):
        self.name = name
        self.plugins = {}

class Line(object):
    """
    All the methods and data necessary for a plugin to act on a line
    """
    def __init__(self, packet, app):
        self.full_text = packet['Content']
        self.text = packet['Content']
        self.user = packet['User']

        # Private attributes not accessible to external plugins
        self._chatbot_id = packet['ChatBotId']
        self._raw = packet['Raw']
        self._channel_name = packet['Channel'].strip()
        self._command = packet['Command']
        self._is_message = packet['Command'] == 'PRIVMSG'
        self._host = packet['Host']

        self._received = convert_nano_timestamp(packet['Received'])

        self.is_direct_message = self.check_direct_message()

    def is_valid(self):
        if self._chatbot and self._channel:
            return True
        return False

    @property
    def _chatbot(self):
        """Simple caching for ChatBot model"""
        if not hasattr(self, '_chatbot_cache'):
            cache_key = 'chatbot:{0}'.format(self._chatbot_id)
            chatbot = cache.get(cache_key)
            if not chatbot:
                try:
                    chatbot = bots_models.ChatBot.objects.get(
                        id=self._chatbot_id)
                except bots_models.ChatBot.DoesNotExist:
                    LOG.warn('Chatbot %s does not exist. Line dropped.',
                             self._chatbot_id)
                    return None
                cache.set(cache_key, chatbot, CACHE_TIMEOUT_2H)
            self._chatbot_cache = chatbot
        return self._chatbot_cache

    @property
    def _channel(self):
        """Simple caching for Channel model"""
        if not hasattr(self, '_channel_cache'):
            cache_key = 'channel:{0}-{1}'.format(self._chatbot_id, self._channel_name)
            channel = cache.get(cache_key)

            if not channel and self._channel_name.startswith("#"):
                try:
                    channel = self._chatbot.channel_set.get(
                        name=self._channel_name)
                except self._chatbot.channel_set.model.DoesNotExist:
                    LOG.warn('Chatbot %s should not be listening to %s. '
                             'Line dropped.',
                             self._chatbot_id, self._channel_name)
                    return None
                cache.set(cache_key, channel, CACHE_TIMEOUT_2H)

                """
                The following logging is to help out in sentry. For some
                channels, we are getting occasional issues with the
                ``channel_set.get()`` lookup above
                """
                LOG.debug(channel)
                LOG.debug(self._channel_name)
                LOG.debug(cache_key)
                LOG.debug("%s", ", ".join(self._chatbot.channel_set.values_list('name', flat=True)))

            self._channel_cache = channel
        return self._channel_cache

    @property
    def _active_plugin_slugs(self):
        if not hasattr(self, '_active_plugin_slugs_cache'):
            if self._channel:
                self._active_plugin_slugs_cache = self._channel.active_plugin_slugs
            else:
                self._active_plugin_slugs_cache = set()
        return self._active_plugin_slugs_cache

    def check_direct_message(self):
        """
        If message is addressed to the bot, strip the bot's nick
        and return the rest of the message. Otherwise, return False.
        """

        nick = self._chatbot.nick

        # Private message
        if self._channel_name == nick:
            LOG.debug('Private message detected')
            # Set channel as user, so plugins reply by PM to correct user
            self._channel_name = self.user

            return True

        if len(nick) == 1:
            # support @<plugin> or !<plugin>
            regex = r'^{0}(.*)'.format(re.escape(nick))
        else:
            # support <nick>: <plugin>
            regex = r'^{0}[:\s](.*)'.format(re.escape(nick))
        match = re.match(regex, self.full_text, re.IGNORECASE)
        if match:
            LOG.debug('Direct message detected')
            self.text = match.groups()[0].lstrip()
            return True
        return False

    def __str__(self):
        return self.full_text

    def __repr__(self):
        return str(self)


class PluginRunner(object):
    """
    Registration and routing for plugins
    Calls to plugins are done via greenlets
    """

    def __init__(self, use_gevent=False):
        if use_gevent:
            import gevent
            self.gevent = gevent
        self.bot_bus = redis.StrictRedis.from_url(
            settings.REDIS_PLUGIN_QUEUE_URL)
        self.storage = redis.StrictRedis.from_url(
            settings.REDIS_PLUGIN_STORAGE_URL)

        self.command_prefix = settings.COMMAND_PREFIX

        self.routers = {
            # plugins that listen to everything coming over the wire
            "firehose": Router("firehose"),
            # plugins that listen to all messages (aka PRIVMSG)
            "messages": Router("messages"),
            # plugins that listen on direct messages (starting with bot nick)
            "mentions": Router("mentions"),
            # plugins that only listen to certain commands
            "commands": Router("commands"),
            # plugins that only listen to certain commands with args parsed by regex
            "regex_commands": Router("regex_commands")
        }

    def register_all_plugins(self):
        """Iterate over all plugins and register them with the app"""
        for core_plugin in ['help', 'logger']:
            mod = import_module('botbot.apps.plugins.core.{}'.format(core_plugin))
            plugin = mod.Plugin()
            self.register(plugin)
        for mod in botbot_plugins.plugins.__all__:
            plugin = import_module('botbot_plugins.plugins.' + mod).Plugin()
            self.register(plugin)

    def register(self, plugin):
        """
        Introspects the Plugin class instance provided for methods
        that need to be registered with the internal app routers.
        """
        for key in dir(plugin):
            try:
                # the config attr bombs if accessed here because it tries
                # to access an attribute from the dummyapp
                attr = getattr(plugin, key)
            except AttributeError:
                continue

            if not key.startswith('__') and getattr(attr, 'route_rule', None):
                router_name = attr.route_rule[0]
                rule = attr.route_rule[1]

                LOG.info('Route: %s.%s listens to %s for rule %s',
                         plugin.slug, key, router_name, rule)

                self.routers[router_name].plugins.setdefault(
                    plugin.slug, []).append((rule, attr, plugin))

    def listen(self):
        """Listens for incoming messages on the Redis queue"""
        while 1:
            val = None
            try:
                # Close stale DB connections before processing each line
                close_old_connections()

                val = self.bot_bus.blpop('q', 1)

                # Track q length
                ql = self.bot_bus.llen('q')

                if val:
                    _, val = val
                    LOG.debug('Received: %s', val)
                    line = Line(json.loads(val), self)

                    if line.is_valid():
                        self.dispatch(line)
            except Exception:
                LOG.error("Line Dispatch Failed", exc_info=True, extra={
                    "line": val
                })
                # Ensure stale DB connections are closed so the next
                # iteration can re-establish a fresh connection.
                try:
                    close_old_connections()
                except Exception:
                    LOG.error("Failed to close DB connections",
                              exc_info=True)

    def dispatch(self, line):
        """Given a line, dispatch it to the right plugins & functions."""
        # This is a pared down version of the `check_for_plugin_route_matches`
        # method for firehose plugins (no regexing or return values)
        active_firehose_plugins = line._active_plugin_slugs.intersection(
            self.routers["firehose"].plugins.keys())
        for plugin_slug in active_firehose_plugins:
            for _, func, plugin in self.routers["firehose"].plugins[plugin_slug]:
                # firehose gets everything, no rule matching
                LOG.info('Match: %s.%s', plugin_slug, func.__name__)
                channel_plugin = self.setup_plugin_for_channel(
                    plugin.__class__, line)
                new_func = log_on_error(LOG, getattr(channel_plugin,
                                                     func.__name__))
                if hasattr(self, 'gevent'):
                    self.gevent.Greenlet.spawn(new_func, line)
                else:
                    channel_plugin.respond(new_func(line))

        # pass line to other routers
        if line._is_message:
            self.check_for_plugin_route_matches(line, self.routers["messages"])

            if line.is_direct_message:
                self.check_for_plugin_route_matches(line, self.routers["mentions"])

            if line.text.startswith(self.command_prefix):
                self.check_for_plugin_route_matches(line, self.routers["commands"])
                self.check_for_plugin_route_matches(line, self.routers["regex_commands"])

    def setup_plugin_for_channel(self, fake_plugin_class, line):
        """Given a dummy plugin class, initialize it for the line's channel"""
        class RealPlugin(RealPluginMixin, fake_plugin_class):
            pass
        plugin = RealPlugin(slug=fake_plugin_class.__module__.split('.')[-1],
                            channel=line._channel,
                            chatbot_id=line._chatbot_id,
                            app=self)
        plugin.initialize()
        return plugin

    def run_plugin(self, line, plugin, plugin_slug, func, arg_dict):
        # Instantiate a plugin specific to this channel
        channel_plugin = self.setup_plugin_for_channel(plugin.__class__, line)
        # get the method from the channel-specific plugin
        new_func = log_on_error(LOG, getattr(channel_plugin, func.__name__))

        if hasattr(self, 'gevent'):
            grnlt = self.gevent.Greenlet(new_func, line, **arg_dict)
            grnlt.link_value(channel_plugin.greenlet_respond)
            grnlt.start()
        else:
            channel_plugin.respond(new_func(line, **arg_dict))


    def check_for_plugin_route_matches(self, line, router):
        """Checks the active plugins' routes and calls functions on matches"""
        # get the active routes for this channel
        active_slugs = line._active_plugin_slugs.intersection(router.plugins.keys())
        for plugin_slug in active_slugs:
            for rule, func, plugin in router.plugins[plugin_slug]:
                if router.name == "commands":
                    cmd = rule
                    args = line.text.split()
                    if args[0] == self.command_prefix + cmd:
                        LOG.info('Command: %s.%s', plugin_slug, func.__name__)
                        # We need a dict as run_plugin will unpack it
                        arg_dict = {"args": args[1:]}
                        self.run_plugin(
                          line, plugin, plugin_slug, func, arg_dict
                        )

                elif router.name == "regex_commands":
                    cmd = rule[0]
                    regex = rule[1]
                    args = line.text.split()
                    if args[0] == self.command_prefix + cmd:
                        linetext = " ".join(args[1:])
                        match = re.match(regex, linetext, re.IGNORECASE)
                        if match:
                            LOG.info('Command+Match: %s.%s', plugin_slug, func.__name__)
                            self.run_plugin(
                              line, plugin, plugin_slug, func, match.groupdict()
                            )

                else:
                    regex = rule
                    match = re.match(regex, line.text, re.IGNORECASE)
                    if match:
                        LOG.info('Match: %s.%s', plugin_slug, func.__name__)
                        self.run_plugin(
                            line, plugin, plugin_slug, func, match.groupdict()
                        )


def start_plugins(*args, **kwargs):
    """
    Used by the management command to start-up plugin listener
    and register the plugins.
    """
    LOG.info('Starting plugins. Gevent=%s', kwargs['use_gevent'])
    app = PluginRunner(**kwargs)
    app.register_all_plugins()
    app.listen()
