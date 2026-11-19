/* SD-WAN Analyzer: Throughput trends view.
 *
 * Interface selector (from /api/wan) + time range (1h, 6h, 24h, 7d) + RX/TX line
 * chart (from /api/analytics/interfaces, every page within the range, bounded).
 * Loading, empty and error states are explicit. Real API data only.
 */
(function (root) {
    'use strict';

    var A = root.SdwanApi, S = root.SdwanSeries, F = root.SdwanFormat, U = root.SdwanUi;
    var h = U.h;

    var RANGES = [
        {value: '3600', label: '1h', long: 'hour', seconds: 3600},
        {value: '21600', label: '6h', long: '6 hours', seconds: 21600},
        {value: '86400', label: '24h', long: '24 hours', seconds: 86400},
        {value: '604800', label: '7d', long: '7 days', seconds: 604800}
    ];
    var DEFAULT_RANGE = '86400';
    var GROUPS = [
        {cls: 'physical', label: 'Physical WAN'},
        {cls: 'sdwan', label: 'SD-WAN overlay'},
        {cls: 'wbond', label: 'WBOND'}
    ];

    function rangeInfo(value) {
        for (var i = 0; i < RANGES.length; i++) { if (RANGES[i].value === value) { return RANGES[i]; } }
        return RANGES[2];
    }

    function ThroughputTrends(mount) {
        this.mount = mount;
        this.interfaces = [];
        this.skew = 0;                    // router clock minus browser clock, seconds
        this.token = 0;                   // identifies the latest load
        this.abort = null;
        this.chart = null;
        this._build();
        this._loadInterfaces();
    }

    ThroughputTrends.prototype._build = function () {
        var self = this;
        this.ifaceField = U.selectField('Interface', function () { self.load(); });
        this.rangeField = U.segmented('Time range', RANGES.map(function (r) {
            return {value: r.value, label: r.label};
        }), DEFAULT_RANGE, function () { self.load(); });
        this.refreshBtn = U.button('Refresh', function () { self._refresh(); });
        this.controls = h('div', {class: 'sa-controls'}, [
            this.ifaceField.el, this.rangeField.el,
            h('div', {class: 'sa-field sa-field-action'}, [this.refreshBtn])]);

        this.meta = h('p', {class: 'sa-meta', 'aria-live': 'polite'});
        this.stateHost = h('div', {class: 'sa-state-host'});
        this.chartHost = h('div', {class: 'sa-chart-host', hidden: true});
        this.notes = h('div', {class: 'sa-notes'});
        this.results = U.panel('RX and TX throughput');
        this.results.content.appendChild(this.meta);
        this.results.content.appendChild(this.stateHost);
        this.results.content.appendChild(this.chartHost);
        this.results.content.appendChild(this.notes);

        this.mount.appendChild(h('section', {class: 'results-panel sa-panel sa-controls-panel'},
                                 [h('div', {class: 'panel-content sa-panel-content'}, [this.controls])]));
        this.mount.appendChild(this.results.el);
        this._setControlsEnabled(false);
    };

    ThroughputTrends.prototype._setControlsEnabled = function (on) {
        this.ifaceField.setDisabled(!on);
        this.rangeField.setDisabled(!on);
        this.refreshBtn.disabled = !on;
    };

    ThroughputTrends.prototype._showState = function (node) {
        U.clear(this.stateHost);
        U.clear(this.notes);
        this.chartHost.hidden = true;
        this.stateHost.hidden = false;
        if (node) { this.stateHost.appendChild(node); }
    };

    ThroughputTrends.prototype._showChart = function () {
        U.clear(this.stateHost);
        this.stateHost.hidden = true;
        this.chartHost.hidden = false;
    };

    // --- interfaces ------------------------------------------------------------

    ThroughputTrends.prototype._loadInterfaces = function () {
        var self = this;
        this.meta.textContent = '';
        this._showState(U.statePanel('loading', 'Loading interfaces'));
        A.fetchInterfaces().then(function (r) {
            self.interfaces = r.interfaces;
            if (r.serverTs !== null) { self.skew = r.serverTs - Date.now() / 1000; }
            if (!r.interfaces.length) {
                self._showState(U.statePanel('empty', 'No WAN interfaces reported',
                    'The analyzer has not reported any physical WAN, SD-WAN overlay or WBOND interface yet.',
                    U.button('Try again', function () { self._loadInterfaces(); })));
                return;
            }
            var groups = [], primary = null;
            GROUPS.forEach(function (g) {
                var opts = r.interfaces.filter(function (i) { return i.objectClass === g.cls; })
                    .map(function (i) {
                        if (i.uid === r.primary) { primary = i.uid; }
                        return {value: i.uid, label: i.label === i.uid ? i.uid : i.label + ' (' + i.uid + ')'};
                    });
                if (opts.length) { groups.push({label: g.label, options: opts}); }
            });
            self.ifaceField.setOptions(groups, primary || r.interfaces[0].uid);
            if (!primary) { self.ifaceField.select.selectedIndex = 0; }
            self._setControlsEnabled(true);
            self.load();
        }).catch(function (e) {
            self._showState(U.statePanel('error', 'Could not load the interface list', A.describe(e),
                U.button('Try again', function () { self._loadInterfaces(); })));
        });
    };

    // --- data ------------------------------------------------------------------

    ThroughputTrends.prototype._refresh = function () {
        // Re-read the interface list too, so new overlays appear, then reload the chart.
        var self = this;
        var current = this.ifaceField.value();
        A.fetchInterfaces().then(function (r) {
            if (r.serverTs !== null) { self.skew = r.serverTs - Date.now() / 1000; }
            var known = r.interfaces.some(function (i) { return i.uid === current; });
            if (known && r.interfaces.length === self.interfaces.length) { self.load(); }
            else { self._loadInterfaces(); }
        }).catch(function () { self.load(); });
    };

    ThroughputTrends.prototype.load = function () {
        var self = this;
        var uid = this.ifaceField.value();
        if (!uid) { return; }
        var range = rangeInfo(this.rangeField.value());
        var iface = this.interfaces.filter(function (i) { return i.uid === uid; })[0];
        var name = iface ? iface.label : uid;
        if (this.abort) { this.abort.abort(); }
        var ctrl = new AbortController();
        this.abort = ctrl;
        var token = ++this.token;
        var now = Date.now() / 1000 + this.skew;
        var from = now - range.seconds;
        this.meta.textContent = '';
        this._showState(U.statePanel('loading', 'Loading throughput history', 'Reading stored measurements for ' + name + '.'));
        var progress = this.stateHost.querySelector('.sa-state-title');

        A.fetchRates(uid, from, ctrl.signal, function (count) {
            if (token === self.token && progress && count) {
                progress.textContent = 'Loading throughput history (' + count + ' measurements)';
            }
        }).then(function (res) {
            if (token !== self.token) { return; }
            self._show(res, {uid: uid, name: name, range: range, from: from, to: now});
        }).catch(function (e) {
            if (token !== self.token || (e && e.kind === 'aborted')) { return; }
            var detail = A.describe(e);
            if (e && e.status === 410) { detail = 'The stored history changed while it was loading. Try again.'; }
            self._showState(U.statePanel('error', 'Could not load throughput history', detail,
                U.button('Try again', function () { self.load(); })));
        });
    };

    ThroughputTrends.prototype._show = function (res, ctx) {
        var built = S.buildIntervals(res.rates);
        var iv = built.intervals;
        var label = ctx.name + ', last ' + ctx.range.long;

        if (!iv.length) {
            this._showState(U.statePanel('empty', 'No throughput history for ' + label,
                'Throughput is calculated between two stored samples, and samples are collected every ' +
                '5 to 60 minutes. Check back after the analyzer has collected at least two samples ' +
                'for this interface in the selected time range.'));
            return;
        }

        this._showChart();
        if (!this.chart) { this.chart = new root.SdwanLineChart(this.chartHost); }
        this.chart.render({intervals: iv, from: ctx.from, to: ctx.to});

        var sum = S.summarize(iv);
        this.meta.textContent = label + ' · ' + iv.length + ' measurement intervals · loaded ' +
            F.timeOnly(Date.now() / 1000);

        U.clear(this.notes);
        var notes = this.notes;
        var line = sum.bothValid + ' of ' + sum.total + ' intervals have valid RX and TX values.';
        if (sum.anyGap) {
            var parts = Object.keys(sum.reasons).filter(function (k) { return sum.reasons[k] > 0; })
                .map(function (k) { return S.statusText(k) + ': ' + sum.reasons[k]; });
            line += ' Gaps are not zero throughput (' + parts.join('; ') + ').';
        }
        notes.appendChild(U.notice('info', line));
        if (!S.hasAnyValue(iv)) {
            notes.appendChild(U.notice('warning',
                'No interval in this range has a valid throughput value, so no line is drawn.'));
        }
        if (res.truncated) {
            notes.appendChild(U.notice('warning', 'This range has more history than the view loads (' +
                res.rates.length + ' measurements, ' + res.requests + ' requests). The oldest measurements are shown; ' +
                'the newest were not loaded. Choose a shorter time range.'));
        }
        if (built.unplaced) {
            notes.appendChild(U.notice('warning', built.unplaced + ' interval(s) without usable timestamps were not drawn.'));
        }
        if (res.skippedLines) {
            notes.appendChild(U.notice('warning', res.skippedLines + ' unreadable history line(s) were skipped by the server.'));
        }
    };

    root.SdwanThroughputTrends = ThroughputTrends;
}(window));
