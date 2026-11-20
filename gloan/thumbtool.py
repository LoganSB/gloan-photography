"""Runs tools/make_thumbnails.py on demand (owner request) as a separate process and reports its status."""

import subprocess
import sys
import threading

from gloan.config import ROOT
from gloan.storage import available_rows, bump_index_generation, thumb_path

TOOL_PATH = ROOT / 'tools' / 'make_thumbnails.py'
JOB_TIMEOUT_SECONDS = 1800  # a huge first run may take a while, but must not hang forever

_state_lock = threading.Lock()  # guards _state across handler threads
_state: dict = {'running': False, 'exitCode': None, 'output': ''}


def _run_job() -> None:
    """Thread body: run the tool to completion and record how it went."""
    try:
        # Fixed argument list, no shell: nothing from the request reaches the command line.
        result = subprocess.run([sys.executable, str(TOOL_PATH)], cwd=ROOT, capture_output=True, text=True,
                                timeout=JOB_TIMEOUT_SECONDS, check=False)
        outcome = {'exitCode': result.returncode, 'output': (result.stdout + result.stderr)[-2000:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        outcome = {'exitCode': -1, 'output': f'could not run the thumbnail tool: {error}'}
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
