/* PyBank 6 - the chat assistant (history, confirm cards, suggestions, feedback), ability tick boxes,
   table filters, note counting for the cash drawer and nominee shares. No library. */
(function () {
    'use strict';
    var csrf = (document.querySelector('meta[name=csrf-token]') || {}).content || '';

    function el(tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) { e.className = cls; }
        if (text !== undefined && text !== null) { e.textContent = text; }
        return e;
    }
    function post(url, data) {
        return fetch(url, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify(data || {})
        }).then(function (r) { return r.json().catch(function () { return { text: 'Sorry, something went wrong.' }; }); });
    }

    /* ---------- 1. chat ---------- */
    function chat(root) {
        var url = root.dataset.chat;
        var log = root.querySelector('.chat-log');
        var form = root.querySelector('.chat-form');
        if (!url || !log || !form) { return; }
        var loaded = false;

        function scroll() { log.scrollTop = log.scrollHeight; }
        function bubble(who) { var m = el('div', 'chat-msg ' + who); log.appendChild(m); scroll(); return m; }

        function feedback(box, id, current) {
            var f = el('span', 'chat-fb');
            [[1, '👍', 'Good answer'], [-1, '👎', 'Bad answer']].forEach(function (o) {
                var b = el('button', 'fb' + (current === o[0] ? ' on' : ''), o[1]);
                b.type = 'button';
                b.setAttribute('aria-label', o[2]);
                b.addEventListener('click', function () {
                    post(url + '/feedback', { id: id, value: o[0] });
                    f.querySelectorAll('.fb').forEach(function (x) { x.classList.remove('on'); });
                    b.classList.add('on');
                });
                f.appendChild(b);
            });
            box.appendChild(f);
        }

        function confirmCard(box, id, c) {
            var card = el('form', 'chat-confirm');
            if (c.pin) {
                var pin = el('input');
                pin.type = 'password'; pin.name = 'pin'; pin.inputMode = 'numeric'; pin.maxLength = 6;
                pin.placeholder = 'PIN'; pin.required = true; pin.setAttribute('aria-label', 'PIN');
                card.appendChild(pin);
            }
            (c.fields || []).forEach(function (f) {
                var i = el('input');
                i.name = f.name || f; i.placeholder = f.label || f; i.required = true;
                i.setAttribute('aria-label', f.label || f);
                card.appendChild(i);
            });
            var ok = el('button', 'btn btn-primary btn-sm', c.button || 'Confirm');
            var no = el('button', 'btn btn-outline btn-sm', 'Cancel');
            no.type = 'button';
            card.appendChild(ok);
            card.appendChild(no);
            card.addEventListener('submit', function (e) {
                e.preventDefault();
                var data = { id: id };
                card.querySelectorAll('input').forEach(function (i) { data[i.name] = i.value; });
                ok.disabled = true;
                post(url + '/confirm', data).then(function (d) {
                    if (d.kind === 'error') { ok.disabled = false; show(d, true); return; }
                    card.remove();
                    show(d);
                });
            });
            no.addEventListener('click', function () { card.remove(); send('cancel'); });
            box.appendChild(card);
        }

        function show(d, isError) {
            var m = bubble('bot' + (isError ? ' error' : ''));
            (d.text || '').split('\n').forEach(function (line, i) {
                if (i) { m.appendChild(el('br')); }
                m.appendChild(document.createTextNode(line));
            });
            if (d.items && d.items.length) {
                var ul = el('ul', 'chat-items');
                d.items.slice(0, 10).forEach(function (it) {
                    var li = el('li');
                    if (typeof it === 'string') { li.textContent = it; } else {
                        var a = el(it.url ? 'a' : 'span', '', it.title || it.text || '');
                        if (it.url) { a.href = it.url; }
                        li.appendChild(a);
                        if (it.detail) { li.appendChild(el('small', '', ' ' + it.detail)); }
                    }
                    ul.appendChild(li);
                });
                m.appendChild(ul);
            }
            if (d.links && d.links.length) {
                var p = el('p', 'chat-links');
                d.links.slice(0, 4).forEach(function (l) {
                    var a = el('a', 'chip', l.title || l.url);
                    a.href = l.url;
                    p.appendChild(a);
                });
                m.appendChild(p);
            }
            if (d.confirm && d.id) { confirmCard(m, d.id, d.confirm); }
            if (d.suggestions && d.suggestions.length) {
                var s = el('p', 'chat-links chat-suggest');
                d.suggestions.slice(0, 4).forEach(function (q) {
                    var b = el('button', 'chip', q);
                    b.type = 'button';
                    b.addEventListener('click', function () { s.remove(); send(q); });
                    s.appendChild(b);
                });
                m.appendChild(s);
            }
            if (d.id && !isError && d.kind !== 'smalltalk') { feedback(m, d.id, d.feedback); }
            scroll();
        }

        function send(q) {
            if (!q) { return; }
            bubble('me').textContent = q;
            var wait = bubble('bot wait');
            wait.textContent = '…';
            post(url, { q: q }).then(function (d) { wait.remove(); show(d); })
                .catch(function () { wait.remove(); show({ text: 'Sorry, I could not answer just now. Please try again.' }, true); });
        }

        function history() {
            if (loaded) { return; }
            loaded = true;
            fetch(url + '/history', { credentials: 'same-origin' }).then(function (r) { return r.ok ? r.json() : []; })
                .then(function (rows) {
                    (rows || []).forEach(function (r) {
                        if (r.role === 'user') { bubble('me').textContent = r.text; } else { show(r); }
                    });
                    if (root.dataset.first) { send(root.dataset.first); }
                }).catch(function () {});
        }

        form.addEventListener('submit', function (e) {
            e.preventDefault();
            var i = form.querySelector('input[name=q]');
            var q = i.value.trim();
            i.value = '';
            send(q);
        });
        root.querySelectorAll('[data-ask]').forEach(function (b) { b.addEventListener('click', function () { send(b.dataset.ask); }); });
        var clear = root.querySelector('[data-chat-clear]');
        if (clear) {
            clear.addEventListener('click', function () {
                post(url + '/clear').then(function () {
                    log.querySelectorAll('.chat-msg:not(.hello)').forEach(function (m) { m.remove(); });
                });
            });
        }
        if (root.hidden) {
            var toggle = document.querySelector('[aria-controls="' + root.id + '"]');
            if (toggle) { toggle.addEventListener('click', history); }
        } else { history(); }
    }
    document.querySelectorAll('.chat[data-v6]').forEach(chat);

    /* ---------- 2. tick boxes of abilities ---------- */
    document.querySelectorAll('[data-tick]').forEach(function (b) {
        b.addEventListener('click', function () {
            var form = b.closest('form');
            var job = form.querySelector('select[name=job_role]');
            var role = job ? (job.selectedOptions[0].dataset.abilities || '').split(',') : [];
            form.querySelectorAll('input[name=abilities]').forEach(function (c) {
                c.checked = b.dataset.tick === 'all' ? true : b.dataset.tick === 'none' ? false : role.indexOf(c.value) >= 0;
            });
        });
    });
    document.querySelectorAll('select[name=job_role]').forEach(function (sel) {
        var form = sel.closest('form');
        if (!form || !form.querySelector('input[name=abilities]')) { return; }
        sel.addEventListener('change', function () {
            var role = (sel.selectedOptions[0].dataset.abilities || '').split(',');
            form.querySelectorAll('.ability-tick').forEach(function (l) {
                l.classList.toggle('in-role', role.indexOf(l.dataset.code) >= 0);
            });
        });
    });
    document.querySelectorAll('[data-tick-all]').forEach(function (b) {
        b.addEventListener('change', function () {
            b.closest('form').querySelectorAll('input[name="' + b.dataset.tickAll + '"]').forEach(function (c) { c.checked = b.checked; });
        });
    });

    /* ---------- 3. filters: type a word, hide what does not match ---------- */
    document.querySelectorAll('input[data-filter]').forEach(function (i) {
        i.addEventListener('input', function () {
            var q = i.value.trim().toLowerCase();
            document.querySelectorAll(i.dataset.filter).forEach(function (row) {
                row.hidden = q && row.textContent.toLowerCase().indexOf(q) < 0;
            });
        });
    });

    /* ---------- 4. cash drawer: notes x value = total ---------- */
    document.querySelectorAll('form[data-count-notes]').forEach(function (form) {
        var total = form.querySelector('[data-total]');
        form.addEventListener('input', function (e) {
            if (!e.target.dataset.value) { return; }
            var sum = 0;
            form.querySelectorAll('[data-value]').forEach(function (n) { sum += (parseInt(n.value, 10) || 0) * parseFloat(n.dataset.value); });
            total.value = sum ? sum.toFixed(2) : '';
        });
    });

    /* ---------- 5. nominee shares ---------- */
    document.querySelectorAll('form[data-shares]').forEach(function (form) {
        var out = form.querySelector('[data-share-total]');
        form.addEventListener('input', function () {
            var sum = 0;
            form.querySelectorAll('[data-share]').forEach(function (s) { sum += parseInt(s.value, 10) || 0; });
            out.textContent = sum;
            out.className = sum === 100 || sum === 0 ? '' : 'bad-text';
        });
    });
}());
