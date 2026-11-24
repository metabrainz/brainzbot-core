 # -*- coding: utf-8 -*-
import datetime

from unittest import mock

from django.test import SimpleTestCase, TestCase
from datetime import datetime, timezone
from . import utils


class UtilsTestCase(TestCase):
    def test_nano_timestamp(self):
        timestamp = '2014-01-27T16:35:53.123456789Z'
        py_date = utils.convert_nano_timestamp(timestamp)
        self.assertEqual(py_date,
                         datetime.datetime(2014, 1, 27, 16, 35,
                                           53, 123456, tzinfo=datetime.UTC))

    def test_short_nano_timestamp(self):
        timestamp = '2014-01-27T16:35:53.1234Z'
        py_date = utils.convert_nano_timestamp(timestamp)
        self.assertEqual(py_date,
                         datetime.datetime(2014, 1, 27, 16, 35, 
                                           53, 123400, tzinfo=datetime.UTC))

class CloseDbConnectionsAfterTestCase(SimpleTestCase):
    @mock.patch('botbot.apps.plugins.utils.connections')
    def test_closes_connections_after_return(self, connections):
        func = utils.close_db_connections_after(lambda x: x + 1)
        self.assertEqual(func(1), 2)
        connections.close_all.assert_called_once_with()

    @mock.patch('botbot.apps.plugins.utils.connections')
    def test_closes_connections_on_error(self, connections):
        def boom():
            raise ValueError
        with self.assertRaises(ValueError):
            utils.close_db_connections_after(boom)()
        connections.close_all.assert_called_once_with()
