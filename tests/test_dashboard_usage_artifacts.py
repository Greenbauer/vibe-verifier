"""Usage counts must have current-owner GitHub provenance and never include content."""
import copy
import io
import json
import unittest
import zipfile
from datetime import datetime, timedelta, timezone

from dashboard.config import Config, BotDefinition
from dashboard.gh_api import ApiError
from dashboard.usage_artifacts import UsageArtifacts, decode_archive, normalize

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
REPO = 'example/site'


def fixture(provider='openai'):
    value = {'schema_version': 1, 'status': 'complete', 'status_reason': None,
             'owner': 'example', 'repository': REPO, 'run_id': 42, 'run_attempt': 1,
             'head_sha': 'a' * 40, 'role': 'qae-explorer', 'provider': provider,
             'account_alias': None, 'observed_at': '2026-09-30T11:00:00Z', 'job_key': 'explore',
             'usage': {'input_tokens': 100, 'cached_input_tokens': 60,
                       'cache_creation_input_tokens': None, 'output_tokens': 10, 'reasoning_output_tokens': None}}
    artifact = {'name': 'vv-usage-qae-explorer-1', 'workflow_run': {'id': 42, 'head_sha': 'a' * 40}}
    run = {'id': 42, 'run_attempt': 1, 'head_sha': 'a' * 40, 'repository': {'full_name': REPO}}
    return value, artifact, run


class ArtifactValidation(unittest.TestCase):
    def normalize(self, value=None, artifact=None, run=None):
        original = fixture()
        return normalize(value or original[0], artifact or original[1], run or original[2], REPO, NOW)

    def test_source_numeric_only_and_codex_cache_not_double_counted(self):
        value, artifact, run = fixture()
        value['arbitrary_model_output'] = 'private-canary-do-not-copy'
        record = self.normalize(value, artifact, run)
        self.assertEqual(record['sample']['input_tokens'], 100)
        self.assertEqual(record['sample']['output_tokens'], 10)
        self.assertNotIn('private-canary', json.dumps(record))

    def test_claude_cache_fields_are_distinct(self):
        value, artifact, run = fixture('anthropic')
        value['usage']['cache_creation_input_tokens'] = 20
        self.assertEqual(self.normalize(value, artifact, run)['sample']['input_tokens'], 180)

    def test_missing_usage_is_not_zero_but_true_zero_is_measured(self):
        value, artifact, run = fixture()
        value['status'], value['usage'] = 'unavailable', None
        self.assertIsNone(self.normalize(value, artifact, run))
        value, artifact, run = fixture()
        value['usage'] = {key: 0 for key in value['usage']}
        self.assertEqual(self.normalize(value, artifact, run)['sample']['input_tokens'], 0)

    def test_identity_scope_and_attempt_must_match_actual_run(self):
        for field, bad in [('owner', 'other'), ('repository', 'other/site'), ('run_id', 99),
                           ('run_attempt', 2), ('head_sha', 'b' * 40), ('role', 'swe-reviewer')]:
            with self.subTest(field=field):
                value, artifact, run = fixture()
                value[field] = bad
                with self.assertRaises(ValueError):
                    self.normalize(value, artifact, run)
        value, artifact, run = fixture()
        run['repository']['full_name'] = 'other/site'
        with self.assertRaises(ValueError):
            self.normalize(value, artifact, run)

    def test_invalid_numbers_dates_and_aliases(self):
        for bad in [True, -1, 1.1, '5', float('inf'), float('nan')]:
            value, artifact, run = fixture()
            value['usage']['input_tokens'] = bad
            with self.assertRaises(ValueError):
                self.normalize(value, artifact, run)
        for bad in ['2026-09-01T12:00:00Z', '2026-10-01T12:00:00Z', None]:
            value, artifact, run = fixture()
            value['observed_at'] = bad
            with self.assertRaises(ValueError):
                self.normalize(value, artifact, run)
        value, artifact, run = fixture()
        value['account_alias'] = 'person@example.test'
        with self.assertRaises(ValueError):
            self.normalize(value, artifact, run)

    def test_unmapped_accounts_remain_distinct(self):
        value, artifact, run = fixture()
        first = self.normalize(value, artifact, run)
        value['run_id'] = artifact['workflow_run']['id'] = run['id'] = 43
        second = self.normalize(value, artifact, run)
        self.assertNotEqual(first['account']['id'], second['account']['id'])
        value['account_alias'] = 'runner-subscription'
        mapped = self.normalize(value, artifact, run)
        self.assertEqual(mapped['account']['id'], 'openai:runner-subscription')

    def test_zip_is_not_extracted_and_accepts_only_small_usage_record(self):
        for name, data, accepted in [('usage.json', json.dumps(fixture()[0]), True),
                                     ('../usage.json', '{}', False), ('usage.json', ' ' * 17000, False)]:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr(name, data)
            if accepted:
                self.assertEqual(decode_archive(stream.getvalue())['run_id'], 42)
            else:
                with self.assertRaises(ValueError):
                    decode_archive(stream.getvalue())


