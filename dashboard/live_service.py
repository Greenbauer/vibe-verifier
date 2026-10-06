"""Compose independent GitHub, runner, quota and numeric usage sources for the UI."""
import copy
import threading
from datetime import timedelta

from .collector_view import join_runner_jobs
from .gh_api import ApiError
from .service import DashboardService, PRIVATE_FAILURES
from .usage_artifacts import UsageArtifacts
from .util import parse_time
from .agents import agent_view

USAGE_REFRESH_SECONDS = 300
# A refresh starts at most one interval after the last one ended and then takes its own time, so a
# history is stale only once it misses a whole cycle.
USAGE_STALE_AFTER = timedelta(seconds=2 * USAGE_REFRESH_SECONDS)


class LiveService(DashboardService):
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.usage_reader = UsageArtifacts(config)
        self._usage_result = None
        self._usage_next = 0
        self._usage_lock = threading.Lock()
        self._usage_running = False
        self._usage_generation = 0
        self._usage_revoked = False

    def _refresh_usage(self, reader=None, generation=None):
        with self._usage_lock:
            reader = reader or self.usage_reader
            generation = self._usage_generation if generation is None else generation
        value, failed = None, False
        try:
            value = reader.collect()
        except (ApiError, OSError, ValueError):
            failed = True
        finally:
            with self._usage_lock:
                if generation == self._usage_generation:
                    self._usage_result = value
                    self._usage_next = self.monotonic() + USAGE_REFRESH_SECONDS
                    if failed:
                        self.usage_reader = UsageArtifacts(self.config)
                self._usage_running = False

    def snapshot(self, **kwargs):
        value = super().snapshot(nonblocking=True, **kwargs)
        revoked = bool(self._source_error_codes(value['github']) & PRIVATE_FAILURES)
        with self._usage_lock:
            if revoked and not self._usage_revoked:
                self._usage_generation += 1
                self._usage_result = None
                # Replace the cache owner instead of mutating the reader while it is
                # collecting. Its old generation cannot publish after revocation.
                self.usage_reader = UsageArtifacts(self.config)
                self._usage_next = 0
            self._usage_revoked = revoked
            capture_ci_usage = not self.config.agents or any(agent.workflow_role for agent in self.config.agents)
            if capture_ci_usage and not revoked and not self._usage_running and self.monotonic() >= self._usage_next:
                self._usage_running = True
                threading.Thread(target=self._refresh_usage,
                                 args=(self.usage_reader, self._usage_generation), daemon=True).start()
            usage = copy.deepcopy(self._usage_result) if not revoked else None
        if usage:
            observed = parse_time(usage.get('sampled_at'))
            age = self.wall_clock() - observed if observed else None
            usage['stale'] = (usage.get('stale', False) or age is None or
                              age < timedelta(0) or age > USAGE_STALE_AFTER)
            usage['history_sampled_at'] = usage.get('sampled_at')
            usage['history_stale'] = usage['stale']
            if usage['stale']:
                usage['completeness'] += ' Token history is stale; last observation: %s.' % (
                    usage.get('sampled_at') or 'unavailable')
        if usage:
            telemetry = value['telemetry']
            existing = telemetry.get('usage', {})
            if existing.get('available'):
                existing['accounts'].extend(usage['accounts'])
                existing['samples'].extend(usage['samples'])
                existing['completeness'] += ' ' + usage['completeness']
                existing['partial'] = usage.get('partial', False)
                existing['history_sampled_at'] = usage['history_sampled_at']
                existing['history_stale'] = usage['history_stale']
            else:
                telemetry.update(available=True, usage=usage)
        join_runner_jobs(value['telemetry'], value['github'])
        value['agents'] = agent_view(self.config, value['github'], value['telemetry'], self.wall_clock())
        return value
