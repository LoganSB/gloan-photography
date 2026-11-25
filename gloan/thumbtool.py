"""Runs thumbnail generation on demand (owner request) in a background thread sharing INDEX_LOCK."""

import importlib.util
import threading

from gloan.config import ROOT
from gloan.storage import available_rows, bump_index_generation, thumb_path

TOOL_PATH = ROOT / 'tools' / 'make_thumbnails.py'

_state_lock = threading.Lock()  # guards _state across handler threads
_state: dict = {'running': False, 'exitCode': None, 'output': ''}


def _run_job() -> None:
    """Thread body: execute `tools/make_thumbnails.py:generate_thumbnails()` in-process so it shares INDEX_LOCK."""
    try:
        spec = importlib.util.spec_from_file_location('gloan_make_thumbnails', TOOL_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError('thumbnail tool module not found')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _, failed, output = module.generate_thumbnails()
        outcome = {'exitCode': 1 if failed else 0, 'output': output[-2000:]}
    except SystemExit as exc:
        outcome = {'exitCode': 1, 'output': str(exc)[-2000:]}
    except Exception as error:  # noqa: BLE001
        outcome = {'exitCode': -1, 'output': f'could not run the thumbnail tool: {error}'[-2000:]}
    with _state_lock:
        _state.update(running=False, **outcome)
    bump_index_generation()


def start_thumbnail_job() -> bool:
    """Start the tool in the background. Returns False if a run is already in progress."""
    with _state_lock:
        if _state['running']:
            return False
        _state.update(running=True, exitCode=None, output='')
    threading.Thread(target=_run_job, daemon=True).start()
    return True


def thumbnail_status() -> dict:
    """Whether a run is active, how the last run ended, and how many photos still lack a tile."""
    with _state_lock:
        status = dict(_state)
    status['missing'] = sum(1 for row in available_rows() if thumb_path(row) is None)
    return status
