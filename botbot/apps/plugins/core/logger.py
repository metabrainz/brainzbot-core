import re

from botbot.apps.logs.models import Log
from botbot.apps.plugins.utils import convert_nano_timestamp
from botbot_plugins.base import BasePlugin
import botbot_plugins.config as config


class Config(config.BaseConfig):
    ignore_prefix = config.Field(
        default=["!-"],
        required=False,
        help_text="Don't log lines starting with any strings in this list"
    )


def should_ignore_text(text, ignore_prefix):
    return any(
        (re.match(prefix, text, flags=re.IGNORECASE) is not None)
        for prefix in ignore_prefix
    )


class Plugin(BasePlugin):
    """
    Logs all activity.

    I keep extensive logs on all the activity in `{{ channel.name }}`.
    You can read and search them at {{ SITE }}{{ channel.get_absolute_url }}.
    """
    config_class = Config

    def logit(self, line):
        """Log a message to the database"""
        # If the channel does not start with "#" that means the message
        # is part of a /query
        if line._channel_name.startswith("#"):
            ignore_prefix = self.config['ignore_prefix']

            if ignore_prefix:
                if not isinstance(ignore_prefix, list):
                    ignore_prefix = [ignore_prefix]
            else:
                ignore_prefix = []

            # Delete ACTION prefix created by /me
            text = line.text
            if text.startswith("ACTION "):
                text = text[7:]

            if not should_ignore_text(text, ignore_prefix):
                Log.objects.create(
                    channel_id=line._channel.pk,
                    timestamp=line._received,
                    nick=line.user,
                    text=line.full_text,
                    room=line._channel,
                    host=line._host,
                    command=line._command,
                    raw=line._raw)

    logit.route_rule = ('firehose', ur'(.*)')
