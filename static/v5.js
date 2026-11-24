/* PyBank 5 - search suggestions, name lookup on transfer forms and the "Ask PyBank" chat. No library. */
(function () {
    'use strict';
    var csrf = (document.querySelector('meta[name=csrf-token]') || {}).content || '';

    function debounce(fn, ms) {
        var t;
        return function () { var a = arguments, self = this; clearTimeout(t); t = setTimeout(function () { fn.apply(self, a); }, ms); };
    }
    function el(tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) { e.className = cls; }
        if (text !== undefined) { e.textContent = text; }
        return e;
    }

    /* ---------- 1. suggestions under every search box with data-suggest ---------- */
    document.querySelectorAll('input[data-suggest]').forEach(function (input) {
        var box = el('ul', 'suggest-list');
        box.setAttribute('role', 'listbox');
        box.hidden = true;
        input.parentNode.style.position = 'relative';
        input.parentNode.appendChild(box);
        input.setAttribute('aria-autocomplete', 'list');
        var active = -1;
        function close() { box.hidden = true; active = -1; }
        function pick(item) {
            if (item.url) { window.location = item.url; return; }
            input.value = item.text;
            close();
            if (input.form) { input.form.submit(); }
        }
        var load = debounce(function () {
            var q = input.value.trim();
            if (q.length < 2) { close(); return; }
            fetch(input.dataset.suggest + '?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
                .then(function (r) { return r.ok ? r.json() : []; })
                .then(function (items) {
                    box.innerHTML = '';
                    active = -1;
                    (items || []).forEach(function (it) {
                        var li = el('li');
                        li.setAttribute('role', 'option');
                        li.appendChild(el('span', '', it.text));
                        if (it.kind && it.kind !== 'word') { li.appendChild(el('small', '', it.kind)); }
                        li.addEventListener('mousedown', function (e) { e.preventDefault(); pick(it); });
                        li._item = it;
                        box.appendChild(li);
                    });
                    box.hidden = !box.children.length;
                }).catch(close);
        }, 200);
        input.addEventListener('input', load);
        input.addEventListener('blur', function () { setTimeout(close, 150); });
        input.addEventListener('keydown', function (e) {
            var items = box.children;
            if (box.hidden || !items.length) { return; }
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                if (active >= 0) { items[active].classList.remove('active'); }
                active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
                items[active].classList.add('active');
            } else if (e.key === 'Enter' && active >= 0) {
                e.preventDefault();
                pick(items[active]._item);
            } else if (e.key === 'Escape') {
                close();
            }
        });
    });

    /* ---------- 2. name lookup: the account holder's name appears after the number is typed ---------- */
    var NE_FIELDS = ['account', 'to_account', 'ifsc', 'upi', 'bic', 'iban'];
    document.querySelectorAll('form[data-ne]').forEach(function (form) {
        var out = form.querySelector('.ne-box');
        if (!out) { return; }
        var last = '';
        function rail() {
            if (form.dataset.neRail) { return form.dataset.neRail; }
            var r = form.querySelector('input[name=rail]:checked') || form.querySelector('select[name=rail]');
            return r ? r.value : '';
        }
        function visible(name) {
            var list = form.querySelectorAll('[name="' + name + '"]');
            for (var i = 0; i < list.length; i++) { if (!list[i].disabled) { return list[i]; } }
            return null;
        }
        function show(cls, html) { out.className = 'ne-box ' + cls; out.innerHTML = ''; if (html) { out.appendChild(html); } }
        function fill(sel, value) {
            form.querySelectorAll(sel).forEach(function (f) {
                if (f.disabled) { return; }
                if (!f.value.trim() || f.dataset.neFilled === '1') { f.value = value; f.dataset.neFilled = '1'; }
            });
        }
        var ask = debounce(function () {
            var r = rail();
            var params = new URLSearchParams({ rail: r });
            var marked = form.querySelectorAll('[data-ne-field]');
            if (marked.length) {                        // the form says which fields are for the lookup
                marked.forEach(function (f) {
                    if (!f.disabled && f.value.trim()) { params.set(f.dataset.neField, f.value.trim()); }
                });
            } else {
                NE_FIELDS.forEach(function (n) {
                    var f = visible(n);
                    if (f && f.value.trim()) { params.set(n === 'to_account' ? 'account' : n, f.value.trim()); }
                });
            }
            var acc = params.get('account') || '';
            var key = params.toString();
            if (key === last) { return; }
            if ((r === 'PYBANK' || r === '') && acc.replace(/\D/g, '').length < 12) { show('', null); last = ''; return; }
            if (r === 'UPI' ? !(params.get('upi') || '').includes('@') : acc.replace(/\s/g, '').length < 6) { show('', null); last = ''; return; }
            last = key;
            var nameField = form.querySelector('[data-ne-name]:not([disabled])');
            if (nameField && nameField.value.trim() && nameField.dataset.neFilled !== '1') { params.set('name', nameField.value.trim()); }
            show('wait', el('span', '', 'Looking up the account holder…'));
            fetch(form.dataset.ne + '?' + params.toString(), { credentials: 'same-origin' })
                .then(function (res) { return res.json(); })
                .then(function (d) {
                    if (!d.ok) { show('bad', el('span', '', d.error || 'Account not found.')); return; }
                    var wrap = el('div');
                    wrap.appendChild(el('span', 'ne-label', 'Account holder'));
                    wrap.appendChild(el('strong', 'ne-name', d.name));
                    var meta = [d.bank, d.branch, d.currency, d.network].filter(Boolean).join(' · ');
                    if (meta) { wrap.appendChild(el('small', '', meta)); }
                    if (d.own) { wrap.appendChild(el('small', 'ne-warn', 'This is one of your own accounts.')); }
                    if (d.status && d.status !== 'Active') { wrap.appendChild(el('small', 'ne-warn', 'Account status: ' + d.status)); }
                    if (d.check === 'no') { wrap.appendChild(el('small', 'ne-warn', 'The name you typed is different from the name at the bank. Check before you send.')); }
                    else if (d.check === 'close') { wrap.appendChild(el('small', '', 'The name you typed is close to the name at the bank.')); }
                    show(d.check === 'no' ? 'warn' : 'ok', wrap);
                    fill('[data-ne-name]', d.name);
                    if (d.bank) { fill('[data-ne-bank]', d.bank); }
                })
                .catch(function () { show('bad', el('span', '', 'The lookup did not answer. You can still continue.')); });
        }, 450);
        form.addEventListener('input', function (e) {
            if (e.target.hasAttribute('data-ne-name')) { e.target.dataset.neFilled = '0'; return; }
            if (e.target.hasAttribute('data-ne-field') || (!form.querySelector('[data-ne-field]') && NE_FIELDS.indexOf(e.target.name) >= 0)) { ask(); }
        });
        form.addEventListener('change', function (e) { if (e.target.name === 'rail') { last = ''; show('', null); ask(); } });
        ask();
    });

    /* ---------- 3. Ask PyBank chat (public widget, Net Banking and back office) ---------- */
    function chat(root) {
        var url = root.dataset.chat || (root.closest('[data-chat]') || {}).dataset.chat;
        var log = root.querySelector('.chat-log');
        var form = root.querySelector('.chat-form');
        if (!url || !log || !form) { return; }
        function add(who, node) {
            var m = el('div', 'chat-msg ' + who);
            if (typeof node === 'string') { m.textContent = node; } else { m.appendChild(node); }
            log.appendChild(m);
            log.scrollTop = log.scrollHeight;
            return m;
        }
        function send(q) {
            if (!q) { return; }
            add('me', q);
            var wait = add('bot wait', '…');
            fetch(url, {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                body: JSON.stringify({ q: q })
            }).then(function (r) { return r.json(); }).then(function (d) {
                wait.remove();
                var box = el('div');
                (d.text || '').split('\n').forEach(function (line, i) {
                    if (i) { box.appendChild(el('br')); }
                    box.appendChild(document.createTextNode(line));
                });
                if (d.items && d.items.length) {
                    var ul = el('ul', 'chat-items');
                    d.items.slice(0, 10).forEach(function (it) {
                        var li = el('li');
                        if (typeof it === 'string') { li.textContent = it; }
                        else {
                            var a = el(it.url ? 'a' : 'span', '', it.title || it.text || '');
                            if (it.url) { a.href = it.url; }
                            li.appendChild(a);
                            if (it.detail) { li.appendChild(el('small', '', ' ' + it.detail)); }
                        }
                        ul.appendChild(li);
                    });
                    box.appendChild(ul);
                }
                if (d.links && d.links.length) {
                    var p = el('p', 'chat-links');
                    d.links.slice(0, 4).forEach(function (l) {
                        var a = el('a', 'chip', l.title || l.text || l.url);
                        a.href = l.url;
                        p.appendChild(a);
                    });
                    box.appendChild(p);
                }
                add('bot', box);
            }).catch(function () { wait.remove(); add('bot', 'Sorry, I could not answer just now. Please try again.'); });
        }
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            var i = form.querySelector('input[name=q]');
            var q = i.value.trim();
            i.value = '';
            send(q);
        });
        root.querySelectorAll('[data-ask]').forEach(function (b) { b.addEventListener('click', function () { send(b.dataset.ask); }); });
        if (root.dataset.first) { send(root.dataset.first); }
    }
    document.querySelectorAll('.chat').forEach(chat);
    document.querySelectorAll('.chat-toggle').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var panel = document.getElementById(btn.getAttribute('aria-controls'));
            panel.hidden = !panel.hidden;
            btn.setAttribute('aria-expanded', String(!panel.hidden));
            if (!panel.hidden) { panel.querySelector('input').focus(); }
        });
    });
})();
