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
    function clock12(ts, includeYear) {
        if (number(ts) === null || ts <= 0) { return 'Unavailable'; }
        var opts = {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true};
        if (includeYear) { opts.year = 'numeric'; }
        return new Date(ts * 1000).toLocaleString('en-US', opts);
    }
    function countText(meta) {
        if (!meta || number(meta.observations_today) === null) { return 'Today’s observations: unavailable'; }
        return 'Today’s observations: ' + (meta.count_complete ? '' : 'at least ') +
            meta.observations_today.toLocaleString('en-US') + (meta.count_complete ? '' : ' (partial count)');
    }
    function stateLabel(v) { return v === 'connected' || v === 'up' || v === 'active' ? 'Connected' : txt(v, 'Unknown'); }
    function child(el, nodes) { (nodes || []).forEach(function (n) { el.appendChild(n); }); return el; }
    function cell(v, className) { return h('td', {class: className || '', text: txt(v, '—')}); }
    function heading(label, extra) { return h('div', {class: 'ov-heading'}, [h('span', {text: label}), extra || null]); }
    function panel(label, body, action) { return h('section', {class: 'ov-panel'}, [heading(label, action), h('div', {class: 'ov-panel-body'}, [body])]); }
    function stat(label, value, sub, glyph) { return h('div', {class: 'ov-stat'}, [
        h('div', {class: 'ov-stat-head'}, [
            h('span', {class: 'ov-stat-icon', 'aria-hidden': 'true'}, [h('i', {class: 'fas fa-' + glyph})]),
            h('p', {class: 'ov-stat-label', text: label})]),
        h('p', {class: 'ov-stat-value', text: txt(value, '—')}),
        h('p', {class: 'ov-stat-sub'}, Array.isArray(sub) ? sub : [document.createTextNode(sub || '')])]); }
    function coloredCount(n, label, cls) { return h('span', {class: 'ov-count-' + cls, text: n + ' ' + label}); }
    function storageKey(snap) {
        var name = (snap.system_summary || {}).router_name || 'unknown';
        return 'sdwan_analyzer_overview_hidden_v1:' + encodeURIComponent(name);
    }
    function safeHidden(key) {
        try { var raw = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(raw) ? raw.filter(function (x) { return typeof x === 'string'; }) : []; }
        catch (_) { return []; }
    }
    function makePopover(title, rows, note) {
        var content = h('div', {class: 'ov-popover', role: 'region', 'aria-label': title, hidden: true},
            [h('strong', {class: 'ov-popover-title', text: title})]);
        rows.forEach(function (r) {
            if (r[1] !== null && r[1] !== undefined && r[1] !== '') {
                content.appendChild(h('div', {class: 'ov-popover-row'}, [
                    h('span', {text: r[0]}), h('code', {text: String(r[1])})]));
            }
        });
        if (note) { content.appendChild(h('p', {class: 'ov-muted', text: note})); }
        return content;
    }
    function attachPopover(btn, popup) {
        btn.setAttribute('aria-expanded', 'false');
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            var opening = popup.hidden;
            document.querySelectorAll('.ov-popover:not([hidden])').forEach(function (p) {
                p.hidden = true;
                var toggle = p.parentNode.querySelector('.ov-iconbtn');
                if (toggle) { toggle.setAttribute('aria-expanded', 'false'); }
            });
            popup.hidden = !opening;
            btn.setAttribute('aria-expanded', String(opening));
        });
        btn.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { popup.hidden = true; btn.setAttribute('aria-expanded', 'false'); btn.focus(); }
        });
    }
    document.addEventListener('click', function (e) {
        document.querySelectorAll('.ov-popover:not([hidden])').forEach(function (p) {
            if (p.contains(e.target)) { return; }
            p.hidden = true;
            var toggle = p.parentNode.querySelector('.ov-iconbtn');
            if (toggle) { toggle.setAttribute('aria-expanded', 'false'); }
        });
    });
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') { return; }
        document.querySelectorAll('.ov-popover:not([hidden])').forEach(function (p) {
            p.hidden = true;
            var toggle = p.parentNode.querySelector('.ov-iconbtn');
            if (toggle) { toggle.setAttribute('aria-expanded', 'false'); toggle.focus(); }
        });
    });
    function bondHealth(b) {
        var ob = b.observed || {}, state = String(ob.link_state || '').toLowerCase();
        var members = validArray(b.members);
        if (['down', 'failed', 'error', 'disconnected'].indexOf(state) !== -1) {
            return {kind: 'down', text: 'Down', reason: 'NCOS reports bond link state: ' + ob.link_state};
        }
        if (state !== 'up' && state !== 'active' && state !== 'connected') {
            return {kind: 'unknown', text: txt(ob.link_state, 'Unknown'), reason: 'Bond state is not verified'};
        }
        var affected = members.filter(function (m) {
            var o = m.observed || {}, a = String(o.link_attach_state || '').toLowerCase();
            var link = String(o.link_state || '').toLowerCase();
            return (a && a !== 'attached') || ['down','failed','disconnected'].indexOf(link) !== -1 ||
                o.has_loss === true;
        });
        if (affected.length) {
            return {kind: 'degraded', text: 'Degraded',
                reason: affected.length + ' member(s) report attachment, link or loss issues',
                affected: affected};
        }
        if (!members.length || members.some(function (m) { return !((m.observed || {}).link_attach_state); })) {
            return {kind: 'unknown', text: 'State unverified', reason: 'Member attachment state is not fully reported'};
        }
        return {kind: 'active', text: 'Active', reason: null};
    }
    function validArray(v) { return Array.isArray(v) ? v : []; }
    function icon(kind) { return kind === 'ethernet' ? '▣' : kind === 'cellular' ? '▥' : kind === 'wbond' ? '↗' : '◇'; }
    function maybeStatus(d) { return d && d.connection_state ? d.connection_state : 'unknown'; }
    /* Identity helpers: every name, SIM and failure fact comes from the shared backend
     * WAN identity layer (analyzer/wan_identity.py); nothing is classified here. */
    function simPlacement(p) {
        var id = p && p.identity;
        if (!id || !id.sim_presence || id.sim_presence === 'not_applicable') { return 'Not applicable'; }
        var slot = id.sim_type === 'esim' ? 'eSIM' : txt(id.sim_slot, 'slot not reported');
        var pres = id.sim_presence === 'empty' ? 'empty' : id.sim_presence === 'present' ? 'present' : 'presence unknown';
        return slot + ' (' + pres + ')';
    }
    function pathState(p) {
        var o = p.observed || {}, s = stateLabel(o.connection_state);
        return o.operational_state === 'inactive' && o.failure_reason ? s + ' — ' + o.failure_reason : s;
    }
    function wanRef(identity, raw) { return identity && identity.resolved && identity.display_name ? identity.display_name : raw; }

    function Overview(mount) {
        this.mount = mount; this.token = 0; this.chartToken = 0;
        this.snapshot = null; this.topology = null; this.wbond = null; this.dayMeta = null;
        this.range = '86400'; this.activeUid = null; this.series = [];
        this.hiddenKey = null; this.hiddenUids = [];
        this._build(); this.reload();
    }
    Overview.prototype._build = function () {
        var self = this;
        this.context = h('div', {class: 'ov-toolbar-left'});
        this.rangeLabel = h('div', {class: 'ov-range-window', 'aria-live': 'polite'});
        this.chartRange = h('select', {class: 'form-select', 'aria-label': 'Throughput time range'});
        RANGES.forEach(function (r) { self.chartRange.appendChild(h('option', {value: r.v, text: r.label})); });
        this.chartRange.value = this.range;
        this.chartRange.addEventListener('change', function () { self.range = self.chartRange.value; self._updateTimeWindow(); self._renderEvents(); self.loadChart(); });
        this.refresh = U.button('Refresh', function () { self.reload(); });
        this.toolbar = h('div', {class: 'ov-toolbar'}, [this.context,
            h('div', {class: 'ov-toolbar-controls'}, [
                h('div', {class: 'ov-toolbar-right'}, [
                    h('label', {class: 'ov-range-label', text: 'Time range', for: 'ov-time-range'}),
                    this.chartRange, this.refresh]), this.rangeLabel])]);
        this.chartRange.id = 'ov-time-range';
        this.stats = h('div', {class: 'ov-summary'});
        this.main = h('div', {class: 'ov-row-main'});
        this.bottom = h('div', {class: 'ov-bottom'});
        this.status = h('p', {class: 'ov-error', 'aria-live': 'polite', hidden: true});
        this.mount.classList.add('sa-overview');
        child(this.mount, [this.toolbar, this.status, this.stats, this.main, this.bottom]);
    };
    Overview.prototype.reload = function () {
        var self = this, token = ++this.token;
        this.refresh.disabled = true;
        this.status.hidden = true;
        this.status.textContent = '';
        Promise.all([
            A.getJson('api/snapshot'), A.getJson('api/analytics/topology'),
            A.getJson('api/analytics/wbond'),
            A.getJson('api/analytics/overview_meta').catch(function () { return {data: null}; })
        ]).then(function (r) {
            if (token !== self.token) { return; }
            self.snapshot = r[0].data || {};
            self.topology = r[1].data || {};
            self.wbond = r[2].data || {};
            self.dayMeta = r[3].data;
            var key = storageKey(self.snapshot);
            if (self.hiddenKey !== key) { self.hiddenKey = key; self.hiddenUids = safeHidden(key); }
            self._render(); self._updateTimeWindow(); self.loadChart();
        }).catch(function (err) {
            if (token !== self.token) { return; }
            self.status.hidden = false;
            self.status.textContent = 'Overview unavailable: ' + A.describe(err);
            U.clear(self.stats); U.clear(self.main); U.clear(self.bottom);
            self.main.appendChild(U.statePanel('error', 'Could not load the overview',
                A.describe(err), U.button('Retry', function () { self.reload(); })));
        }).then(function () { if (token === self.token) { self.refresh.disabled = false; } });
    };
    Overview.prototype._visiblePhysical = function () {
        var hidden = this.hiddenUids || [];
        return validArray((this.topology || {}).physical).filter(function (p) {
            return hidden.indexOf(p.uid) === -1;
        });
    };
    Overview.prototype._setWanVisible = function (uid, visible) {
        if (!uid) { return; }
        this.hiddenUids = this.hiddenUids.filter(function (x) { return x !== uid; });
        if (!visible) { this.hiddenUids.push(uid); }
        try { localStorage.setItem(this.hiddenKey, JSON.stringify(this.hiddenUids)); } catch (_) {}
        this._render(); this.loadChart();
    };
    Overview.prototype._render = function () {
        var snap = this.snapshot, topo = this.topology, phys = this._visiblePhysical();
        var overlays = validArray(topo.overlays), bonds = validArray(topo.bonds);
        var rules = validArray((snap.traffic_steering || {}).logical_rules);
        var connected = phys.filter(function (p) { return maybeStatus(p.observed) === 'connected'; }).length;
        var inactive = phys.filter(function (p) { return (p.observed || {}).operational_state === 'inactive'; }).length;
        U.clear(this.context); U.clear(this.stats); U.clear(this.main); U.clear(this.bottom);
        child(this.context, [h('strong', {class: 'ov-context ov-router-name',
            text: txt((snap.system_summary || {}).router_name, 'Router name unavailable')}),
            h('span', {class: 'ov-context', text: 'Primary WAN: ' + txt((topo.primary || {}).display_name, 'Unknown')})]);
        var counts = [coloredCount(connected, 'connected', 'good')];
        if (inactive) { counts.push(document.createTextNode(' · '), coloredCount(inactive, 'inactive', 'warn')); }
        if (this.hiddenUids.length) { counts.push(document.createTextNode(' · ' + this.hiddenUids.length + ' hidden')); }
        child(this.stats, [
            stat('Physical WANs', phys.length, counts, 'broadcast-tower'),
            stat('SD-WAN overlays', overlays.length, 'Reported by NCOS', 'layer-group'),
            stat('WBOND interfaces', bonds.length, 'Runtime bond objects', 'link'),
            stat('Logical steering rules', rules.length, 'Latest collected snapshot', 'project-diagram'),
            stat('Last observed', clock12(snap.timestamp, true), countText(this.dayMeta), 'clock')
        ]);
        var side = h('div', {class: 'ov-side'}, [this._steering(rules), this._bond(bonds)]);
        child(this.main, [this._topology(topo, phys, bonds), side]);
        child(this.bottom, [this._paths(phys, overlays, snap.swans), this._events(snap), this._throughputPanel()]);
    };
    Overview.prototype._renderEvents = function () {
        if (!this.snapshot || !this.eventsHost) { return; }
        U.clear(this.eventsHost);
        this.eventsHost.appendChild(this._eventsTable(this.snapshot));
    };
    Overview.prototype._updateTimeWindow = function () {
        var end = number(this.snapshot && this.snapshot.timestamp);
        if (end === null || end <= 0) { this.rangeLabel.textContent = 'Time window unavailable'; return; }
        var start = end - Number(this.range);
        this.rangeLabel.textContent = 'Selected window: ' + clock12(start, true) + ' – ' +
            clock12(end, true) + ' (' + F.timeZoneName() + '; ends at last observation)';
    };
    Overview.prototype._topology = function (topo, phys, bonds) {
        var container = h('div'), flow = h('div', {class: 'ov-topology'});
        var model = ((this.snapshot || {}).wan || {}).identity || {};
        var router = h('div', {class: 'ov-node ov-router'}, [
            h('span', {class: 'ov-topology-glyph', 'aria-hidden':'true'}, [h('i', {class:'fas fa-server'})]),
            h('div', {}, [h('strong', {text:'NCOS Router'}),
                h('span', {class:'ov-muted', text:txt(model.host_model, 'Router model unavailable')})])]);
        flow.appendChild(router);
        var first = bonds[0];
        if (first) {
            var health = bondHealth(first);
            var bondNode = h('div', {class: 'ov-node ov-bond ov-bond-' + health.kind}, [
                h('span', {class:'ov-topology-glyph', 'aria-hidden':'true'}, [h('i', {class:'fas fa-link'})]),
                h('div', {}, [
                    h('strong', {text:txt(first.display_name, 'Bonded WAN')}),
                    h('span', {class:'ov-health ov-health-' + health.kind, text:health.text})])]);
            if (health.kind === 'degraded' || health.kind === 'down') {
                var wrap = h('span', {class:'ov-popover-wrap'});
                var b = h('button', {class:'ov-iconbtn', type:'button', text:'i',
                    'aria-label':'Why is WBOND ' + health.text + '?'});
                var rows = [['Condition',health.reason]];
                (health.affected || []).forEach(function (m) {
                    var ref = m.physical || {}, p = validArray(topo.physical).concat(validArray(topo.hidden_physical))
                        .filter(function (x) {return x.uid === ref.uid;})[0];
                    rows.push(['Affected member', p ? p.display_name : txt(m.reported_intf_name,'Unresolved interface')]);
                    rows.push(['Reported attachment', (m.observed || {}).link_attach_state]);
                    rows.push(['Reported link', (m.observed || {}).link_state]);
                    if ((m.observed || {}).has_loss === true) { rows.push(['Member loss', 'Reported by NCOS']); }
                });
                var pop = makePopover('WBOND ' + health.text, rows,
                    'Based on NCOS-reported link and member attachment state.');
                wrap.appendChild(b); wrap.appendChild(pop); bondNode.appendChild(wrap);
                attachPopover(b,pop);
            }
            flow.appendChild(h('span', {class:'ov-link-line', 'aria-hidden':'true'}));
            flow.appendChild(bondNode);
        }
        var members = h('div', {class:'ov-member-list'});
        var visible = first ? validArray(first.members).filter(function (m) {
            return !m.physical || (this.hiddenUids || []).indexOf(m.physical.uid) === -1;
        }, this) : phys;
        if (!visible.length) { members.appendChild(h('p', {class:'ov-muted', text:'No displayable WAN members.'})); }
        visible.forEach(function (m) {
            var ref = first ? m.physical || {} : {state:'resolved',uid:m.uid};
            var path = phys.filter(function (p) { return p.uid === ref.uid; })[0] ||
                validArray(topo.hidden_physical).filter(function (p) { return p.uid === ref.uid; })[0];
            var name = path ? txt(path.display_name, path.uid) : 'Unresolved physical WAN';
            var observed = first ? m.observed || {} : path && path.observed || {};
            var attached = String(observed.link_attach_state || '').toLowerCase() === 'attached';
            var good = first ? attached : maybeStatus(observed) === 'connected';
            var state = first ? txt(observed.link_attach_state,'Unknown') : pathState(path || m);
            var overlay = m.overlay && m.overlay.state === 'resolved' ?
                validArray(topo.overlays).filter(function (o) {return o.uid === m.overlay.uid;})[0] : null;
            var sdwanTunnel = overlay && overlay.iface && overlay.iface !== m.tunnel_iface ? overlay.iface : null;
            var rows = first ? [['Bond tunnel',m.tunnel_iface],
                ['SD-WAN tunnel',sdwanTunnel],
                ['Physical interface',m.reported_intf_name]] :
                [['Physical interface',m.uid]];
            var wrap = h('span', {class:'ov-popover-wrap'});
            var btn = h('button', {type:'button',class:'ov-iconbtn', text:'i','aria-label':'Details for '+name});
            var pop = makePopover(name, rows);
            wrap.appendChild(btn); wrap.appendChild(pop); attachPopover(btn,pop);
            members.appendChild(h('div', {class:'ov-node ov-member'}, [
                h('span', {class:'ov-topology-glyph', 'aria-hidden':'true'},
                    [h('i', {class:'fas fa-'+ (path && path.kind === 'ethernet' ? 'ethernet' : 'signal')})]),
                h('div', {class:'ov-member-info'}, [
                    h('span', {class:'ov-member-name', text:name}),
                    h('span', {class:'ov-health ov-health-'+(good?'active':'unknown'), text:state})]),
                wrap]));
        });
        if (first) { flow.appendChild(h('span', {class:'ov-link-line', 'aria-hidden':'true'})); }
        flow.appendChild(members); container.appendChild(flow);
        if (validArray(topo.unresolved).length) {
            container.appendChild(h('p', {class:'ov-muted', text:validArray(topo.unresolved).length+
                ' unresolved topology association(s).'}));
        }
        return panel('Topology & Relationship',container);
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
        var self = this, swanById = {};
        validArray(swans && swans.paths).forEach(function (p) { swanById[p.uid] = p; });
        var table = h('table', {class:'ov-table'}), head = h('tr');
        ['Path','State','SWANS latency','SWANS jitter','Measurement','Overview'].forEach(function (t) {
            head.appendChild(h('th', {text:t}));
        });
        table.appendChild(h('thead',{},[head]));
        var body = h('tbody');
        var all = validArray((this.topology || {}).physical);
        all.forEach(function (p) {
            var assoc = overlays.filter(function (o) {return (o.underlay || {}).state === 'resolved' && (o.underlay || {}).uid === p.uid;});
            var ms = assoc.map(function (o) {return swanById[o.uid];}).filter(Boolean);
            var q = ms.length === 1 ? ms[0] : null, shown = self.hiddenUids.indexOf(p.uid) === -1;
            var checkbox = h('input', {type:'checkbox','aria-label':'Show '+p.display_name+' on Overview'});
            checkbox.checked = shown;
            checkbox.addEventListener('change', function () {self._setWanVisible(p.uid,checkbox.checked);});
            var values = [cell(p.display_name || p.uid),cell(pathState(p),'ov-wrap'),
                cell(q && number(q.latency_ms) !== null ? q.latency_ms+' ms' : '—'),
                cell(q && number(q.jitter_ms) !== null ? q.jitter_ms+' ms' : '—'),
                cell(q ? time(q.epoch) : ms.length > 1 ? 'Multiple overlay samples' : 'Not available'),
                h('td',{},[h('label',{class:'ov-visibility-toggle'},[checkbox,h('span',{text:shown?'Shown':'Hidden'})])])];
            body.appendChild(h('tr', {class:shown?'':'ov-hidden-wan'}, values));
        });
        if (!all.length) {body.appendChild(h('tr',{},[h('td',{colspan:'6',text:'No physical WANs reported.'})]));}
        table.appendChild(body);
        return panel('WAN / Path Status', h('div',{class:'ov-table-wrap'},[table,
            h('p',{class:'ov-muted',text:'Overview visibility is local to this browser and router; hidden interfaces remain available to telemetry and analysis. SWANS metrics are displayed only for unambiguous overlay associations.'})]));
    };
    Overview.prototype._events = function (snap) {
        this.eventsHost = h('div');
        this.eventsHost.appendChild(this._eventsTable(snap));
        return panel('Recent Changes & Events',this.eventsHost);
    };
    Overview.prototype._eventsTable = function (snap) {
        var end = number(snap.timestamp), start = end - Number(this.range);
        var events = validArray((snap.traffic_steering || {}).events).filter(function (e) {
            return number(e.epoch) !== null && end !== null && e.epoch >= start && e.epoch <= end;
        }).sort(function (a,b) {return b.epoch - a.epoch;});
        var table = h('table',{class:'ov-table'}), header = h('tr');
        ['Time','Rule','Observed change'].forEach(function (v) {header.appendChild(h('th',{text:v}));});
        table.appendChild(h('thead',{},[header]));
        var body = h('tbody');
        events.slice(0,5).forEach(function (e) {
            var from = wanRef(e.wan_from_identity,e.wan_from);
            var to = wanRef(e.wan_to_identity,e.wan_to);
            var change = (e.wan_from || e.wan_to) ?
                txt(from,'Previous path not reported')+' → '+txt(to,'Destination not reported') :
                'Destination not reported';
            var row = h('tr',{},[cell(time(e.epoch)),cell(e.steering_rule),cell(change,'ov-wrap')]);
            var extra = [['NCOS reason', e.reason],['Flows moved', e.total_flows_moved]];
            if (extra.some(function (x) {return x[1] !== undefined && x[1] !== null && x[1] !== '';})) {
                var anchor = h('span',{class:'ov-popover-wrap'});
                var btn = h('button',{type:'button',class:'ov-iconbtn',text:'i','aria-label':'Event details'});
                var pop = makePopover('Steering event details',extra);
                anchor.appendChild(btn);anchor.appendChild(pop);attachPopover(btn,pop);
                row.lastChild.appendChild(document.createTextNode(' '));row.lastChild.appendChild(anchor);
            }
            body.appendChild(row);
        });
        if (!events.length) {body.appendChild(h('tr',{},[h('td',{colspan:'3',text:'No timestamped steering events in the selected window (latest event cache only).'})]));}
        table.appendChild(body);
        return h('div',{class:'ov-table-wrap'},[table,
            h('p',{class:'ov-muted',text:'Events are filtered to the selected time window; the router may retain only a limited recent-event cache.'})]);
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
            var phys = this._visiblePhysical();
            validArray(bond.members).forEach(function (m) {
                var ref = m.physical || {};
                if (ref.state !== 'resolved') { return; }
                var p = phys.filter(function (x) { return x.uid === ref.uid; })[0];
                if (p) { add(p.uid, p.display_name, 'physical'); }
            });
        } else {
            var allPaths = this._visiblePhysical();
            var primaryPath = validArray((this.snapshot.wan || {}).paths).filter(function (p) {
                return p.uid === primary && (p.object_class === 'physical' || p.object_class === 'sdwan');
            })[0];
            if (primaryPath) { add(primaryPath.uid, primaryPath.display_name, primaryPath.object_class); }
            allPaths.forEach(function (p) { add(p.uid, p.display_name, 'physical'); });
        }
        // If the recorded primary is unknown, fall back to the first observed path without claiming it is primary.
        if (!all.length) { this._visiblePhysical().forEach(function (p) { add(p.uid, p.display_name, 'physical'); }); }
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