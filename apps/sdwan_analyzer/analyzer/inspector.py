"""Storage Inspector: on-demand metadata about what the app is storing.

Engineering visibility only. NOT a filesystem browser and NOT persisted:
every call builds a fresh report, adds nothing to history, starts no
timer/scheduler, and does no router (NCOS) I/O.

File inspection is a strict allowlist:
  * only paths under the app's OWN directory (APP_ROOT) and the durable
    telemetry directory (tmp/telemetry, a child of APP_ROOT; reported in its
    own `persistent` section and never double counted in `application_files`);
  * symlinks are never followed or listed (so a link cannot escape);
  * only known app-file extensions are listed, and anything that looks like
    credentials/keys/certs/tokens is excluded by name;
  * only metadata (relative path, size, mtime, type) is returned, never
    file contents.
"""

import os
import time

# Durable telemetry root (relative to the app root) and the whole app-local
# runtime directory that is excluded from the application-file listing.
TELEMETRY_REL_ROOT = 'tmp/telemetry'
RUNTIME_DIR_NAME = 'tmp'

MAX_FILES = 500          # listing cap per section
MAX_DEPTH = 6            # directory depth cap

# extension (lower case) -> reported type. Anything else is not listed.
_TYPE_BY_EXT = {
    '.py': 'code', '.sh': 'code',
    '.md': 'doc', '.txt': 'doc',
    '.ini': 'config', '.json': 'config',
    '.jsonl': 'data', '.log': 'data', '.ndjson': 'data',
    '.ds': 'metadata',
    # Durable-storage working/recovery files (see persistence.py).
    '.tmp': 'working', '.bak': 'backup', '.corrupt': 'quarantine',
}

# Exact names never listed.
_DENY_NAMES = frozenset((
    'sdk_settings.ini', 'sdk_settings.ini.example', '.env', '.netrc',
    'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'authorized_keys',
    'known_hosts', 'shadow', 'passwd',
))
# Substrings in a (lower-case) name that mark it as sensitive.
_DENY_FRAGMENTS = ('secret', 'token', 'credential', 'password', 'passwd',
                   'private', 'apikey', 'api_key', 'cert', '.pem', '.key',
                   '.crt', '.cer', '.p12', '.pfx', '.jks')


def _is_sensitive(name):
    low = name.lower()
    return low in _DENY_NAMES or any(f in low for f in _DENY_FRAGMENTS)


def _file_type(name):
    return _TYPE_BY_EXT.get(os.path.splitext(name.lower())[1])


def _inside(root_real, path):
    """True when `path` resolves inside `root_real` (symlink-safe)."""
    try:
        real = os.path.realpath(path)
        return real == root_real or real.startswith(root_real + os.sep)
    except Exception:
        return False


def scan_files(root, skip_dirs=(), max_files=MAX_FILES, max_depth=MAX_DEPTH):
    """Metadata for allowed files under `root`. Never raises.

    Returns {'root_exists', 'file_count', 'total_bytes', 'files',
    'truncated', 'excluded_count', 'skipped_symlinks'}. file_count and
    total_bytes cover exactly the listed files (when truncated they cover
    the files seen up to the cap). Excluded files are only counted, never
    named.
    """
    out = {'root_exists': False, 'file_count': 0, 'total_bytes': 0,
           'files': [], 'truncated': False, 'excluded_count': 0,
           'skipped_symlinks': 0}
    try:
        if not root or not os.path.isdir(root) or os.path.islink(root):
            return out
        root_real = os.path.realpath(root)
        out['root_exists'] = True
        skip_real = set(os.path.realpath(d) for d in skip_dirs)
        for dirpath, dirnames, filenames in os.walk(root_real,
                                                    followlinks=False):
            rel_dir = os.path.relpath(dirpath, root_real)
            depth = 0 if rel_dir == '.' else rel_dir.count(os.sep) + 1
            keep = []
            for d in sorted(dirnames):
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    out['skipped_symlinks'] += 1
                elif os.path.realpath(full) in skip_real:
                    continue
                elif (depth + 1 > max_depth or d == '__pycache__'
                      or _is_sensitive(d)):
                    out['excluded_count'] += 1
                else:
                    keep.append(d)
            dirnames[:] = keep
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    out['skipped_symlinks'] += 1
                    continue
                ftype = _file_type(name)
                if (ftype is None or _is_sensitive(name)
                        or not _inside(root_real, full)):
                    out['excluded_count'] += 1
                    continue
                if len(out['files']) >= max_files:
                    out['truncated'] = True
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root_real).replace(os.sep, '/')
                out['files'].append({'relative_path': rel,
                                     'size_bytes': st.st_size,
                                     'modified_ts': int(st.st_mtime),
                                     'type': ftype})
                out['total_bytes'] += st.st_size
        out['file_count'] = len(out['files'])
    except Exception:
        pass
    return out


