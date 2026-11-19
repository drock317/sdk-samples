/* SD-WAN Analyzer: number and time formatting shared by the views. Pure. */
(function (root) {
    'use strict';

    function pad(n) { return (n < 10 ? '0' : '') + n; }

    /* Decimal networking units (1 Kbps = 1000 bps). */
    var UNITS = [{name: 'bps', div: 1}, {name: 'Kbps', div: 1e3},
                 {name: 'Mbps', div: 1e6}, {name: 'Gbps', div: 1e9}];

    function unitIndex(bps) {
        var i = 0;
        while (i < UNITS.length - 1 && bps >= UNITS[i + 1].div) { i += 1; }
        return i;
    }

    /* One unit for a whole chart, chosen from the largest valid value (bits/s).
     * No data, or only zeros, gives bps. */
    function pickUnit(maxBps) {
        if (typeof maxBps !== 'number' || !isFinite(maxBps) || maxBps <= 0) { return UNITS[0]; }
        return UNITS[unitIndex(maxBps)];
    }

    /* A number in its unit with enough precision that a small non-zero value is
     * never shown as 0: 12.346, 250.0, 0.000412. */
    function plain(v) {
        if (v === 0) { return '0'; }
        if (v >= 100) { return v.toFixed(1); }
        if (v >= 1) { return v.toFixed(3); }
        return String(parseFloat(v.toPrecision(3)));
    }

    /* Value with its unit, e.g. "12.346 Mbps". Pass `unit` to force one (axis);
     * otherwise the unit is chosen for this value. Returns '' for non-numbers. */
    function rate(bps, unit) {
        if (typeof bps !== 'number' || !isFinite(bps)) { return ''; }
        if (bps === 0) { return '0 ' + (unit || UNITS[0]).name; }
        var i = unit ? UNITS.indexOf(unit) : unitIndex(bps);
        var text = plain(bps / UNITS[i].div);
        // 999.9996 Kbps would print as 1000.0: show it in the next unit instead.
        if (!unit && i < UNITS.length - 1 && parseFloat(text) >= 1000) {
            i += 1;
            text = plain(bps / UNITS[i].div);
        }
        return text + ' ' + UNITS[i].name;
    }

    /* Axis tick label (value already divided by the chart unit). */
    function tick(v) {
        if (v === 0) { return '0'; }
        if (v >= 100) { return String(Math.round(v)); }
        if (v >= 1) { return String(parseFloat(v.toFixed(2))); }
        return String(parseFloat(v.toPrecision(2)));
    }

    function clock(d, seconds) {
        return pad(d.getHours()) + ':' + pad(d.getMinutes()) + (seconds ? ':' + pad(d.getSeconds()) : '');
    }

    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    /* Local date + time, e.g. "19 Nov 2026 14:05:09". */
    function dateTime(ts) {
        var d = new Date(ts * 1000);
        return d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear() + ' ' + clock(d, true);
    }

    function utcDateTime(ts) {
        var d = new Date(ts * 1000);
        return d.getUTCDate() + ' ' + MONTHS[d.getUTCMonth()] + ' ' + d.getUTCFullYear() + ' ' +
               pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + ' UTC';
    }

    function timeOnly(ts) { return clock(new Date(ts * 1000), true); }

    function duration(seconds) {
        if (typeof seconds !== 'number' || !isFinite(seconds)) { return ''; }
        var s = Math.round(seconds * 10) / 10;
        if (s < 120) { return (s % 1 === 0 ? s : s.toFixed(1)) + ' s'; }
        var m = Math.round(s / 60);
        if (m < 120) { return m + ' min'; }
        return (Math.round(m / 6) / 10) + ' h';
    }

    function timeZoneName() {
        try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time'; }
        catch (e) { return 'local time'; }
    }

    /* Nice y-axis: {max, ticks[]} with a 0 baseline. */
    function niceAxis(maxValue, wanted) {
        var max = (typeof maxValue === 'number' && maxValue > 0) ? maxValue : 1;
        var raw = max / (wanted || 4);
        var mag = Math.pow(10, Math.floor(Math.log10(raw)));
        var norm = raw / mag;
        var step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
        var ticks = [];
        for (var k = 0; k < 20; k++) {
            ticks.push(parseFloat((k * step).toPrecision(12)));
            if (k * step >= max * 0.999999) { break; }
        }
        return {max: ticks[ticks.length - 1], ticks: ticks};
    }

    var STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800];

    /* Time ticks over [t0, t1] aligned to LOCAL clock boundaries. At most `maxTicks`. */
    function timeTicks(t0, t1, maxTicks) {
        var span = t1 - t0;
        var step = STEPS[STEPS.length - 1];
        for (var i = 0; i < STEPS.length; i++) {
            if (span / STEPS[i] <= maxTicks) { step = STEPS[i]; break; }
        }
        var offset = -new Date(t0 * 1000).getTimezoneOffset() * 60;   // local = utc + offset
        var first = Math.ceil((t0 + offset) / step) * step - offset;
        var ticks = [];
        for (var t = first; t <= t1; t += step) { ticks.push(t); }
        return {ticks: ticks, step: step};
    }

    function timeTickLabel(ts, step, spanS) {
        var d = new Date(ts * 1000);
        if (step >= 86400) { return d.getDate() + ' ' + MONTHS[d.getMonth()]; }
        if (spanS > 86400) { return d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + clock(d, false); }
        return clock(d, false);
    }

    root.SdwanFormat = {UNITS: UNITS, pickUnit: pickUnit, rate: rate, tick: tick, dateTime: dateTime,
                        utcDateTime: utcDateTime, timeOnly: timeOnly, duration: duration,
                        timeZoneName: timeZoneName, niceAxis: niceAxis,
                        timeTicks: timeTicks, timeTickLabel: timeTickLabel};
}(typeof window !== 'undefined' ? window : this));
