"""Application API: composes collectors + normalize + storage.

This is the ONLY layer the presentation (web) tier is allowed to call. It
owns router polling on two cadences, caches the latest normalized snapshot,
and exposes plain-dict sections ready for JSON. The browser never reaches
NCOS APIs -- it reaches web, which reaches here, which serves the CACHED
snapshot. Web requests NEVER trigger router collection (COLLECTION
PERFORMANCE / SAFETY).

Cadence model
-------------
- FAST cadence: lightweight topology/state (collect_base_snapshot). Small,
  frequently-changing trees.
- SLOW cadence: heavier runtime + static config (collect_heavy_snapshot),
  including the loadbalancers endpoint which is slow on the R1900.

The two raw passes are cached independently and merged into the composed
snapshot. A collector failure keeps the last-known-good raw for that group
plus an error/timestamp, so one failing optional collector never blanks the
whole snapshot or stops the app.

Business logic lives here (and in normalize), never in the web layer.
"""

import threading
import time

import cp

from . import collectors
from . import normalize
from . import storage

# Conservative default cadences (seconds). Centralized + overridable via the
# constructor so they are not scattered or chosen blindly. Heavy trees
# (loadbalancers/bonding/QoE/config) run much slower than topology.
DEFAULT_FAST_INTERVAL = 15
DEFAULT_SLOW_INTERVAL = 120


class _RawCache(object):
    """Per-collector last-known-good cache for one cadence group.

    A collection pass returns {key: collectors.CollectResult}. Each key is
    merged INDEPENDENTLY:
      * a successful read (ok=True, even value None/{}/[]) replaces that
        key's value and clears its error;
      * a failed read (ok=False) RETAINS the previous value for that key and
        records the error + error timestamp.
    A single failing endpoint therefore never erases unrelated cached data.
    Never raises.
    """

    def __init__(self):
        self.data = {}                # key -> last-known-good value
        self._updated = {}            # key -> ts of last successful read
        self._errors = {}            # key -> last error string
        self._error_ts = {}          # key -> ts of last error
        self.updated_ts = None        # ts of last pass that updated any key

    def update(self, fetch_callable):
        try:
            results = fetch_callable()
        except Exception as e:  # collection framework failure; never crash
            cp.log('service: raw collection pass error: {}'.format(e))
            return
        if not isinstance(results, dict):
            return
        now = int(time.time())
        any_success = False
        for key, res in results.items():
            ok = getattr(res, 'ok', None)
            if ok is None:
                # Back-compat: a bare value (not a CollectResult) is a success.
                self.data[key] = res
                self._updated[key] = now
                self._errors.pop(key, None)
                self._error_ts.pop(key, None)
                any_success = True
                continue
            if res.ok:
                self.data[key] = res.value
                self._updated[key] = now
                self._errors.pop(key, None)
                self._error_ts.pop(key, None)
                any_success = True
            else:
                # Retain previous value; record per-key error + timestamp.
                self._errors[key] = res.error
                self._error_ts[key] = now
                cp.log('service: collector {} failed ({}); '
                       'keeping last-known-good'.format(key, res.error))
        if any_success:
            self.updated_ts = now

    def health(self):
        """Per-collector health: {key: {updated_ts, error, error_ts}}."""
        keys = set(self.data) | set(self._errors)
        out = {}
        for k in keys:
            out[k] = {
                'updated_ts': self._updated.get(k),
                'error': self._errors.get(k),
                'error_ts': self._error_ts.get(k),
            }
        return out