def _area_of(rel):
    """Storage area of a path relative to the telemetry root."""
    head = rel.split('/', 1)[0]
    if head == 'history':
        return 'history'
    if head == 'recordings':
        return 'recordings'
    if head.startswith('config_versions'):
        return 'config'
    if head.startswith('events'):
        return 'events'
    if head.startswith('state'):
        return 'state'
    return 'other'


def _breakdown(files):
    """Per-area file/byte subtotals (+ quarantine and Recording job count)."""
    areas = {a: {'files': 0, 'bytes': 0} for a in (
        'history', 'recordings', 'config', 'events', 'state', 'other')}
    quarantine = {'files': 0, 'bytes': 0}
    jobs = set()
    for f in files:
        rel = f['relative_path']
        a = areas[_area_of(rel)]
        a['files'] += 1
        a['bytes'] += f['size_bytes']
        if f['type'] == 'quarantine':
            quarantine['files'] += 1
            quarantine['bytes'] += f['size_bytes']
        parts = rel.split('/')
        if parts[0] == 'recordings' and len(parts) >= 3:
            jobs.add(parts[1])
    areas['recordings']['jobs'] = len(jobs)
    return areas, quarantine


def build_report(memory, app_root, now=None, persistent=None):
    """Assemble the full Storage Inspector response.

    `memory` is the counts-only in-memory section built by the service.
    `persistent` is the durable backend's own status dict (None when the
    app runs with in-memory stores only; then it may carry only
    `init_error` / `init_error_type` explaining why durable init failed): enabled flag, root, write health,
    retention limits, recovery summary and free-space information.
    """
    now = int(time.time() if now is None else now)
    runtime_root = os.path.join(app_root, RUNTIME_DIR_NAME)
    # The whole runtime dir is excluded so tmp/ is never double counted.
    app = scan_files(app_root, skip_dirs=(runtime_root,))
    app.pop('root_exists', None)
    app['root'] = os.path.basename(os.path.normpath(app_root)) or '.'
    info = persistent or {}
    scan_root = info.get('abs_root') or os.path.join(
        app_root, *TELEMETRY_REL_ROOT.split('/'))
    pers = scan_files(scan_root)
    areas, quarantine = _breakdown(pers['files'])
    out = {
        'enabled': bool(info.get('enabled', False)),
        'root': info.get('root', TELEMETRY_REL_ROOT),
        'root_exists': pers['root_exists'],
        'total_bytes': pers['total_bytes'],
        'file_count': pers['file_count'],
        'files': pers['files'],
        'truncated': pers['truncated'],
        'excluded_count': pers['excluded_count'],
        'skipped_symlinks': pers['skipped_symlinks'],
        'history': areas['history'],
        'recordings': areas['recordings'],
        'config': areas['config'],
        'events': areas['events'],
        'state': areas['state'],
        'quarantine': quarantine,
        'run_id': info.get('run_id'),
        'status': info.get('status', 'disabled'),
        'init_error': info.get('init_error'),
        'init_error_type': info.get('init_error_type'),
        'write_errors': info.get('write_errors', 0),
        'last_write_error': info.get('last_write_error'),
        'last_write_error_ts': info.get('last_write_error_ts'),
        'last_fsync_ts': info.get('last_fsync_ts'),
        'clock_sane': info.get('clock_sane', True),
        'held_observations': info.get('held_observations', 0),
        'persisted_observations': info.get('persisted_observations', 0),
        'retention': info.get('retention'),
        'recovery': info.get('recovery'),
        'free_space': info.get('free_space'),
    }
    return {'generated_ts': now, 'memory': memory,
            'persistent': out, 'application_files': app}
