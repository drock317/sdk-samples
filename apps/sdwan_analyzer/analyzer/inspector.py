"""Storage Inspector: on-demand metadata about what the app is storing.

Engineering visibility only. NOT a filesystem browser and NOT persisted:
every call builds a fresh report, adds nothing to history, starts no
timer/scheduler, and does no router (NCOS) I/O.

File inspection is a strict allowlist:
  * only paths under the app's OWN directory (APP_ROOT) and the dedicated
    future telemetry directory (DATA_DIR_NAME, a child of APP_ROOT);
  * symlinks are never followed or listed (so a link cannot escape);
  * only known app-file extensions are listed, and anything that looks like
    credentials/keys/certs/tokens is excluded by name;
  * only metadata (relative path, size, mtime, type) is returned, never
    file contents.
"""

import os
import time

# Directory (relative to the app root) reserved for FUTURE durable telemetry
# files. Nothing writes here yet; durable storage is not implemented.
DATA_DIR_NAME = 'data'

# Durable telemetry is not implemented. Flip only when a real backend exists.
PERSISTENT_ENABLED = False

MAX_FILES = 500          # listing cap per section
MAX_DEPTH = 6            # directory depth cap

# extension (lower case) -> reported type. Anything else is not listed.
_TYPE_BY_EXT = {
    '.py': 'code', '.sh': 'code',
    '.md': 'doc', '.txt': 'doc',
    '.ini': 'config', '.json': 'config',
    '.jsonl': 'data', '.log': 'data', '.ndjson': 'data',
    '.ds': 'metadata',
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


def build_report(memory, app_root, now=None):
    """Assemble the full Storage Inspector response.

    `memory` is the counts-only in-memory section built by the service.
    """
    now = int(time.time() if now is None else now)
    data_root = os.path.join(app_root, DATA_DIR_NAME)
    app = scan_files(app_root, skip_dirs=(data_root,))
    app.pop('root_exists', None)
    app['root'] = os.path.basename(os.path.normpath(app_root)) or '.'
    pers = scan_files(data_root)
    persistent = {
        'enabled': PERSISTENT_ENABLED,
        'root': DATA_DIR_NAME,
        'root_exists': pers['root_exists'],
        'total_bytes': pers['total_bytes'],
        'file_count': pers['file_count'],
        'files': pers['files'],
        'truncated': pers['truncated'],
        'excluded_count': pers['excluded_count'],
        'skipped_symlinks': pers['skipped_symlinks'],
    }
    return {'generated_ts': now, 'memory': memory,
            'persistent': persistent, 'application_files': app}
