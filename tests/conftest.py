"""Run the default test suite against temporary local storage, never WorkBuddy."""
import os
import tempfile

_storage = tempfile.TemporaryDirectory(prefix='lease-tests-')
os.environ['ZL_STORAGE'] = 'local'
os.environ['ZL_DATA_DIR'] = _storage.name
os.environ['ZL_SESSION_KEY'] = 'isolated-tests-only-not-for-deployment'
