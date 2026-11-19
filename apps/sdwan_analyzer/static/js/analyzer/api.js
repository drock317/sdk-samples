/* SD-WAN Analyzer: API client.
 *
 * The browser talks only to this app's own JSON endpoints:
 *   GET /api/wan                  interfaces available (paths[])
 *   GET /api/analytics/interfaces calculated RX/TX rates, paginated
 * Nothing is cached, aggregated or synthesised here.
 */
(function (root) {
    'use strict';

    var REQUEST_TIMEOUT_MS = 20000;   // one HTTP request
    var PAGE_LIMIT = 250;             // server maximum rates per page
    var MAX_REQUESTS = 40;            // hard cap on pages fetched per view load
    var MAX_RATES = 3000;             // hard cap on rates held / drawn per view load
    // A pair of observations further apart than this is reported by the server as
    // gap_exceeded (a chart gap) instead of one long averaged rate. Twice the
    // longest selectable collection interval (60 minutes).
    var MAX_GAP_S = 7200;

    function ApiError(kind, message, status) {
        this.name = 'ApiError';
        this.kind = kind;          // 'http' | 'network' | 'timeout' | 'parse' | 'aborted'
        this.message = message;
        this.status = status || null;
    }
    ApiError.prototype = Object.create(Error.prototype);

    function describe(err) {
        if (!err) { return 'Unknown error'; }
        if (err.kind === 'timeout') { return 'The router did not answer in time.'; }
        if (err.kind === 'network') { return 'Could not reach the SD-WAN Analyzer.'; }
        return err.message || 'Unknown error';
    }

    /* GET JSON with a timeout and optional external abort signal.
     * Resolves {data, serverTs}; serverTs is the router clock from the HTTP Date
     * header (epoch seconds) or null. */
    function getJson(url, signal) {
        return new Promise(function (resolve, reject) {
            var ctrl = new AbortController();
            var timedOut = false;
            var timer = setTimeout(function () { timedOut = true; ctrl.abort(); },
                                   REQUEST_TIMEOUT_MS);
            function onAbort() { ctrl.abort(); }
            if (signal) {
                if (signal.aborted) { clearTimeout(timer); reject(new ApiError('aborted', 'aborted')); return; }
                signal.addEventListener('abort', onAbort);
            }
            function done() {
                clearTimeout(timer);
                if (signal) { signal.removeEventListener('abort', onAbort); }
            }
            fetch(url, {signal: ctrl.signal, cache: 'no-store'}).then(function (resp) {
                var dateHdr = resp.headers.get('Date');
                var serverTs = dateHdr ? Date.parse(dateHdr) / 1000 : NaN;
                return resp.text().then(function (text) {
                    var data = null;
                    try { data = text ? JSON.parse(text) : null; } catch (e) {
                        if (resp.ok) { throw new ApiError('parse', 'The response was not valid JSON.'); }
                    }
                    if (!resp.ok) {
                        var msg = (data && data.error) ? String(data.error) : ('HTTP ' + resp.status);
                        throw new ApiError('http', msg, resp.status);
                    }
                    done();
                    resolve({data: data, serverTs: isFinite(serverTs) ? serverTs : null});
                });
            }).catch(function (e) {
                done();
                if (e instanceof ApiError) { reject(e); return; }
                if (signal && signal.aborted) { reject(new ApiError('aborted', 'aborted')); return; }
                if (timedOut) { reject(new ApiError('timeout', 'timeout')); return; }
                reject(new ApiError('network', String(e && e.message || e)));
            });
        });
    }

    /* Interfaces for the selector, from /api/wan paths[] (physical, sdwan, wbond only).
     * Resolves {interfaces:[{uid,label,objectClass,state}], primary, serverTs}. */
    function fetchInterfaces(signal) {
        return getJson('api/wan', signal).then(function (r) {
            var wan = r.data || {};
            var out = [];
            (Array.isArray(wan.paths) ? wan.paths : []).forEach(function (p) {
                if (!p || typeof p.uid !== 'string') { return; }
                if (p.object_class !== 'physical' && p.object_class !== 'sdwan' &&
                    p.object_class !== 'wbond') { return; }
                out.push({uid: p.uid, label: p.display_name || p.uid,
                          objectClass: p.object_class, state: p.connection_state || null});
            });
            return {interfaces: out, primary: wan.primary_device || null, serverTs: r.serverTs};
        });
    }

    /* Every rate for one interface from startTs onward, following next_cursor.
     * Bounded by MAX_REQUESTS and MAX_RATES; `truncated` is true when history
     * remained that was not loaded. onProgress(count) is called after each page.
     * Resolves {rates, truncated, requests, skippedLines}. */
    function fetchRates(uid, startTs, signal, onProgress) {
        var rates = [];
        var seen = {};
        var state = {requests: 0, skipped: 0};

        function page(cursor) {
            var q = 'uid=' + encodeURIComponent(uid) +
                    '&start=' + encodeURIComponent(startTs.toFixed(3)) +
                    '&limit=' + PAGE_LIMIT + '&max_gap_s=' + MAX_GAP_S;
            if (cursor) { q += '&cursor=' + encodeURIComponent(cursor); }
            state.requests += 1;
            return getJson('api/analytics/interfaces?' + q, signal).then(function (r) {
                var d = r.data || {};
                var list = Array.isArray(d.rates) ? d.rates : [];
                list.forEach(function (rate) {
                    var key = rate && (rate.end_ts + '|' + rate.start_ts);
                    if (!rate || seen[key]) { return; }
                    seen[key] = true;
                    rates.push(rate);
                });
                state.skipped += d.skipped_lines || 0;
                if (onProgress) { onProgress(rates.length); }
                var next = d.has_more && typeof d.next_cursor === 'string' ? d.next_cursor : null;
                if (next && next === cursor) { next = null; }       // never loop on a stuck cursor
                if (!next) { return false; }
                if (rates.length >= MAX_RATES || state.requests >= MAX_REQUESTS) { return true; }
                return page(next);
            });
        }

        return page(null).then(function (truncated) {
            return {rates: rates, truncated: truncated, requests: state.requests,
                    skippedLines: state.skipped};
        });
    }

    root.SdwanApi = {
        ApiError: ApiError, describe: describe, getJson: getJson,
        fetchInterfaces: fetchInterfaces, fetchRates: fetchRates,
        limits: {PAGE_LIMIT: PAGE_LIMIT, MAX_REQUESTS: MAX_REQUESTS,
                 MAX_RATES: MAX_RATES, MAX_GAP_S: MAX_GAP_S}
    };
}(window));
