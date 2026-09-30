"""Usage counts must have current-owner GitHub provenance and never include content."""
import copy
import io
import json
import unittest
import zipfile
from datetime import datetime, timezone

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
        self.elapsed, self.downloads, self.revoked = 0, 0, False
        config = Config('example', (REPO,), {'explorer': BotDefinition('explore.yml', ('explore',))}, None)
        self.reader = UsageArtifacts(config, api=self, downloader=self.download, clock=lambda: self.elapsed)

    def begin(self):
        pass

    def one(self, endpoint):
        if self.revoked:
            raise ApiError('not_found')
        if endpoint.endswith('actions/artifacts?per_page=100&page=1'):
            return self.listing
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


if __name__ == '__main__':
    unittest.main()
