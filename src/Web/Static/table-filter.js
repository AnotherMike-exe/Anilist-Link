/*
 * Per-column sort + filter menus for the library and watchlist tables.
 *
 * Each column header gets a small menu (the ▼ button) offering sort direction
 * and a checkbox list of the values actually present in that column, so the
 * two can be used together or separately — filter to "Missing", then sort by
 * year, and so on.
 *
 * Values come from data-filter-<col> when a row or card carries one, otherwise
 * from the cell's text. Cards only ever have the attributes, so a column a card
 * doesn't carry simply doesn't filter it — the grid never blanks itself out
 * because it lacks a column the table has.
 *
 * The component owns filter state only. Sorting is handed back to the page's
 * existing sort function, and the page's own applyFilters() asks passes() for
 * each row, so search, status tabs and these filters compose instead of
 * fighting each other.
 */
(function () {
    'use strict';

    function TableFilters(opts) {
        this.table = document.getElementById(opts.tableId);
        // Rows aren't always in a tbody with an id — the library table keys off
        // a row class instead.
        this.rowSelector = opts.rowSelector ||
            ('#' + opts.bodyId + ' tr');
        this.onChange = opts.onChange || function () {};
        this.onSort = opts.onSort || null;
        this.active = {};       // col -> Set of allowed values
        this.colIndex = {};     // col -> cell index
        this.menu = null;
        this.openCol = null;
        if (this.table) this._decorateHeaders();
        this._installGlobalHandlers();
    }

    TableFilters.prototype._decorateHeaders = function () {
        var self = this;
        var ths = this.table.querySelectorAll('thead th');
        ths.forEach(function (th, idx) {
            var col = th.getAttribute('data-col');
            if (!col) return;
            self.colIndex[col] = idx;
            if (th.querySelector('.col-filter-btn')) return;
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'col-filter-btn';
            btn.setAttribute('aria-label', 'Sort and filter this column');
            btn.textContent = '▾';
            btn.onclick = function (e) {
                e.stopPropagation();
                self.toggleMenu(col, th);
            };
            th.appendChild(btn);
        });
    };

    /* Value for one column on one row/card. */
    TableFilters.prototype.valueOf = function (el, col) {
        var attr = el.getAttribute('data-filter-' + col);
        if (attr !== null) return attr;
        if (el.tagName === 'TR') {
            var idx = this.colIndex[col];
            if (idx === undefined) return null;
            var cell = el.querySelectorAll('td')[idx];
            if (!cell) return null;
            return (cell.textContent || '').trim();
        }
        return null;  // card without the attribute — this column can't filter it
    };

    /* Distinct values currently in the column, for the checkbox list. */
    TableFilters.prototype.distinct = function (col) {
        var self = this;
        var rows = document.querySelectorAll(this.rowSelector);
        var seen = {};
        rows.forEach(function (row) {
            var v = self.valueOf(row, col);
            if (v === null) return;
            var key = v === '' ? '(blank)' : v;
            seen[key] = true;
        });
        return Object.keys(seen).sort(function (a, b) {
            return a.localeCompare(b, undefined, { numeric: true });
        });
    };

    /* True when the element satisfies every active column filter. */
    TableFilters.prototype.passes = function (el) {
        for (var col in this.active) {
            if (!Object.prototype.hasOwnProperty.call(this.active, col)) continue;
            var allowed = this.active[col];
            if (!allowed || !allowed.size) continue;
            var v = this.valueOf(el, col);
            if (v === null) continue;  // column absent here — don't judge it
            if (!allowed.has(v === '' ? '(blank)' : v)) return false;
        }
        return true;
    };

    TableFilters.prototype.hasFilters = function () {
        for (var col in this.active) {
            if (this.active[col] && this.active[col].size) return true;
        }
        return false;
    };

    TableFilters.prototype.clear = function () {
        this.active = {};
        this._markHeaders();
        this.onChange();
    };

    TableFilters.prototype._markHeaders = function () {
        var self = this;
        this.table.querySelectorAll('thead th[data-col]').forEach(function (th) {
            var col = th.getAttribute('data-col');
            var on = !!(self.active[col] && self.active[col].size);
            th.classList.toggle('col-filtered', on);
            var btn = th.querySelector('.col-filter-btn');
            if (btn) btn.textContent = on ? '▾*' : '▾';
        });
    };

    TableFilters.prototype.closeMenu = function () {
        if (this.menu) { this.menu.remove(); this.menu = null; }
        this.openCol = null;
    };

    TableFilters.prototype.toggleMenu = function (col, th) {
        if (this.openCol === col) { this.closeMenu(); return; }
        this.closeMenu();
        var self = this;
        this.openCol = col;

        var menu = document.createElement('div');
        menu.className = 'col-filter-menu';
        menu.onclick = function (e) { e.stopPropagation(); };

        // --- sort ---
        var sortWrap = document.createElement('div');
        sortWrap.className = 'col-filter-sort';
        [['↑ Sort ascending', 1], ['↓ Sort descending', -1]].forEach(function (pair) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'col-filter-action';
            b.textContent = pair[0];
            b.onclick = function () {
                if (self.onSort) self.onSort(col, pair[1]);
                self.closeMenu();
            };
            sortWrap.appendChild(b);
        });
        menu.appendChild(sortWrap);

        // --- value list ---
        var values = this.distinct(col);
        var allowed = this.active[col];

        var search = document.createElement('input');
        search.type = 'search';
        search.className = 'col-filter-search';
        search.placeholder = 'Filter values…';
        menu.appendChild(search);

        var list = document.createElement('div');
        list.className = 'col-filter-list';
        menu.appendChild(list);

        function render(term) {
            list.innerHTML = '';
            var shown = values.filter(function (v) {
                return !term || v.toLowerCase().indexOf(term) !== -1;
            });
            if (!shown.length) {
                var none = document.createElement('div');
                none.className = 'col-filter-empty';
                none.textContent = 'No matching values';
                list.appendChild(none);
                return;
            }
            shown.forEach(function (v) {
                var label = document.createElement('label');
                label.className = 'col-filter-item';
                var cb = document.createElement('input');
                cb.type = 'checkbox';
                // No filter set means everything is included.
                cb.checked = !allowed || !allowed.size || allowed.has(v);
                cb.onchange = function () {
                    if (!self.active[col]) self.active[col] = new Set(values);
                    allowed = self.active[col];
                    if (cb.checked) allowed.add(v); else allowed.delete(v);
                    // Everything ticked is the same as no filter at all.
                    if (allowed.size === values.length) delete self.active[col];
                    self._markHeaders();
                    self.onChange();
                };
                var span = document.createElement('span');
                span.textContent = v;
                label.appendChild(cb);
                label.appendChild(span);
                list.appendChild(label);
            });
        }
        render('');
        search.oninput = function () { render(search.value.toLowerCase()); };

        // --- footer ---
        var footer = document.createElement('div');
        footer.className = 'col-filter-footer';
        var selAll = document.createElement('button');
        selAll.type = 'button';
        selAll.className = 'col-filter-action';
        selAll.textContent = 'Select all';
        selAll.onclick = function () {
            delete self.active[col];
            allowed = null;
            self._markHeaders();
            render(search.value.toLowerCase());
            self.onChange();
        };
        var selNone = document.createElement('button');
        selNone.type = 'button';
        selNone.className = 'col-filter-action';
        selNone.textContent = 'Clear';
        selNone.onclick = function () {
            self.active[col] = new Set();
            allowed = self.active[col];
            self._markHeaders();
            render(search.value.toLowerCase());
            self.onChange();
        };
        footer.appendChild(selAll);
        footer.appendChild(selNone);
        menu.appendChild(footer);

        document.body.appendChild(menu);
        var r = th.getBoundingClientRect();
        menu.style.top = (window.scrollY + r.bottom + 2) + 'px';
        // Keep the menu on screen when the column is near the right edge.
        var left = window.scrollX + r.left;
        var overflow = (left + menu.offsetWidth) - (window.scrollX + window.innerWidth - 8);
        if (overflow > 0) left -= overflow;
        menu.style.left = Math.max(8, left) + 'px';
        this.menu = menu;
        search.focus();
    };

    TableFilters.prototype._installGlobalHandlers = function () {
        var self = this;
        document.addEventListener('click', function () { self.closeMenu(); });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') self.closeMenu();
        });
    };

    window.TableFilters = TableFilters;
})();
