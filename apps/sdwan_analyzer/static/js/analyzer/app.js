/* Mount the Overview and preserve the existing Throughput trends page. */
(function (root) {
    'use strict';
    function start() {
        var overview = document.getElementById('overview-root');
        if (overview && root.SdwanOverview) { new root.SdwanOverview(overview); }
        var trends = document.getElementById('throughput-trends-root');
        if (trends) { new root.SdwanThroughputTrends(trends); }
    }
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', start); }
    else { start(); }
}(window));