/* SD-WAN Analyzer: turn API rates into chart data. Pure functions, no DOM.
 *
 * Rules (docs/analytics/counter-derivation.md):
 *  - A direction is plotted only when the API marks that direction status "ok"
 *    and gives a finite, non-negative bits_per_s. Zero is a valid measurement and is kept.
 *  - Everything else (missing value or baseline, suspect input, counter decrease,
 *    gap exceeded, invalid or non-monotonic time, identity mismatch) is NOT zero:
 *    it is a gap. No value is interpolated, bridged or filled.
 *  - A rate covers the interval start_ts..end_ts. It is plotted at end_ts and joined
 *    to the previous point only when that previous interval ends exactly where this
 *    one starts (consecutive observations).
 */
(function (root) {
    'use strict';

    var CONTIGUOUS_EPS_S = 0.001;

    var STATUS_TEXT = {
        ok: 'valid',
        missing_baseline: 'no previous measurement',
        missing_value: 'counter value missing',
        identity_mismatch: 'interface identity changed',
        invalid_time: 'invalid timestamps',
        non_monotonic_time: 'timestamps out of order',
        counter_decrease: 'counter reset or decrease',
        gap_exceeded: 'samples too far apart',
        suspect_input: 'counters not trusted (failed read)'
    };

    function statusText(status) {
        if (status === undefined || status === null) { return 'no status'; }
        return STATUS_TEXT[status] || String(status);
    }

    function num(v) {
        return (typeof v === 'number' && isFinite(v)) ? v : null;
    }

    /* One direction of one rate -> {value: bits/s | null, status:string}. */
    function direction(d) {
        if (!d || typeof d !== 'object') { return {value: null, status: 'missing_value'}; }
        var status = typeof d.status === 'string' ? d.status : 'missing_value';
        var v = num(d.bits_per_s);   // bits per second; the chart picks the display unit
        if (status === 'ok' && v !== null && v >= 0) { return {value: v, status: 'ok'}; }
        if (status === 'ok') { return {value: null, status: 'missing_value'}; }
        return {value: null, status: status};
    }

    /* rates[] (API order) -> intervals sorted by end time.
     * Each: {start,end,rx,tx,rxStatus,txStatus,status,intervalS,reduced}
     * Rates with no usable end timestamp cannot be placed on a time axis; they are
     * counted in `unplaced`. */
    function buildIntervals(rates) {
        var items = [];
        var unplaced = 0;
        (rates || []).forEach(function (r) {
            if (!r || typeof r !== 'object') { unplaced += 1; return; }
            var end = num(r.end_ts);
            if (end === null) { unplaced += 1; return; }
            var start = num(r.start_ts);
            var rx = direction(r.rx);
            var tx = direction(r.tx);
            // suspect_input is stamped on the whole rate; never plot through it.
            if (r.status === 'suspect_input') {
                rx = {value: null, status: 'suspect_input'};
                tx = {value: null, status: 'suspect_input'};
            }
            items.push({
                start: (start !== null && start <= end) ? start : end,
                end: end,
                rx: rx.value, tx: tx.value,
                rxStatus: rx.status, txStatus: tx.status,
                status: typeof r.status === 'string' ? r.status : 'missing_value',
                intervalS: num(r.interval_s),
                reduced: r.reduced_precision === true
            });
        });
        items.sort(function (a, b) { return a.end - b.end; });
        return {intervals: items, unplaced: unplaced};
    }

    /* Split one direction into drawable runs. Each run is [{t, v, i}] where i is
     * the interval index. A run breaks at every gap or non-contiguous interval. */
    function runs(intervals, key) {
        var out = [];
        var cur = null;
        var prev = null;
        intervals.forEach(function (iv, i) {
            var v = iv[key];
            if (v === null) { cur = null; prev = null; return; }
            var joined = cur !== null && prev !== null &&
                         Math.abs(prev.end - iv.start) <= CONTIGUOUS_EPS_S;
            if (!joined) { cur = []; out.push(cur); }
            cur.push({t: iv.end, v: v, i: i});
            prev = iv;
        });
        return out;
    }

    /* Counts for the data-quality line. Not throughput statistics. */
    function summarize(intervals) {
        var s = {total: intervals.length, bothValid: 0, anyGap: 0, reasons: {}};
        intervals.forEach(function (iv) {
            if (iv.rx !== null && iv.tx !== null) { s.bothValid += 1; return; }
            s.anyGap += 1;
            [iv.rxStatus, iv.txStatus].forEach(function (st) {
                if (st !== 'ok') { s.reasons[st] = (s.reasons[st] || 0) + 1; }
            });
            if (iv.rxStatus !== 'ok' && iv.txStatus !== 'ok' && iv.rxStatus === iv.txStatus) {
                s.reasons[iv.rxStatus] -= 1;   // same cause on both directions counts once
            }
        });
        return s;
    }

    function hasAnyValue(intervals) {
        return intervals.some(function (iv) { return iv.rx !== null || iv.tx !== null; });
    }

    /* Index of the interval nearest to time t (containing it, else closest end). */
    function nearest(intervals, t) {
        var n = intervals.length;
        if (n === 0) { return -1; }
        var lo = 0, hi = n - 1;
        while (lo < hi) {                       // first interval with end >= t
            var mid = (lo + hi) >> 1;
            if (intervals[mid].end >= t) { hi = mid; } else { lo = mid + 1; }
        }
        var best = lo;
        if (lo > 0) {
            var a = intervals[lo - 1], b = intervals[lo];
            if (t >= b.start && t <= b.end) { best = lo; }
            else if (t >= a.start && t <= a.end) { best = lo - 1; }
            else if (Math.abs(a.end - t) < Math.abs(b.end - t)) { best = lo - 1; }
        }
        return best;
    }

    root.SdwanSeries = {
        STATUS_TEXT: STATUS_TEXT, statusText: statusText, direction: direction,
        buildIntervals: buildIntervals, runs: runs, summarize: summarize,
        hasAnyValue: hasAnyValue, nearest: nearest
    };
}(typeof window !== 'undefined' ? window : this));