class AnalyzerService(object):
    def __init__(self, store=None,
                 fast_interval=DEFAULT_FAST_INTERVAL,
                 slow_interval=DEFAULT_SLOW_INTERVAL):
        self._store = store if store is not None else storage.default_store()
        self._web_port_status = None

        self._fast_interval = fast_interval
        self._slow_interval = slow_interval

        # Independent raw caches per cadence group.
        self._base_raw = _RawCache()
        self._heavy_raw = _RawCache()

        # Composed normalized snapshot cache + guard.
        self._lock = threading.Lock()
        self._snapshot = None
        self._snapshot_ts = None

        # Fingerprints from the previous composed snapshot, for change-ready
        # history (topology / config / logical-policy churn detection).
        self._prev_fingerprints = {}

    def set_web_port_provider(self, provider):
        """Register a zero-arg callable that returns web port status."""
        self._web_port_status = provider

    # --- cadence intervals ------------------------------------------------

    def fast_interval(self):
        return self._fast_interval

    def slow_interval(self):
        return self._slow_interval

    # --- collection (background collector owns these) ---------------------

    def refresh_base(self):
        """Run one FAST (lightweight topology) collection pass into cache."""
        self._base_raw.update(collectors.collect_base_snapshot)

    def refresh_heavy(self):
        """Run one SLOW (heavy runtime + static config) collection pass."""
        self._heavy_raw.update(collectors.collect_heavy_snapshot)

    def recompose(self):
        """Recompose the normalized snapshot from cached raw and store it.

        Called by the background collector after a refresh. Web requests do
        NOT call this -- they read the cached snapshot via snapshot().
        """
        snap = self._compose_snapshot()
        with self._lock:
            self._snapshot = snap
            self._snapshot_ts = snap.get('timestamp')
        # Store a COMPACT history sample, never the full snapshot. The live
        # snapshot keeps raw investigative fields; history must stay bounded
        # in memory (R1900), so it holds only analysis-useful normalized
        # values -- no raw passthrough trees, no compiled_rules array.
        self._store.append(self._compact_history_sample(snap))
        return snap

    def _compact_history_sample(self, snap):
        """Project a full snapshot into a compact, memory-bounded sample.

        Contains ONLY analysis-useful normalized values -- no raw passthrough
        trees, no compiled_rules array, no per-object 'raw'. This is what
        /api/history serves; the live /api/snapshot keeps the richer data.
        """
        if not isinstance(snap, dict):
            return {'timestamp': int(time.time())}
        wan = snap.get('wan', {}) or {}
        ts = snap.get('traffic_steering', {}) or {}

        wan_states = {}
        underlay = {}
        for p in wan.get('paths', []):
            if isinstance(p, dict):
                wan_states[p.get('uid')] = p.get('connection_state')
                if p.get('underlay_uid'):
                    underlay[p.get('uid')] = p.get('underlay_uid')

        swans = [{'uid': p.get('uid'), 'latency_ms': p.get('latency_ms'),
                  'jitter_ms': p.get('jitter_ms')}
                 for p in (snap.get('swans', {}) or {}).get('paths', [])
                 if isinstance(p, dict)]
        qoe = [{'uid': p.get('uid'),
                'latency_avg_ms': p.get('latency_avg_ms'),
                'latency_last_ms': p.get('latency_last_ms'),
                'loss': p.get('loss')}
               for p in (snap.get('qoe', {}) or {}).get('paths', [])
               if isinstance(p, dict)]

        rules = []
        for lr in ts.get('logical_rules', []):
            if not isinstance(lr, dict):
                continue
            rules.append({
                'logical_name': lr.get('logical_name'),
                'fingerprint': lr.get('semantic_fingerprint'),
                'selector': lr.get('wan_selector_type'),
                'preferred_wan': lr.get('preferred_wan'),
                'steering_to': lr.get('steering_to'),
                'runtime_algorithm': lr.get('runtime_algorithm'),
                'preferred_flows': lr.get('preferred_flows'),
                'nonpreferred_flows': lr.get('nonpreferred_flows'),
                'latest_event_reason': lr.get('latest_event_reason'),
            })

        lbs = []
        for lb in (snap.get('loadbalancers', []) or []):
            if not isinstance(lb, dict):
                continue
            lbs.append({
                'name': lb.get('name'),
                'algorithm': lb.get('algorithm'),
                'active_device': lb.get('active_device'),
                'scores': {m.get('uid'): m.get('score')
                           for m in lb.get('members', [])
                           if isinstance(m, dict)},
            })

        bonds = []
        for b in (snap.get('bonding', {}) or {}).get('bonds', []):
            if not isinstance(b, dict):
                continue
            bonds.append({
                'iface': b.get('iface'),
                'name': b.get('name'),
                'total_download_flows': b.get('total_download_flows'),
                'total_upload_flows': b.get('total_upload_flows'),
                'members': [{
                    'member_uid': m.get('member_uid'),
                    'configured_weight': m.get('configured_weight'),
                    'current_download_weight': m.get('current_download_weight'),
                    'current_upload_weight': m.get('current_upload_weight'),
                    'download_flows': m.get('download_flows'),
                    'upload_flows': m.get('upload_flows'),
                } for m in b.get('members', []) if isinstance(m, dict)],
            })

        return {
            'timestamp': snap.get('timestamp'),
            'primary_device': wan.get('primary_device'),
            'wan_states': wan_states,
            'underlay_map': underlay,
            'swans': swans,
            'qoe': qoe,
            'logical_rules': rules,
            'loadbalancers': lbs,
            'bonding': bonds,
            'fingerprints': snap.get('fingerprints', {}),
            'changes': snap.get('changes', {}),
        }

    # --- composition (pure over cached raw; no router I/O except WBOND) ---

    def _compose_snapshot(self):
        base = self._base_raw.data or {}
        heavy = self._heavy_raw.data or {}

        primary = base.get('primary_device')
        raw_devices = base.get('wan_devices')
        devices = raw_devices if isinstance(raw_devices, dict) else {}

        # SD-WAN underlay correlation (per overlay device).
        sdwan_underlays = self._resolve_sdwan_underlays(devices)
        underlay_map = {u['sdwan_uid']: u['underlay_uid']
                        for u in sdwan_underlays if u.get('underlay_uid')}

        # WAN path inventory with object_class + underlay-aware naming.
        wan_paths = normalize.wan_paths(raw_devices, primary, underlay_map)
        physical = [p for p in wan_paths
                    if p.get('object_class') == normalize.OBJ_PHYSICAL]
        overlays = [p for p in wan_paths
                    if p.get('object_class') == normalize.OBJ_SDWAN]

        # Destination identity index (from cached config).
        dest_index = normalize.destination_index(heavy.get('identities_ip'))

        # Logical Traffic Steering grouping + correlation.
        logical = normalize.logical_rules(
            base.get('steering_rules'),
            base.get('steering_stats'),
            base.get('steering_events'),
            dest_index)

        events = normalize.steering_events(base.get('steering_events'))
        stats = normalize.steering_stats(base.get('steering_stats'))

        # SWANS + QoE (kept separate concepts).
        swans = normalize.swans_paths(base.get('swans'))
        qoe = normalize.qoe_paths(heavy.get('qoe'))

        # Traffic Classes (config-authoritative; merges both config trees).
        traffic_classes = normalize.traffic_classes(
            heavy.get('identities_intent'),
            heavy.get('traffic_class_config'))

        # Loadbalancers (heavy tree).
        lbs = normalize.loadbalancers(heavy.get('loadbalancers'))

        # WAN bonding detailed runtime + member friendly names.
        bonding = self._compose_bonding(heavy.get('wan_bonding'), devices)

        # WAN config correlation (config/wan/rules2) -- read-only.
        wan_rules2 = normalize.wan_rules2_index(heavy.get('wan_rules2'))

        compiled_rules = base.get('steering_rules') or []
        if isinstance(compiled_rules, dict):
            compiled_rules = list(compiled_rules.values())

        snapshot = {
            'timestamp': int(time.time()),
            'system_summary': {
                'primary_device': primary,
                'wan_path_count': len(wan_paths),
                'physical_count': len(physical),
                'overlay_count': len(overlays),
                'bond_count': len(bonding.get('bonds', [])),
                'logical_rule_count': len(logical),
            },
            'wan': {
                'primary_device': primary,
                'paths': wan_paths,
                'physical': physical,
                'overlays': overlays,
                'underlay_map': sdwan_underlays,
                'config': wan_rules2,
            },
            'swans': swans,
            'traffic_classes': traffic_classes,
            'traffic_steering': {
                'logical_rules': logical,
                'compiled_rules': compiled_rules,
                'events': events.get('events', []),
                'events_meta': {
                    'began_capture': events.get('began_capture'),
                    'total_perf_events': events.get('total_perf_events'),
                    'event_overflow': events.get('event_overflow'),
                },
                'stats': stats.get('rules', []),
                'flow_alignment': self._flow_alignment(logical),
            },
            'loadbalancers': lbs,
            'bonding': bonding,
            'qoe': qoe,
            'identities': {
                'destinations': list(dest_index.values()),
            },
            'fingerprints': self._compute_fingerprints(wan_paths, logical,
                                                        bonding, traffic_classes),
            'collectors': self._collector_health(),
        }

        snapshot['changes'] = self._detect_changes(snapshot['fingerprints'])
        self._prev_fingerprints = dict(snapshot['fingerprints'])
        return snapshot

    def _flow_alignment(self, logical_rules):
        """Per-logical-rule preferred vs nonpreferred flow population.

        These are a current/aging population, NOT lifetime totals (named
        plainly here to avoid implying cumulative semantics).
        """
        out = []
        for lr in logical_rules:
            pref = lr.get('preferred_flows')
            nonpref = lr.get('nonpreferred_flows')
            if pref is None and nonpref is None:
                continue
            out.append({
                'logical_name': lr.get('logical_name'),
                'trigger_priority': lr.get('trigger_priority'),
                'preferred_wan': lr.get('preferred_wan'),
                'preferred_flows': pref,
                'nonpreferred_flows': nonpref,
                'was_flow_matched': lr.get('was_flow_matched'),
            })
        return out

    def _compose_bonding(self, raw_wan_bonding, devices):
        """Normalize bonding runtime and resolve member/bond friendly names.

        Member friendly names reuse normalize.friendly_name over the devices
        map. WBOND is kept a separate forwarding object; members are NOT
        flattened away -- they are attached to their bond.
        """
        detail = normalize.wan_bonding_detail(raw_wan_bonding)
        for bond in detail.get('bonds', []):
            bond_name = bond.get('name') or ''
            # Attach a bond display name (configured name preferred).
            bond['display_name'] = (
                normalize.friendly_name_ex('wbond', {}, devices, '',
                                           bond_name))
            for m in bond.get('members', []):
                muid = m.get('member_uid')
                dev = devices.get(muid) if muid else None
                resolved_uid = muid
                if not isinstance(dev, dict) and muid:
                    # Fall back to an info.iface match (STA behavior).
                    for duid, d in devices.items():
                        if not isinstance(d, dict):
                            continue
                        if muid == (d.get('info', {}) or {}).get('iface', ''):
                            dev, resolved_uid = d, duid
                            break
                if isinstance(dev, dict):
                    m['display_name'] = normalize.friendly_name(
                        resolved_uid, dev)
                else:
                    m['display_name'] = muid
        return detail

    def _resolve_sdwan_underlays(self, devices):
        """Map each SD-WAN device to its dependent (underlay) WAN.

        STA-validated: active_dep_wandev / dep_wandevs.
        """
        out = []
        for uid, device in devices.items():
            if not isinstance(device, dict):
                continue
            info = device.get('info') if isinstance(
                device.get('info'), dict) else {}
            if str(info.get('type') or '') != 'sdwan':
                continue
            underlay_uid = normalize.sdwan_underlay_uid(device)
            underlay_name = ''
            underlay_dev = devices.get(underlay_uid)
            if isinstance(underlay_dev, dict):
                underlay_name = normalize.friendly_name(
                    underlay_uid, underlay_dev)
            out.append({
                'sdwan_uid': uid,
                'sdwan_iface': info.get('iface', ''),
                'underlay_uid': underlay_uid,
                'underlay_name': underlay_name,
            })
        return out

    def _compute_fingerprints(self, wan_paths, logical, bonding,
                              traffic_classes):
        """Build change-ready fingerprints for topology/config/policy."""
        rule_fps = {}
        for lr in logical:
            name = lr.get('logical_name') or 'rule'
            rule_fps['{}#{}'.format(name, lr.get('trigger_priority'))] = \
                lr.get('semantic_fingerprint')
        return {
            'topology': normalize.topology_fingerprint(wan_paths),
            'bonding_membership': normalize.bonding_membership_fingerprint(
                bonding.get('bonds', [])),
            'logical_rules': normalize._stable_hash(rule_fps),
            'logical_rule_map': rule_fps,
            'traffic_classes': normalize._stable_hash(
                sorted(
                    (normalize.traffic_class_fingerprint_payload(tc)
                     for tc in traffic_classes),
                    key=lambda p: (p.get('intent_id') or '',
                                   p.get('traffic_class_id') or ''))),
        }

    def _detect_changes(self, fingerprints):
        """Compare fingerprints to the previous snapshot.

        Returns lists categorized as TOPOLOGY / CONFIG / (runtime is tracked
        by value elsewhere). Compiled UUID churn alone never lands here
        because fingerprints exclude compiled UUIDs.
        """
        prev = self._prev_fingerprints or {}
        topology = []
        config = []
        if not prev:
            return {'topology': topology, 'config': config,
                    'first_snapshot': True}

        if prev.get('topology') != fingerprints.get('topology'):
            topology.append('wan_topology_changed')

        if prev.get('bonding_membership') != fingerprints.get(
                'bonding_membership'):
            config.append('bond_membership_or_config_changed')
        if prev.get('traffic_classes') != fingerprints.get('traffic_classes'):
            config.append('traffic_class_set_changed')

        prev_map = prev.get('logical_rule_map') or {}
        cur_map = fingerprints.get('logical_rule_map') or {}
        for key in cur_map:
            if key not in prev_map:
                config.append('logical_rule_added:{}'.format(key))
            elif prev_map[key] != cur_map[key]:
                config.append('logical_rule_changed:{}'.format(key))
        for key in prev_map:
            if key not in cur_map:
                config.append('logical_rule_removed:{}'.format(key))

        return {'topology': topology, 'config': config,
                'first_snapshot': False}

    def _collector_health(self):
        """Per-cadence + per-collector collection health.

        Per-collector detail (last-known-good ts, error, error ts) lets
        /api/status show exactly which optional endpoint failed while the
        rest of the snapshot stays valid.
        """
        return {
            'fast': {
                'interval': self._fast_interval,
                'last_pass_ts': self._base_raw.updated_ts,
                'collectors': self._base_raw.health(),
            },
            'slow': {
                'interval': self._slow_interval,
                'last_pass_ts': self._heavy_raw.updated_ts,
                'collectors': self._heavy_raw.health(),
            },
        }

    # --- cached read paths (web serves these; NO router I/O) --------------

    def snapshot(self):
        """Return the latest CACHED composed snapshot (may be None early).

        Web requests use this. It never triggers collection.
        """
        with self._lock:
            return self._snapshot

    def section(self, name):
        """Return one top-level section of the cached snapshot, or None."""
        snap = self.snapshot()
        if not isinstance(snap, dict):
            return None
        return snap.get(name)

    # --- back-compat helpers ---------------------------------------------

    def build_snapshot(self):
        """Back-compat: force a fresh collect + compose and return it.

        Retained for the foundation loop / any direct caller. Prefer the
        background refresh_* + snapshot() path so web never blocks on
        collection. This performs router I/O, so it must NOT be called from a
        web request handler.
        """
        self.refresh_base()
        self.refresh_heavy()
        return self.recompose()

    def record_snapshot(self):
        """Back-compat: build + store one snapshot (used by legacy loop)."""
        return self.build_snapshot()

    def cellular_detail(self, device_id):
        """Per-modem diagnostics/stats for one cellular device UID.

        Returns None for non-mdm devices. Sensitive identity/GPS fields are
        NOT collected here (SECURITY / PRIVACY): only SD-WAN-useful cellular
        metrics are surfaced by the caller; raw is filtered by the web layer.
        """
        diag = collectors.modem_diagnostics(device_id)
        stats = collectors.modem_stats(device_id)
        if diag is None and stats is None:
            return None
        return {
            'uid': device_id,
            'cellular': normalize_cellular_safe(diag),
        }

    def history(self, limit=100):
        return self._store.recent(limit=limit)

    def status(self):
        """Lightweight health/introspection payload for the dev/status page.

        Serves cached metadata only -- no router I/O.
        """
        web = None
        if self._web_port_status is not None:
            try:
                web = self._web_port_status()
            except Exception:
                web = None
        snap = self.snapshot()
        return {
            'app': 'sdwan_analyzer',
            'phase': 'backend',
            'storage': self._store.stats(),
            'web': web,
            'snapshot_ts': self._snapshot_ts,
            'collectors': self._collector_health(),
            'system_summary': (snap.get('system_summary')
                               if isinstance(snap, dict) else None),
        }


# Cellular fields safe to surface (SECURITY / PRIVACY). Excludes IMSI, ICCID,
# MDN, GPS, credentials/secrets. Only SD-WAN-useful metrics.
_SAFE_CELLULAR_KEYS = (
    'CARRID', 'SERDIS', 'HOMECARRID', 'RFBAND', 'RFCHANNEL',
    'RSRP', 'RSRQ', 'SINR', 'DBM', 'SS', 'CELLTECH',
    'PRD', 'MDL', 'DISP_CONN', 'SIMSLOT', 'CONNTEXT',
)


def normalize_cellular_safe(diagnostics):
    """Return a privacy-filtered subset of modem diagnostics.

    Only known-safe SD-WAN-relevant metrics pass through. IMEI/IMSI/ICCID/
    MDN/GPS/secrets are never included even if present in the raw object.
    """
    if not isinstance(diagnostics, dict):
        return None
    out = {}
    for k in _SAFE_CELLULAR_KEYS:
        if k in diagnostics and diagnostics.get(k) is not None:
            out[k] = diagnostics.get(k)
    return out
