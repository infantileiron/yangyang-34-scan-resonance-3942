import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB_FILE = Path(tempfile.mkdtemp(prefix='desk-test-')) / 'test.db'
os.environ['DATABASE_URL'] = f'sqlite+pysqlite:///{DB_FILE}'
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
os.environ['SEED_DEMO'] = 'false'
os.environ['ARTIFACT_ROOT'] = str(DB_FILE.parent / 'artifacts')


class _Transaction:
    def __init__(self, store):
        self.store = store
        self.ops = []

    def incr(self, key):
        self.ops.append(('incr', key, None))
        return self

    def expire(self, key, seconds, nx=False):
        self.ops.append(('expire', key, seconds))
        return self

    def execute(self):
        results = []
        for op, key, value in self.ops:
            if op == 'incr':
                self.store[key] = self.store.get(key, 0) + 1
                results.append(self.store[key])
            else:
                results.append(True)
        self.ops.clear()
        return results

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRedis:
    """Only the login throttle surface used by /api/auth/login."""

    def __init__(self):
        self.store = {}

    def pipeline(self):
        return _Transaction(self.store)

    def ping(self):
        return True

    def set(self, *args, **kwargs):
        return True

    def get(self, key):
        return None

    def blpop(self, *args, **kwargs):
        return None

    def lpush(self, *args, **kwargs):
        return 1

    def ltrim(self, *args, **kwargs):
        return True


fake_redis = FakeRedis()

import src.routes.session as session_route  # noqa: E402
import src.routes.analysis as analysis_route  # noqa: E402
import src.routes.reports as reports_route  # noqa: E402
import src.main as main_module  # noqa: E402
import src.worker as worker_module  # noqa: E402

session_route.redis = fake_redis
main_module.redis = fake_redis
worker_module.redis = fake_redis
analysis_route.wake_worker = lambda: None
reports_route.wake_worker = lambda: None
