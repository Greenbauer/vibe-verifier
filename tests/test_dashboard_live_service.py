"""Independent live sources compose without losing partial coverage or private caches."""
import copy
import unittest
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from dashboard.config import Config
from dashboard.gh_api import ApiError
from dashboard.live_service import LiveService


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def snapshot():
    return {'github': {'errors': [], 'repositories': []}, 'telemetry': {
        'available': True, 'usage': {'available': True, 'accounts': [{'id': 'quota'}],
                                   'samples': [], 'sampled_at': NOW.isoformat(), 'stale': False,
                                   'completeness': 'Account-wide quota.'}}}


class LiveComposition(unittest.TestCase):
    def setUp(self):
        self.service = LiveService(Config('example', ('example/repo',), {}, None), wall_clock=lambda: NOW)
        self.service._usage_next = float('inf')

    def test_empty_history_preserves_partial_capture_notice_alongside_quota(self):
        self.service._usage_result = {'available': True, 'accounts': [], 'samples': [],
                                      'partial': True, 'completeness': 'Partial capture.'}
        with patch('dashboard.live_service.DashboardService.snapshot', return_value=snapshot()) as base:
            value = self.service.snapshot()
        base.assert_called_once_with(nonblocking=True)
        usage = value['telemetry']['usage']
        self.assertTrue(usage['partial'])
        self.assertEqual(usage['accounts'], [{'id': 'quota'}])
        self.assertIn('Partial capture.', usage['completeness'])
        self.assertEqual(usage['samples'], [])

    def test_revocation_does_not_serve_previously_captured_private_usage(self):
        self.service._usage_result = {'available': True, 'accounts': [{'id': 'private'}],
                                      'samples': [{'input_tokens': 123}], 'completeness': 'Captured.'}
        value = snapshot()
        value['github']['errors'] = [{'code': 'forbidden'}]
        with patch('dashboard.live_service.DashboardService.snapshot', return_value=value):
            result = self.service.snapshot()
        self.assertIsNone(self.service._usage_result)
        self.assertEqual(result['telemetry']['usage']['accounts'], [{'id': 'quota'}])
        self.assertEqual(result['telemetry']['usage']['samples'], [])

    def test_missing_resource_and_nested_permission_errors_clear_all_reader_caches(self):
        cases = [
            {'errors': [{'code': 'not_found'}]},
            {'errors': [], 'repositories': [{'errors': [{'code': 'forbidden'}]}]},
            {'errors': [], 'bots': {'errors': [{'code': 'authentication_failed'}]}},
        ]
        for github in cases:
            with self.subTest(github=github):
                self.service._usage_revoked = False
                old_reader = self.service.usage_reader
                old_reader.cache = {('example/private', 1): {'private': True}}
                old_reader.result = {'samples': [{'input_tokens': 123}]}
                self.service._usage_result = old_reader.result
                value = snapshot(); value['github'] = github
                with patch('dashboard.live_service.DashboardService.snapshot', return_value=value):
                    result = self.service.snapshot()
                self.assertEqual(result['telemetry']['usage']['samples'], [])
                self.assertIsNone(self.service._usage_result)
                self.assertIsNot(self.service.usage_reader, old_reader)
                self.assertEqual(self.service.usage_reader.cache, {})
                self.assertIsNone(self.service.usage_reader.result)

    def test_inflight_result_cannot_republish_after_revocation(self):
        started, release = threading.Event(), threading.Event()
        reader = self.service.usage_reader

        def collect():
            started.set()
            self.assertTrue(release.wait(2))
            reader.cache = {('example/private', 1): {'private': True}}
            reader.result = {'samples': [{'input_tokens': 123}]}
            return reader.result

        self.service._usage_running = True
        generation = self.service._usage_generation
        with patch.object(reader, 'collect', side_effect=collect):
            thread = threading.Thread(target=self.service._refresh_usage, args=(reader, generation))
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                value = snapshot(); value['github']['errors'] = [{'code': 'forbidden'}]
                with patch('dashboard.live_service.DashboardService.snapshot', return_value=value):
                    self.service.snapshot()
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.service._usage_running)
        self.assertIsNone(self.service._usage_result)
        self.assertEqual(self.service.usage_reader.cache, {})
        self.assertIsNone(self.service.usage_reader.result)
        self.assertEqual(self.service._usage_next, 0)

    def test_fresh_quota_does_not_hide_stale_history_age(self):
        observed = (NOW - timedelta(minutes=11)).isoformat()
        self.service._usage_running = True
        self.service._usage_result = {'available': True, 'sampled_at': observed, 'stale': False,
            'accounts': [], 'samples': [{'input_tokens': 123}], 'completeness': 'Captured.'}
        with patch('dashboard.live_service.DashboardService.snapshot', return_value=snapshot()):
            usage = self.service.snapshot()['telemetry']['usage']
        self.assertFalse(usage['stale'])
        self.assertEqual(usage['sampled_at'], NOW.isoformat())
        self.assertTrue(usage['history_stale'])
        self.assertEqual(usage['history_sampled_at'], observed)
        self.assertIn('Token history is stale', usage['completeness'])
        self.assertEqual(self.service._usage_result['sampled_at'], observed)

    def test_history_only_source_ages_without_a_refresh_and_boundary_stays_current(self):
        for minutes, stale in [(10, False), (11, True)]:
            with self.subTest(minutes=minutes):
                observed = (NOW - timedelta(minutes=minutes)).isoformat()
                self.service._usage_result = {'available': True, 'sampled_at': observed, 'stale': False,
                    'accounts': [], 'samples': [], 'completeness': 'Captured.'}
                value = snapshot(); value['telemetry'] = {'available': False}
                with patch('dashboard.live_service.DashboardService.snapshot', return_value=value):
                    usage = self.service.snapshot()['telemetry']['usage']
                self.assertEqual(usage['stale'], stale)
                self.assertEqual(usage['history_stale'], stale)
                self.assertEqual(usage['history_sampled_at'], observed)

    def test_usage_source_failure_finishes_refresh_and_removes_old_history(self):
        self.service._usage_result = {'samples': [123]}
        self.service._usage_running = True
        with patch.object(self.service.usage_reader, 'collect', side_effect=ApiError('forbidden')):
            self.service._refresh_usage()
        self.assertFalse(self.service._usage_running)
        self.assertIsNone(self.service._usage_result)


if __name__ == '__main__':
    unittest.main()
