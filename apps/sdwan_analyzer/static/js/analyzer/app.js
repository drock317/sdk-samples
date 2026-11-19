/* SD-WAN Analyzer: page bootstrap. Mounts the Throughput trends view. Header,
 * sidebar, theme, layout and the version label come from the web app template
 * (static/js/script.js). */
(function (root) {
    'use strict';

    function start() {
        var mount = document.getElementById('throughput-trends-root');
        if (mount) { new root.SdwanThroughputTrends(mount); }
    }

    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', start); }
    else { start(); }
}(window));