class ArtifactCollection(unittest.TestCase):
    def setUp(self):
        self.record, self.artifact, self.run = fixture()
        self.artifact.update(id=9, size_in_bytes=500, expired=False, created_at='2026-09-30T11:00:00Z')
        self.run['path'] = '.github/workflows/explore.yml'
        self.listing = {'total_count': 1, 'artifacts': [self.artifact]}
        self.elapsed, self.downloads, self.revoked, self.exhausted = 0, 0, False, False
        self.runner_name = None
        self.more, self.pages_read = {}, []
        config = Config('example', (REPO,), {'explorer': BotDefinition('explore.yml', ('explore',))}, None)
        self.reader = UsageArtifacts(config, api=self, downloader=self.download, clock=lambda: self.elapsed)

    def begin(self):
        pass

    def one(self, endpoint):
        if self.revoked:
            raise ApiError('not_found')
        if self.exhausted:
            raise ApiError('request_budget_exhausted')
        if '/actions/artifacts?per_page=100&page=' in endpoint:
            number = int(endpoint.rsplit('=', 1)[1])
            self.pages_read.append(number)
            return self.listing if number == 1 else self.more.get(number, {'artifacts': []})
        if endpoint.endswith('/jobs?per_page=100'):
            job = {'name': 'explore', 'runner_name': self.runner_name} if self.runner_name else None
            return {'jobs': [job] if job else []}
        self.assertEqual(endpoint, 'repos/example/site/actions/runs/42/attempts/1')
        return self.run

    def download(self, repository, artifact_id):
        self.assertEqual((repository, artifact_id), (REPO, 9))
        self.downloads += 1
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('usage.json', json.dumps(self.record))
        return stream.getvalue()

    def test_real_archive_is_imported_once_then_removed_when_source_disappears(self):
        result = self.reader.collect(NOW)
        self.assertEqual(result['samples'][0]['input_tokens'], 100)
        self.assertFalse(result['partial'])
        self.elapsed = 301
        self.reader.collect(NOW)
        self.assertEqual(self.downloads, 1)
        self.elapsed = 602
        self.listing = {'total_count': 0, 'artifacts': []}
        self.assertEqual(self.reader.collect(NOW)['samples'], [])
        self.assertEqual(self.reader.cache, {})

    def test_wrong_workflow_never_downloads_or_imports_usage(self):
        self.run['path'] = '.github/workflows/unrelated.yml'
        result = self.reader.collect(NOW)
        self.assertTrue(result['partial'])
        self.assertEqual(result['samples'], [])
        self.assertEqual(self.downloads, 0)

    def test_revocation_removes_private_cache_and_reports_partial_without_fake_zero(self):
        self.reader.collect(NOW)
        self.elapsed, self.revoked = 301, True
        result = self.reader.collect(NOW)
        self.assertTrue(result['partial'])
        self.assertEqual(result['samples'], [])
        self.assertEqual(result['accounts'], [])
        self.assertEqual(self.reader.cache, {})

    def filler(self, created, count=100):
        return [{'id': 1000 + index, 'name': 'build-output', 'expired': False, 'size_in_bytes': 10,
                 'created_at': created} for index in range(count)]

    def test_a_week_that_spills_onto_a_second_page_is_read_whole(self):
        self.listing = {'total_count': 101, 'artifacts': self.filler('2026-09-30T11:30:00Z')}
        self.more = {2: {'total_count': 101, 'artifacts': [self.artifact]}}
        result = self.reader.collect(NOW)
        self.assertEqual(self.pages_read, [1, 2])
        self.assertEqual(result['samples'][0]['input_tokens'], 100)
        self.assertFalse(result['partial'])

    def test_a_page_that_reaches_past_the_week_ends_the_listing(self):
        rows = self.filler('2026-09-30T11:30:00Z', 99) + [{**self.filler('2026-09-20T00:00:00Z', 1)[0], 'id': 5}]
        self.listing = {'total_count': 300, 'artifacts': [self.artifact] + rows[1:]}
        self.listing['artifacts'][-1]['created_at'] = '2026-09-20T00:00:00Z'
        result = self.reader.collect(NOW)
        self.assertEqual(self.pages_read, [1])
        self.assertFalse(result['partial'])

    def test_a_listing_longer_than_the_page_cap_is_partial(self):
        self.listing = {'total_count': 900, 'artifacts': self.filler('2026-09-30T11:30:00Z')}
        self.more = {n: {'artifacts': self.filler('2026-09-30T11:30:00Z')} for n in range(2, 10)}
        self.assertTrue(self.reader.collect(NOW)['partial'])
        self.assertEqual(self.pages_read, [1, 2, 3, 4, 5])

    def test_page_cap_does_not_force_partial_when_the_plan_window_is_covered(self):
        # Five pages never reach the seven-day cutoff, but the oldest artifact is before the window.
        self.listing = {'total_count': 900, 'artifacts': self.filler('2026-09-30T11:30:00Z')}
        self.more = {n: {'artifacts': self.filler('2026-09-30T11:30:00Z')} for n in range(2, 5)}
        self.more[5] = {'artifacts': self.filler('2026-09-24T12:00:00Z')}
        result = self.reader.collect(NOW, window_start=NOW - timedelta(days=2))
        self.assertFalse(result['hard_partial'])
        self.assertFalse(result['partial'])
        self.assertEqual(self.pages_read, [1, 2, 3, 4, 5])

    def test_a_page_cap_that_stops_inside_the_plan_window_stays_partial(self):
        self.listing = {'total_count': 900, 'artifacts': self.filler('2026-09-30T11:30:00Z')}
        self.more = {n: {'artifacts': self.filler('2026-09-30T11:30:00Z')} for n in range(2, 10)}
        result = self.reader.collect(NOW, window_start=NOW - timedelta(days=3))
        self.assertFalse(result['hard_partial'])
        self.assertTrue(result['partial'])

    def test_an_unreadable_artifact_stays_partial_when_the_window_is_covered(self):
        self.run['path'] = '.github/workflows/unrelated.yml'
        result = self.reader.collect(NOW, window_start=NOW - timedelta(days=1))
        self.assertTrue(result['hard_partial'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['samples'], [])

    def test_explore_usage_keeps_the_lane_instance_from_the_runner_name(self):
        self.runner_name = 'box-ci-qae-2-1700000000'
        result = self.reader.collect(NOW)
        self.assertEqual(result['samples'][0]['instance'], 2)
        self.reader.cache.clear()
        self.reader.updated = 0
        self.elapsed = 301
        self.runner_name = 'GitHub Actions 4'
        plain = self.reader.collect(NOW)
        self.assertIsNone(plain['samples'][0]['instance'])

    def test_a_spent_call_budget_keeps_the_history_already_read(self):
        self.assertEqual(len(self.reader.collect(NOW)['samples']), 1)
        self.elapsed, self.exhausted = 301, True
        result = self.reader.collect(NOW)
        self.assertTrue(result['partial'])
        self.assertEqual(result['samples'][0]['input_tokens'], 100)
        self.assertEqual(len(self.reader.cache), 1)
        self.elapsed, self.exhausted = 602, False
        self.assertFalse(self.reader.collect(NOW)['partial'])
        self.assertEqual(self.downloads, 1)


if __name__ == '__main__':
    unittest.main()
