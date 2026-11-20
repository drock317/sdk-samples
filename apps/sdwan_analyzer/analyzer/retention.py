"""History retention policy and disk-space safety math (v0.8.4).

Pure functions plus constants; no I/O. durable.py applies them.

Policy (normal history only; Recording retention is independent):
  * history_max_bytes  default 256 MiB, bounds 32 MiB .. 1 GiB
  * history_max_days   default 7,       bounds 1 .. 30
Operators may set them through read-only App Data
(`sdwan_analyzer_history_max_mb`, `sdwan_analyzer_history_max_days`); the app
never writes defaults to App Data. Invalid values fall back to the default
and are reported; out-of-bound values are clamped and reported.

Space safety: history writes pause when free space falls below a reserve and
resume only above a higher threshold (hysteresis). The size cap is also
lowered when the disk could not hold it without eating into the reserve.
"""

MIB = 1024 * 1024

DEFAULT_HISTORY_MAX_BYTES = 256 * MIB
MIN_HISTORY_MAX_BYTES = 32 * MIB
MAX_HISTORY_MAX_BYTES = 1024 * MIB
DEFAULT_HISTORY_MAX_DAYS = 7
MIN_HISTORY_MAX_DAYS = 1
MAX_HISTORY_MAX_DAYS = 30

SWEEP_INTERVAL_S = 3600            # periodic retention sweep
SPACE_CHECK_INTERVAL_S = 30        # statvfs throttle
EVENT_MIN_INTERVAL_S = 300         # pause/resume event rate limit

RESERVE_MIN_BYTES = 512 * MIB      # never write history below this free space
RESERVE_FRACTION = 0.10            # ... or 10% of the filesystem if larger
RESUME_MARGIN_MIN = 64 * MIB       # extra free space needed to resume
MIN_EFFECTIVE_CAP = 16 * MIB       # floor when the disk limits the cap
CAP_SAFETY_MARGIN = 64 * MIB       # kept free beyond the reserve for the cap

REASON_LOW_SPACE = 'low_space'
REASON_DAY_OVERFLOW = 'day_file_exceeds_cap'


def _parse_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(float(text)) if text.replace('.', '', 1).isdigit() else None
    except (TypeError, ValueError, OverflowError):
        return None


def _one(name, raw, default, lo, hi):
    """-> (effective, report). `raw` may be None (unset)."""
    rep = {'default': default, 'min': lo, 'max': hi, 'requested': raw,
           'effective': default, 'source': 'default'}
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default, rep
    val = _parse_int(raw)
    if val is None:
        rep['source'] = 'invalid_using_default'
        return default, rep
    if val < lo or val > hi:
        eff = min(max(val, lo), hi)
        rep.update(effective=eff, source='clamped')
        return eff, rep
    rep.update(effective=val, source='appdata')
    return val, rep


def normalize_history_policy(max_mb=None, max_days=None):
    """App Data strings -> ({'history_max_bytes', 'history_max_age_s'},
    report). Never raises."""
    mb, mb_rep = _one('history_max_mb', max_mb,
                      DEFAULT_HISTORY_MAX_BYTES // MIB,
                      MIN_HISTORY_MAX_BYTES // MIB,
                      MAX_HISTORY_MAX_BYTES // MIB)
    days, d_rep = _one('history_max_days', max_days,
                       DEFAULT_HISTORY_MAX_DAYS, MIN_HISTORY_MAX_DAYS,
                       MAX_HISTORY_MAX_DAYS)
    limits = {'history_max_bytes': mb * MIB,
              'history_max_age_s': days * 86400}
    report = {'history_max_mb': mb_rep, 'history_max_days': d_rep,
              'appdata_fields': {
                  'history_max_mb': 'sdwan_analyzer_history_max_mb',
                  'history_max_days': 'sdwan_analyzer_history_max_days'}}
    return limits, report


def space_reserve(total_bytes, override=None):
    """Free-space floor below which history writes pause."""
    if isinstance(override, int) and override >= 0:
        return override
    frac = int((total_bytes or 0) * RESERVE_FRACTION)
    return max(RESERVE_MIN_BYTES, frac)


def resume_threshold(reserve):
    """Free space required before paused writes resume (hysteresis)."""
    return reserve + max(RESUME_MARGIN_MIN, reserve // 4)


def effective_cap(configured, history_bytes, free_bytes, reserve):
    """History size cap after the disk's own limits.

    The history may use what it already occupies plus the free space above
    the reserve (minus a safety margin). Returns (cap, limited_by_disk).
    """
    avail = history_bytes + free_bytes - reserve - CAP_SAFETY_MARGIN
    cap = min(configured, max(MIN_EFFECTIVE_CAP, avail))
    return cap, cap < configured


def limiting_factor(days_present, max_days, total_bytes, cap, disk_limited):
    """Which limit currently bounds retention (reporting only)."""
    if disk_limited:
        return 'disk_space'
    if total_bytes >= cap:
        return 'size'
    if days_present >= max_days:
        return 'age'
    return 'none'
