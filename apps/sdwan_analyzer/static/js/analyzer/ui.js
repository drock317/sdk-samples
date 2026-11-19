/* SD-WAN Analyzer: small reusable UI components, built on the web app template
 * classes (control-panel / results-panel, form-select, btn, spinner-circle).
 * Text is always set with textContent: no API value is ever parsed as HTML. */
(function (root) {
    'use strict';

    /* h('div', {class:'x', 'data-a':'1'}, [child | 'text', ...]) */
    function h(tag, attrs, children) {
        var el = document.createElement(tag);
        Object.keys(attrs || {}).forEach(function (k) {
            var v = attrs[k];
            if (v === null || v === undefined || v === false) { return; }
            if (k === 'class') { el.className = v; }
            else if (k === 'text') { el.textContent = v; }
            else { el.setAttribute(k, v === true ? '' : v); }
        });
        [].concat(children || []).forEach(function (c) {
            if (c === null || c === undefined) { return; }
            el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
        });
        return el;
    }

    function clear(el) {
        while (el.firstChild) { el.removeChild(el.firstChild); }
    }

    var uid = 0;
    function nextId(prefix) { uid += 1; return prefix + '-' + uid; }

    /* Panel with a header (title + optional right-hand actions) and a content area.
     * Returns {el, header, content}. */
    function panel(title, actions) {
        var content = h('div', {class: 'panel-content sa-panel-content'});
        var head = h('div', {class: 'panel-header sa-panel-header'}, [
            h('h2', {text: title}), actions ? h('div', {class: 'sa-panel-actions'}, actions) : null
        ]);
        return {el: h('section', {class: 'results-panel sa-panel'}, [head, content]),
                header: head, content: content};
    }

    /* Labelled <select> that supports option groups.
     * groups: [{label, options:[{value,label}]}]. Returns {el, select, setOptions, value}. */
    function selectField(label, onChange) {
        var id = nextId('sa-select');
        var select = h('select', {class: 'form-select', id: id});
        select.addEventListener('change', function () { onChange(select.value); });
        var field = h('div', {class: 'sa-field'}, [
            h('label', {class: 'sa-label', for: id, text: label}), select]);
        return {
            el: field, select: select,
            setOptions: function (groups, selected) {
                clear(select);
                groups.forEach(function (g) {
                    var og = h('optgroup', {label: g.label});
                    g.options.forEach(function (o) {
                        og.appendChild(h('option', {value: o.value, text: o.label}));
                    });
                    select.appendChild(og);
                });
                if (selected !== undefined && selected !== null) { select.value = selected; }
            },
            setDisabled: function (d) { select.disabled = !!d; },
            value: function () { return select.value; }
        };
    }

    /* Single-choice button group (radio semantics via aria-pressed).
     * options: [{value,label}]. Returns {el, setValue, value, setDisabled}. */
    function segmented(label, options, selected, onChange) {
        var current = selected;
        var buttons = {};
        var group = h('div', {class: 'sa-segmented', role: 'group', 'aria-label': label});
        options.forEach(function (o) {
            var b = h('button', {type: 'button', class: 'sa-segment', text: o.label,
                                 'data-value': o.value});
            b.addEventListener('click', function () {
                if (o.value === current) { return; }
                set(o.value);
                onChange(o.value);
            });
            buttons[o.value] = b;
            group.appendChild(b);
        });
        function set(v) {
            current = v;
            Object.keys(buttons).forEach(function (k) {
                buttons[k].setAttribute('aria-pressed', k === v ? 'true' : 'false');
                buttons[k].classList.toggle('active', k === v);
            });
        }
        set(selected);
        return {
            el: h('div', {class: 'sa-field'}, [h('span', {class: 'sa-label', text: label}), group]),
            setValue: set, value: function () { return current; },
            setDisabled: function (d) {
                Object.keys(buttons).forEach(function (k) { buttons[k].disabled = !!d; });
            }
        };
    }

    /* Loading / empty / error / info block shown where content would be.
     * kind: 'loading' | 'empty' | 'error'. */
    function statePanel(kind, title, detail, action) {
        var kids = [];
        if (kind === 'loading') { kids.push(h('div', {class: 'spinner-circle', 'aria-hidden': 'true'})); }
        kids.push(h('p', {class: 'sa-state-title', text: title}));
        if (detail) { kids.push(h('p', {class: 'sa-state-detail', text: detail})); }
        if (action) { kids.push(action); }
        return h('div', {class: 'sa-state sa-state-' + kind,
                         role: kind === 'error' ? 'alert' : 'status'}, kids);
    }

    function button(label, onClick, extra) {
        var b = h('button', {type: 'button', class: 'btn btn-secondary sa-btn ' + (extra || ''),
                             text: label});
        b.addEventListener('click', onClick);
        return b;
    }

    /* Inline notice under a chart: kind 'info' | 'warning'. */
    function notice(kind, text) {
        return h('p', {class: 'sa-notice sa-notice-' + kind, role: 'note', text: text});
    }

    root.SdwanUi = {h: h, clear: clear, panel: panel, selectField: selectField,
                    segmented: segmented, statePanel: statePanel, button: button,
                    notice: notice};
}(window));
