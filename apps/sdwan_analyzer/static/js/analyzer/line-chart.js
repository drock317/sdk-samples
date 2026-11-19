/* SD-WAN Analyzer: dependency-free SVG line chart for RX/TX throughput.
 *
 * Usage:  var chart = new SdwanLineChart(containerEl);
 *         chart.render({intervals, from, to});   // from/to: epoch seconds (x domain)
 *         chart.destroy();
 *
 * Gaps: lines are drawn as separate runs (see SdwanSeries.runs), so an invalid
 * interval is a break in the line, never a drop to zero. Intervals with no valid
 * value in either direction are also shaded as a gap band. Colours come from CSS
 * classes (.sa-rx, .sa-tx, ...), so theme changes need no redraw.
 */
(function (root) {
    'use strict';

    var NS = 'http://www.w3.org/2000/svg';
    var S = root.SdwanSeries, F = root.SdwanFormat, U = root.SdwanUi;
    var MARGIN = {l: 58, r: 16, t: 14, b: 40};
    var DOT_LIMIT = 150;       // draw a marker per measurement up to this many points

    function svgEl(tag, attrs) {
        var el = document.createElementNS(NS, tag);
        Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
        return el;
    }

    function LineChart(container) {
        this.container = container;
        this.model = null;
        this.hover = -1;
        this._raf = 0;
        this._build();
        var self = this;
        this._onResize = function () {
            if (self._raf) { return; }
            self._raf = root.requestAnimationFrame(function () { self._raf = 0; self._draw(); });
        };
        if (root.ResizeObserver) {
            this._ro = new root.ResizeObserver(this._onResize);
            this._ro.observe(this.wrap);
        } else {
            root.addEventListener('resize', this._onResize);
        }
    }

    LineChart.prototype._build = function () {
        var h = U.h;
        this.legend = h('ul', {class: 'sa-legend', 'aria-label': 'Chart legend'}, [
            h('li', {}, [h('span', {class: 'sa-swatch sa-swatch-rx', 'aria-hidden': 'true'}), 'RX (received)']),
            h('li', {}, [h('span', {class: 'sa-swatch sa-swatch-tx', 'aria-hidden': 'true'}), 'TX (transmitted)']),
            h('li', {}, [h('span', {class: 'sa-swatch sa-swatch-gap', 'aria-hidden': 'true'}),
                         'Gap: no valid measurement'])
        ]);
        this.tip = h('div', {class: 'sa-tooltip', role: 'status', hidden: true});
        this.svg = svgEl('svg', {class: 'sa-chart', tabindex: '0', role: 'img'});
        this.wrap = h('div', {class: 'sa-chart-wrap'}, [this.svg, this.tip]);
        this.container.appendChild(this.legend);
        this.container.appendChild(this.wrap);
        var self = this;
        this.svg.addEventListener('keydown', function (e) { self._key(e); });
        this.svg.addEventListener('blur', function () { self._setHover(-1); });
    };

    LineChart.prototype.destroy = function () {
        if (this._ro) { this._ro.disconnect(); } else { root.removeEventListener('resize', this._onResize); }
        if (this._raf) { root.cancelAnimationFrame(this._raf); }
        U.clear(this.container);
    };

    LineChart.prototype.render = function (model) {
        this.model = model;
        this.hover = -1;
        this._draw();
    };

    LineChart.prototype._draw = function () {
        var m = this.model;
        if (!m) { return; }
        this.hover = -1;
        this.tip.hidden = true;
        var width = Math.max(280, Math.floor(this.wrap.clientWidth || 600));
        var height = width < 560 ? 260 : 340;
        var iv = m.intervals;
        var from = m.from, to = m.to;
        var plotW = width - MARGIN.l - MARGIN.r;
        var plotH = height - MARGIN.t - MARGIN.b;
        var maxV = 0;
        iv.forEach(function (p) {
            if (p.rx !== null && p.rx > maxV) { maxV = p.rx; }
            if (p.tx !== null && p.tx > maxV) { maxV = p.tx; }
        });
        var unit = F.pickUnit(maxV);              // one unit for both directions and all ticks
        var yAxis = F.niceAxis(maxV / unit.div, 4);
        var span = Math.max(1, to - from);
        function x(t) { return MARGIN.l + (Math.min(Math.max(t, from), to) - from) / span * plotW; }
        function y(v) { return MARGIN.t + plotH - (v / unit.div / yAxis.max) * plotH; }
        this._x = x; this._plotW = plotW; this._plot = {w: plotW, h: plotH, width: width, height: height};

        var svg = this.svg;
        U.clear(svg);
        svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
        svg.setAttribute('width', width);
        svg.setAttribute('height', height);
        svg.setAttribute('aria-label', 'Throughput line chart in ' + unit.name + ', ' + iv.length +
            ' measurement intervals from ' + F.dateTime(from) + ' to ' + F.dateTime(to) +
            '. Focus the chart and use the left and right arrow keys to read values.');

        var i;
        // horizontal grid + y labels
        for (i = 0; i < yAxis.ticks.length; i++) {
            var ty = y(yAxis.ticks[i]);
            svg.appendChild(svgEl('line', {class: 'sa-grid', x1: MARGIN.l, x2: MARGIN.l + plotW, y1: ty, y2: ty}));
            var lab = svgEl('text', {class: 'sa-tick', x: MARGIN.l - 8, y: ty + 4, 'text-anchor': 'end'});
            lab.textContent = F.tick(yAxis.ticks[i]);
            svg.appendChild(lab);
        }
        // x ticks
        var tt = F.timeTicks(from, to, Math.max(3, Math.floor(plotW / 90)));
        for (i = 0; i < tt.ticks.length; i++) {
            var tx = x(tt.ticks[i]);
            svg.appendChild(svgEl('line', {class: 'sa-grid sa-grid-x', x1: tx, x2: tx, y1: MARGIN.t, y2: MARGIN.t + plotH}));
            var xl = svgEl('text', {class: 'sa-tick', x: tx, y: MARGIN.t + plotH + 16, 'text-anchor': 'middle'});
            xl.textContent = F.timeTickLabel(tt.ticks[i], tt.step, span);
            svg.appendChild(xl);
        }
        // axis titles
        var yt = svgEl('text', {class: 'sa-axis-title', transform: 'translate(14 ' + (MARGIN.t + plotH / 2) +
                               ') rotate(-90)', 'text-anchor': 'middle'});
        yt.textContent = 'Throughput (' + unit.name + ')';
        svg.appendChild(yt);
        var xt = svgEl('text', {class: 'sa-axis-title', x: MARGIN.l + plotW / 2, y: height - 6, 'text-anchor': 'middle'});
        xt.textContent = 'Time (' + F.timeZoneName() + ')';
        svg.appendChild(xt);

        // gap bands: intervals with no valid value in either direction
        iv.forEach(function (p) {
            if (p.rx !== null || p.tx !== null) { return; }
            var x0 = x(p.start), x1 = x(p.end);
            svg.appendChild(svgEl('rect', {class: 'sa-gap-band', x: x0, y: MARGIN.t,
                                           width: Math.max(2, x1 - x0), height: plotH}));
        });

        // lines per direction, one path per contiguous run
        var dots = iv.length <= DOT_LIMIT;
        [['rx', 'sa-rx'], ['tx', 'sa-tx']].forEach(function (d) {
            S.runs(iv, d[0]).forEach(function (run) {
                if (run.length > 1) {
                    var path = run.map(function (p, k) {
                        return (k ? 'L' : 'M') + x(p.t).toFixed(2) + ' ' + y(p.v).toFixed(2);
                    }).join('');
                    svg.appendChild(svgEl('path', {class: 'sa-line ' + d[1], d: path}));
                }
                if (run.length === 1 || dots) {
                    run.forEach(function (p) {
                        svg.appendChild(svgEl('circle', {class: 'sa-dot ' + d[1], cx: x(p.t).toFixed(2),
                                                         cy: y(p.v).toFixed(2), r: run.length === 1 ? 3.5 : 2.5}));
                    });
                }
            });
        });

        // axes frame
        svg.appendChild(svgEl('line', {class: 'sa-axis', x1: MARGIN.l, x2: MARGIN.l + plotW,
                                       y1: MARGIN.t + plotH, y2: MARGIN.t + plotH}));
        // hover layer
        this.cross = svgEl('line', {class: 'sa-cross', y1: MARGIN.t, y2: MARGIN.t + plotH, visibility: 'hidden'});
        this.markRx = svgEl('circle', {class: 'sa-mark sa-rx', r: 5, visibility: 'hidden'});
        this.markTx = svgEl('circle', {class: 'sa-mark sa-tx', r: 5, visibility: 'hidden'});
        svg.appendChild(this.cross); svg.appendChild(this.markRx); svg.appendChild(this.markTx);
        this._y = y;
        var overlay = svgEl('rect', {class: 'sa-overlay', x: MARGIN.l, y: MARGIN.t, width: plotW, height: plotH});
        var self = this;
        function at(e) {
            var r = svg.getBoundingClientRect();
            var px = (e.clientX - r.left) * (width / r.width);
            var t = from + ((px - MARGIN.l) / plotW) * span;
            self._setHover(S.nearest(iv, t));
        }
        overlay.addEventListener('pointermove', at);
        overlay.addEventListener('pointerdown', at);
        overlay.addEventListener('pointerleave', function () { self._setHover(-1); });
        svg.appendChild(overlay);
    };

    LineChart.prototype._key = function (e) {
        var n = this.model ? this.model.intervals.length : 0;
        if (!n) { return; }
        var next = this.hover;
        if (e.key === 'ArrowLeft') { next = this.hover < 0 ? n - 1 : Math.max(0, this.hover - 1); }
        else if (e.key === 'ArrowRight') { next = this.hover < 0 ? n - 1 : Math.min(n - 1, this.hover + 1); }
        else if (e.key === 'Home') { next = 0; }
        else if (e.key === 'End') { next = n - 1; }
        else if (e.key === 'Escape') { next = -1; }
        else { return; }
        e.preventDefault();
        this._setHover(next);
    };

    LineChart.prototype._dirLine = function (label, value, status) {
        var h = U.h;
        var text = value !== null ? F.rate(value) : 'no valid measurement (' + S.statusText(status) + ')';
        return h('div', {class: 'sa-tip-row'}, [
            h('span', {class: 'sa-swatch ' + (label === 'RX' ? 'sa-swatch-rx' : 'sa-swatch-tx'), 'aria-hidden': 'true'}),
            h('strong', {text: label}), h('span', {text: text})]);
    };

    LineChart.prototype._setHover = function (idx) {
        if (idx === this.hover) { return; }
        this.hover = idx;
        var tip = this.tip;
        if (idx < 0 || !this.model) {
            tip.hidden = true;
            this.cross.setAttribute('visibility', 'hidden');
            this.markRx.setAttribute('visibility', 'hidden');
            this.markTx.setAttribute('visibility', 'hidden');
            return;
        }
        var p = this.model.intervals[idx];
        var px = this._x(p.end);
        this.cross.setAttribute('x1', px); this.cross.setAttribute('x2', px);
        this.cross.setAttribute('visibility', 'visible');
        var self = this;
        [[this.markRx, p.rx], [this.markTx, p.tx]].forEach(function (m) {
            if (m[1] === null) { m[0].setAttribute('visibility', 'hidden'); return; }
            m[0].setAttribute('cx', px); m[0].setAttribute('cy', self._y(m[1]));
            m[0].setAttribute('visibility', 'visible');
        });
        // tooltip content (text nodes only)
        var h = U.h;
        U.clear(tip);
        tip.appendChild(h('div', {class: 'sa-tip-time', text: F.dateTime(p.end)}));
        var sub = 'Interval ' + F.timeOnly(p.start) + ' to ' + F.timeOnly(p.end);
        if (p.intervalS !== null) { sub += ' (' + F.duration(p.intervalS) + ')'; }
        tip.appendChild(h('div', {class: 'sa-tip-sub', text: sub}));
        tip.appendChild(h('div', {class: 'sa-tip-sub', text: F.utcDateTime(p.end)}));
        tip.appendChild(this._dirLine('RX', p.rx, p.rxStatus));
        tip.appendChild(this._dirLine('TX', p.tx, p.txStatus));
        if (p.reduced) {
            tip.appendChild(h('div', {class: 'sa-tip-sub', text: 'Whole-second timestamps (reduced precision)'}));
        }
        tip.hidden = false;
        // position: right of the cursor line unless it would overflow
        var wrapW = this.wrap.clientWidth;
        var tw = tip.offsetWidth;
        var left = px + 12;
        if (left + tw > wrapW) { left = Math.max(0, px - 12 - tw); }
        tip.style.left = left + 'px';
        tip.style.top = (MARGIN.t + 4) + 'px';
    };

    root.SdwanLineChart = LineChart;
}(window));
