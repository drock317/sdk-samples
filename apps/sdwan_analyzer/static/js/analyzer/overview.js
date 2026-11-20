/* SD-WAN Analyzer Overview: read-only cached NCOS telemetry and history.
 * Uses the established template and analyzer helpers. Never calls NCOS directly.
 * Unsupported values remain absent; no site-wide steering preference is inferred.
 */
(function (root) {
    'use strict';
    var A = root.SdwanApi, S = root.SdwanSeries, F = root.SdwanFormat, U = root.SdwanUi;
    var h = U.h;
    var COLORS = ['#1174e6', '#229cbe', '#8750dc', '#d84294', '#248861', '#e58b27'];
    var RANGES = [{v: '3600', label: '1h'}, {v: '21600', label: '6h'},
                  {v: '86400', label: '24h'}, {v: '604800', label: '7d'}];
    var MAX_CHART_SERIES = 5;
    function txt(v, fallback) { return v === null || v === undefined || v === '' ? (fallback || 'Unavailable') : String(v); }
    function number(v) { return typeof v === 'number' && isFinite(v) ? v : null; }
    function time(v) { return number(v) !== null && v > 0 ? F.dateTime(v) : 'Time unavailable'; }
    function stateLabel(v) { return v === 'connected' || v === 'up' || v === 'active' ? 'Connected' : txt(v, 'Unknown'); }
    function child(el, nodes) { (nodes || []).forEach(function (n) { el.appendChild(n); }); return el; }
    function cell(v, className) { return h('td', {class: className || '', text: txt(v, '—')}); }
    function heading(label, extra) { return h('div', {class: 'ov-heading'}, [h('span', {text: label}), extra || null]); }
    function panel(label, body, action) { return h('section', {class: 'ov-panel'}, [heading(label, action), h('div', {class: 'ov-panel-body'}, [body])]); }
    function stat(label, value, sub) { return h('div', {class: 'ov-stat'}, [
        h('p', {class: 'ov-stat-label', text: label}), h('p', {class: 'ov-stat-value', text: txt(value, '—')}),
        h('p', {class: 'ov-stat-sub', text: sub || ''})]); }
    function validArray(v) { return Array.isArray(v) ? v : []; }
    function icon(kind) { return kind === 'ethernet' ? '▣' : kind === 'cellular' ? '▥' : kind === 'wbond' ? '↗' : '◇'; }
    function maybeStatus(d) { return d && d.connection_state ? d.connection_state : 'unknown'; }

    function Overview(mount) {
        this.mount = mount; this.token = 0; this.chartToken = 0;
        this.snapshot = null; this.topology = null; this.wbond = null;
        this.range = '86400'; this.activeUid = null; this.series = [];
        this._build(); this.reload();
    }
    Overview.prototype._build = function () {
        var self = this;
        this.context = h('div', {class: 'ov-toolbar-left'});
        this.chartRange = h('select', {class: 'form-select', 'aria-label': 'Throughput time range'});
        RANGES.forEach(function (r) { self.chartRange.appendChild(h('option', {value: r.v, text: r.label})); });
        this.chartRange.value = this.range;
        this.chartRange.addEventListener('change', function () { self.range = self.chartRange.value; self.loadChart(); });
        this.refresh = U.button('Refresh', function () { self.reload(); });
        this.toolbar = h('div', {class: 'ov-toolbar'}, [this.context,
            h('div', {class: 'ov-toolbar-right'}, [this.chartRange, this.refresh])]);
        this.stats = h('div', {class: 'ov-summary'});
        this.main = h('div', {class: 'ov-row-main'});
        this.bottom = h('div', {class: 'ov-bottom'});
        this.status = h('p', {class: 'ov-muted', 'aria-live': 'polite'});
        this.mount.classList.add('sa-overview');
        child(this.mount, [this.toolbar, this.status, this.stats, this.main, this.bottom]);
    };
    Overview.prototype.reload = function () {
        var self = this, token = ++this.token;
        this.refresh.disabled = true;
        this.status.textContent = 'Loading the last saved router measurements…';
        Promise.all([
            A.getJson('api/snapshot'), A.getJson('api/analytics/topology'),
            A.getJson('api/analytics/wbond')
        ]).then(function (r) {
            if (token !== self.token) { return; }
            self.snapshot = r[0].data || {};
            self.topology = r[1].data || {};
            self.wbond = r[2].data || {};
            self._render(); self.loadChart();
            self.status.textContent = 'Read-only overview · last saved snapshot ' +
                time(self.snapshot.timestamp) + ' · No automatic router collection';
        }).catch(function (err) {
            if (token !== self.token) { return; }
            self.status.textContent = 'Overview unavailable: ' + A.describe(err);
            U.clear(self.stats); U.clear(self.main); U.clear(self.bottom);
            self.main.appendChild(U.statePanel('error', 'Could not load the overview',
                A.describe(err), U.button('Retry', function () { self.reload(); })));
        }).then(function () { if (token === self.token) { self.refresh.disabled = false; } });
    };
    Overview.prototype._render = function () {
        var snap = this.snapshot, topo = this.topology;
        var phys = validArray(topo.physical), overlays = validArray(topo.overlays), bonds = validArray(topo.bonds);
        var rules = validArray((snap.traffic_steering || {}).logical_rules);
        U.clear(this.context); U.clear(this.stats); U.clear(this.main); U.clear(this.bottom);
        child(this.context, [h('strong', {class: 'ov-context', text: 'Current NCOS router'}),
            h('span', {class: 'ov-pill good', text: 'Saved telemetry'}),
            h('span', {class: 'ov-context', text: 'Primary WAN: ' + txt((topo.primary || {}).display_name, 'Unknown')}),
            h('span', {class: 'ov-muted', text: 'Updated: ' + time(snap.timestamp)})]);
        child(this.stats, [
            stat('Physical WANs', phys.length, phys.filter(function (p) { return maybeStatus(p.observed) === 'connected'; }).length + ' connected'),
            stat('SD-WAN overlays', overlays.length, 'Reported by NCOS'),
            stat('WBOND interfaces', bonds.length, 'Runtime bond objects'),
            stat('Logical steering rules', rules.length, 'Defined in latest snapshot'),
            stat('Last observed', snap.timestamp ? F.timeOnly(snap.timestamp) : null, 'Router snapshot time')
        ]);
        var side = h('div', {class: 'ov-side'}, [this._steering(rules), this._bond(bonds)]);
        child(this.main, [this._topology(topo, phys, bonds), side]);
        child(this.bottom, [this._paths(phys, overlays, snap.swans), this._events(snap), this._throughputPanel()]);
    };
    Overview.prototype._topology = function (topo, phys, bonds) {
        var self = this;
        var container = h('div');
        var flow = h('div', {class: 'ov-topology'});
        flow.appendChild(h('div', {class: 'ov-node'}, [h('strong', {text: 'NCOS Router'}),
            h('span', {class: 'ov-muted', text: 'Current device'})]));
        var first = bonds[0];
        if (first) {
            flow.appendChild(h('span', {class: 'ov-divider-arrow', 'aria-hidden': 'true', text: '→'}));
            flow.appendChild(h('div', {class: 'ov-node ov-bond'}, [h('strong', {text: txt(first.display_name, 'Bonded WAN')}),
                h('span', {class: 'ov-muted', text: txt((first.observed || {}).link_state, 'Unknown') +
                    ' · ' + validArray(first.members).length + ' listed members'})]));
        }
        flow.appendChild(h('span', {class: 'ov-divider-arrow', 'aria-hidden': 'true', text: '→'}));
        var members = h('div', {class: 'ov-member-list'}), tooltip = h('div', {class: 'ov-detail', hidden: true});
        var visible = first ? validArray(first.members) : phys;
        if (!visible.length) { members.appendChild(h('p', {class: 'ov-muted', text: 'No WAN paths reported.'})); }
        visible.forEach(function (m) {
            var ref = first ? (m.physical || {}) : {state: 'resolved', uid: m.uid};
            var path = phys.filter(function (p) { return p.uid === ref.uid; })[0];
            var name = path ? txt(path.display_name, path.uid) : 'Unresolved physical WAN';
            var detail = first ? ('Bond tunnel: ' + txt(m.tunnel_iface, 'Not reported') + '\nPhysical member: ' +
                txt(m.reported_intf_name, 'Not reported') + '\nResolved WAN: ' +
                txt(ref.state === 'resolved' ? ref.uid : null, 'Unresolved') + '\nRelationship: ' +
                txt(m.underlay_consistency, 'Unknown') + '\nSIM placement: Not available in the current topology API') :
                ('Interface: ' + txt(m.uid) + '\nType: ' + txt(m.kind));
            var btn = h('button', {type: 'button', class: 'ov-iconbtn', text: 'i',
                'aria-label': 'Details for ' + name, 'aria-expanded': 'false'});
            btn.addEventListener('click', function () {
                var open = tooltip.hidden || tooltip.textContent !== detail;
                tooltip.hidden = !open; tooltip.textContent = open ? detail : '';
                members.querySelectorAll('button').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
                btn.setAttribute('aria-expanded', String(open));
            });
            members.appendChild(h('div', {class: 'ov-node ov-member'}, [
                h('div', {class: 'ov-member-info'}, [h('span', {class: 'ov-member-name', text: icon(path && path.kind) + '  ' + name}),
                    h('span', {class: 'ov-muted', text: first ? 'WBOND member · ' + txt((m.observed || {}).link_attach_state, 'state unknown') : stateLabel(m.observed && m.observed.connection_state)})]), btn]));
        });
        flow.appendChild(members); container.appendChild(flow); container.appendChild(tooltip);
        if (validArray(topo.unresolved).length) {
            container.appendChild(h('p', {class: 'ov-muted', text: validArray(topo.unresolved).length +
                ' topology associations are unresolved; only confirmed member identities are labelled.'}));
        } else {
            container.appendChild(h('p', {class: 'ov-muted', text: 'Relationships reflect NCOS-reported runtime state; attachment alone does not establish traffic carriage.'}));
        }
        return panel('Topology & Relationship', container);
    };
    Overview.prototype._steering = function (rules) {
        var rows = [], body = h('div');
        rules.forEach(function (r) {
            var p = number(r.preferred_flows), n = number(r.nonpreferred_flows);
            if (p === null && n === null) { return; }
            rows.push({name: txt(r.logical_name, 'Unnamed rule'), value: (p || 0) + (n || 0), p: p, n: n});
        });
        rows.sort(function (a, b) { return b.value - a.value; });
        var max = rows.length ? rows[0].value : 0;
        rows.slice(0, 5).forEach(function (r) {
            var meter = h('div', {class: 'ov-flow-meter'}, [h('span', {style: 'width:' +
                (max ? Math.round(r.value / max * 100) : 0) + '%;'})]);
            body.appendChild(h('div', {class: 'ov-flow'}, [h('span', {text: r.name}), meter,
                h('span', {text: r.value.toLocaleString() + ' flows', title: 'Preferred: ' +
                    txt(r.p, 'unknown') + ' · Nonpreferred: ' + txt(r.n, 'unknown')})]));
        });
        if (!rows.length) { body.appendChild(h('p', {class: 'ov-muted', text: 'No valid per-rule flow populations reported.'})); }
        body.appendChild(h('p', {class: 'ov-muted', text: 'Current/aging NCOS flow populations, not bandwidth or 24-hour totals.'}));
        return panel('Most Active Steering Rules', body);
    };
    Overview.prototype._bond = function (bonds) {
        var b = (this.wbond.bonds || [])[0] || bonds[0] || {};
        var ob = b.observed || {};
        var members = validArray(b.members);
        function mini(label, value) { return h('div', [h('strong', {text: txt(value, '—')}), h('small', {text: label})]); }
        var values = [mini('Listed members', ob.listed_member_count === undefined ? members.length || null : ob.listed_member_count),
            mini('Download flows', ob.total_download_flows === null ? null : ob.total_download_flows),
            mini('Upload flows', ob.total_upload_flows === null ? null : ob.total_upload_flows)];
        var content = h('div', [h('div', {class: 'ov-bond-grid'}, values),
            h('p', {class: 'ov-muted', text: 'Mode: ' + txt(ob.bonding_mode, 'Unknown') +
                ' · Link: ' + txt(ob.link_state, 'Unknown')})]);
        if (members.length) {
            var observed = members.map(function (m) { return m.observed || {}; });
            var weight = observed.map(function (o) { return number(o.current_download_weight); });
            content.appendChild(h('p', {class: 'ov-muted', text: 'Router-reported DL weights: ' +
                weight.map(function (w) { return w === null ? '—' : String(w); }).join(' : ') +
                ' (not traffic percentages)'}));
        }
        return panel('WBOND Snapshot', content);
    };
    Overview.prototype._paths = function (phys, overlays, swans) {
        var swanById = {};
        validArray(swans && swans.paths).forEach(function (p) { swanById[p.uid] = p; });
        var table = h('table', {class: 'ov-table'});
        var head = h('tr');
        ['Path', 'State', 'SWANS latency', 'SWANS jitter', 'Measurement'].forEach(function (t) { head.appendChild(h('th', {text: t})); });
        table.appendChild(h('thead', {}, [head]));
        var body = h('tbody');
        phys.forEach(function (p) {
            var assoc = overlays.filter(function (o) { return (o.underlay || {}).state === 'resolved' && (o.underlay || {}).uid === p.uid; });
            var ms = assoc.map(function (o) { return swanById[o.uid]; }).filter(Boolean);
            var q = ms.length === 1 ? ms[0] : null;
            var values = [cell(p.display_name || p.uid), cell(stateLabel((p.observed || {}).connection_state)),
                cell(q && number(q.latency_ms) !== null ? q.latency_ms + ' ms' : '—'),
                cell(q && number(q.jitter_ms) !== null ? q.jitter_ms + ' ms' : '—'),
                cell(q ? time(q.epoch) : ms.length > 1 ? 'Multiple overlay samples' : 'Not available')];
            body.appendChild(h('tr', {}, values));
        });
        if (!phys.length) { body.appendChild(h('tr', {}, [h('td', {colspan: '5', text: 'No physical WANs reported.'})])); }
        table.appendChild(body);
        return panel('WAN / Path Status', h('div', {class: 'ov-table-wrap'}, [table,
            h('p', {class: 'ov-muted', text: 'SWANS metrics are shown only when exactly one measured overlay resolves to the physical WAN.'})]));
    };
    Overview.prototype._events = function (snap) {
        var events = validArray((snap.traffic_steering || {}).events).filter(function (e) { return number(e.epoch) !== null; });
        events.sort(function (a, b) { return b.epoch - a.epoch; });
        var table = h('table', {class: 'ov-table'}), header = h('tr');
        ['Time', 'Rule', 'Observed change'].forEach(function (v) { header.appendChild(h('th', {text: v})); });
        table.appendChild(h('thead', {}, [header]));
        var body = h('tbody');
        events.slice(0, 5).forEach(function (e) {
            var change = (e.wan_from || e.wan_to) ? txt(e.wan_from, '?') + ' → ' + txt(e.wan_to, '?') :
                'Reason: ' + txt(e.reason, 'Not reported');
            body.appendChild(h('tr', {}, [cell(time(e.epoch)), cell(e.steering_rule), cell(change, 'ov-wrap')]));
        });
        if (!events.length) { body.appendChild(h('tr', {}, [h('td', {colspan: '3', text: 'No timestamped steering events in the latest snapshot.'})])); }
        table.appendChild(body);
        return panel('Recent Changes & Events', h('div', {class: 'ov-table-wrap'}, [table,
            h('p', {class: 'ov-muted', text: 'Latest router-reported steering event cache. Historical change categories will be added once a unified event feed is verified.'})]));
    };
    Overview.prototype._throughputPanel = function () {
        var self = this;
        this.chartHost = h('div', {class: 'ov-chart-host', 'aria-live': 'polite'});
        this.chartLegend = h('div', {class: 'ov-legend'});
        var content = h('div', [h('div', {class: 'ov-chart-layout'}, [this.chartHost, this.chartLegend]),
            h('p', {class: 'ov-chart-foot', text: 'RX throughput by interface (bps). Focus a line using its checkbox. Different layers of traffic are not additive.'})]);
        return panel('Key Telemetry — Interface Throughput', content);
    };
    Overview.prototype._chartInterfaces = function () {
        var topo = this.topology || {}, all = [], seen = {};
        var primary = topo.primary && topo.primary.state === 'known' ? topo.primary.uid : null;
        function add(uid, label, cls) {
            if (!uid || seen[uid] || all.length >= MAX_CHART_SERIES) { return; }
            seen[uid] = true; all.push({uid: uid, label: txt(label, uid), cls: cls});
        }
        var bonds = validArray(topo.bonds);
        var bond = bonds.filter(function (b) { return b.wbond_device && b.wbond_device.uid === primary; })[0];
        if (bond) {
            add(primary, bond.display_name || 'Bonded WAN', 'wbond');
            var phys = validArray(topo.physical);
            validArray(bond.members).forEach(function (m) {
                var ref = m.physical || {};
                if (ref.state !== 'resolved') { return; }
                var p = phys.filter(function (x) { return x.uid === ref.uid; })[0];
                if (p) { add(p.uid, p.display_name, 'physical'); }
            });
        } else {
            var allPaths = validArray(topo.physical);
            var primaryPath = validArray((this.snapshot.wan || {}).paths).filter(function (p) {
                return p.uid === primary && (p.object_class === 'physical' || p.object_class === 'sdwan');
            })[0];
            if (primaryPath) { add(primaryPath.uid, primaryPath.display_name, primaryPath.object_class); }
            allPaths.forEach(function (p) { add(p.uid, p.display_name, 'physical'); });
        }
        // If the recorded primary is unknown, fall back to the first observed path without claiming it is primary.
        if (!all.length) { validArray(topo.physical).forEach(function (p) { add(p.uid, p.display_name, 'physical'); }); }
        return all;
    };
    Overview.prototype.loadChart = function () {
        var self = this, token = ++this.chartToken;
        if (!this.snapshot || !this.chartHost) { return; }
        var choices = this._chartInterfaces();
        U.clear(this.chartHost); U.clear(this.chartLegend);
        if (!choices.length) { this.chartHost.appendChild(h('p', {class: 'ov-loading', text: 'No interfaces available for throughput history.'})); return; }
        var previous = this.activeUid;
        if (!choices.some(function (i) { return i.uid === previous; })) { this.activeUid = choices[0].uid; }
        this.chartHost.appendChild(h('p', {class: 'ov-loading', text: 'Loading stored throughput history…'}));
        var now = number(this.snapshot.timestamp) || (Date.now() / 1000);
        var from = now - Number(this.range);
        // Bounded per-interface reads. A failure on a single path does not erase the rest.
        Promise.all(choices.map(function (i) {
            return A.fetchRates(i.uid, from).then(function (r) {
                return {uid: i.uid, label: i.label, intervals: S.buildIntervals(r.rates).intervals, truncated: r.truncated};
            }).catch(function (err) { return {uid: i.uid, label: i.label, intervals: [], error: A.describe(err)}; });
        })).then(function (results) {
            if (token !== self.chartToken) { return; }
            self.series = results;
            self._drawChart(from, now);
        });
    };
    Overview.prototype._drawChart = function (from, to) {
        var self = this, series = this.series;
        U.clear(this.chartHost); U.clear(this.chartLegend);
        var max = 0;
        series.forEach(function (s) { s.intervals.forEach(function (iv) { if (number(iv.rx) !== null) { max = Math.max(max, iv.rx); } }); });
        var axis = F.niceAxis(max, 4), unit = F.pickUnit(axis.max);
        var W = 760, H = 240, L = 72, R = 14, T = 10, B = 34;
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
        svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Historical RX throughput by interface');
        function node(tag, attrs, textValue) {
            var e = document.createElementNS('http://www.w3.org/2000/svg', tag);
            Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
            if (textValue !== undefined) { e.textContent = textValue; }
            svg.appendChild(e); return e;
        }
        function x(t) { return L + (t - from) / Math.max(1, to - from) * (W - L - R); }
        function y(v) { return T + (1 - v / Math.max(1, axis.max)) * (H - T - B); }
        axis.ticks.forEach(function (v) {
            node('line', {x1:L, x2:W-R, y1:y(v), y2:y(v), stroke:'var(--sa-grid)', 'stroke-width':1});
            node('text', {x:L-8, y:y(v)+4, 'text-anchor':'end', fill:'var(--text-primary)', 'font-size':11}, F.tick(v/unit.div));
        });
        node('text', {x:9, y:14, fill:'var(--text-primary)', 'font-size':11}, unit.name);
        var ticks = F.timeTicks(from,to,6);
        ticks.ticks.forEach(function (t) {
            node('text',{x:x(t), y:H-8, 'text-anchor':'middle', fill:'var(--text-primary)', 'font-size':11},
                 F.timeTickLabel(t,ticks.step,to-from));
        });
        series.forEach(function (s, i) {
            var color = COLORS[i % COLORS.length], focused = s.uid === self.activeUid;
            S.runs(s.intervals,'rx').forEach(function (run) {
                if (!run.length) { return; }
                var d = run.map(function (p,j) { return (j ? 'L' : 'M') + x(p.t).toFixed(2) + ',' + y(p.v).toFixed(2); }).join(' ');
                node('path', {d:d, fill:'none', stroke:color, 'stroke-width':focused?2.7:1.7,
                    opacity:focused?1:0.25, 'stroke-linecap':'round', 'stroke-linejoin':'round'});
            });
            var box = h('input',{type:'radio', name:'ov-focused-interface', value:s.uid});
            box.checked = focused;
            box.addEventListener('change', function () { self.activeUid = s.uid; self._drawChart(from,to); });
            self.chartLegend.appendChild(h('label', {}, [box, h('span', {class:'ov-linekey', style:'border-color:'+color}),
                h('span', {text:s.label + (s.truncated?' (partial)':'')})]));
        });
        this.chartHost.appendChild(svg);
        if (series.every(function (s) { return !S.hasAnyValue(s.intervals); })) {
            this.chartHost.appendChild(h('p',{class:'ov-muted',text:'No valid RX intervals were reported in the selected time window.'}));
        }
        var failed = series.filter(function (s) { return s.error; });
        if (failed.length) { this.chartHost.appendChild(h('p',{class:'ov-muted',text:failed.length+' interface history request(s) failed. Others remain visible.'})); }
    };
    root.SdwanOverview = Overview;
}(window));