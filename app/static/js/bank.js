// Bank connections (FinTS/HBCI) on the Accounts page, and the dialog that follows a sync job.
import {
    accountOptions, api, clear, confirmDialog, el, fmtDate, loadRefs, modal, options, run, state, toast,
} from './api.js';

const STATUS = {
    new: ['Not synced yet', ''],
    ok: ['OK', 'pos'],
    error: ['Error', 'neg'],
    tan_required: ['Needs your confirmation', 'neg'],
    pin_error: ['PIN rejected', 'neg'],
};

export async function bankSection(onAccountsChanged) {
    const cfg = await api('/bank/config');
    const box = el('div');
    const card = el('div', { class: 'card' },
        el('div', { class: 'spread' },
            el('h2', {}, 'Bank connections'),
            el('button', { class: 'btn', disabled: !cfg.product_id_configured, onclick: async () => {
                const created = await addConnection(cfg);
                if (!created) return;
                await reload();
                await followJob(created.job);
                reload();
            } }, '+ Connect a bank')),
        el('p', { class: 'muted', style: { marginBottom: '16px' } },
            'Fetch transactions directly from your bank via FinTS/HBCI (Sparkassen, Volks- und Raiffeisenbanken, DKB, ING, comdirect and many more). ',
            'New transactions go through the same duplicate detection and rules as CSV imports.'),
        cfg.product_id_configured ? null : el('div', { class: 'status info', style: { marginTop: 0, marginBottom: '16px' } },
            'Bank sync is not set up on this server yet: the administrator needs to add a FinTS product registration number ',
            '(FINTS_PRODUCT_ID) to the .env file. Registration is free at fints.org.'),
        box);

    async function reload() {
        const conns = await api('/bank/connections');
        clear(box);
        if (!conns.length) {
            box.append(el('p', { class: 'muted' }, 'No bank connected yet.'));
            return;
        }
        for (const c of conns) box.append(connectionBlock(c, cfg, reload, onAccountsChanged));
    }
    await reload();
    return card;
}

function connectionBlock(c, cfg, reload, onAccountsChanged) {
    const [statusText, statusCls] = STATUS[c.last_status] || [c.last_status, ''];
    const syncBtn = el('button', { class: 'btn btn-sm', onclick: () => startSync(c, cfg).then(() => { reload(); onAccountsChanged(); }) }, 'Sync now');
    const autoToggle = el('input', { type: 'checkbox', checked: c.auto_sync, disabled: !c.pin_stored || cfg.sync_interval_hours <= 0 });
    autoToggle.addEventListener('change', () => run(null, async () => {
        await api(`/bank/connections/${c.id}`, { method: 'PATCH', body: { auto_sync: autoToggle.checked } });
        reload();
    }));

    const links = el('table', { class: 'data', style: { marginTop: '10px' } },
        el('thead', {}, el('tr', {}, ['Bank account', 'Syncs into', 'Sync from', 'Last synced'].map(h => el('th', {}, h)))),
        el('tbody', {}, c.links.map(lk => linkRow(lk, reload, onAccountsChanged))));

    return el('div', { style: { borderTop: '1px solid var(--border)', padding: '16px 0' } },
        el('div', { class: 'spread', style: { marginBottom: '6px' } },
            el('div', {},
                el('strong', {}, c.name), el('span', { class: 'muted small' }, `  BLZ ${c.blz} · ${c.login_name}`),
                el('div', { class: 'small' },
                    el('span', { class: statusCls }, statusText),
                    c.last_sync_at ? el('span', { class: 'muted' }, ` · ${new Date(c.last_sync_at).toLocaleString()}`) : null,
                    c.last_message ? el('span', { class: 'muted' }, ` · ${c.last_message}`) : null)),
            el('div', { class: 'row' },
                syncBtn,
                el('button', { class: 'btn-light btn-sm', onclick: async () => {
                    const r = await editConnection(c, cfg);
                    if (r?.job) await followJob(r.job);
                    if (r) reload();
                } }, 'Settings'),
                el('button', { class: 'btn-danger btn-sm', onclick: async () => {
                    if (!await confirmDialog(`Remove the connection “${c.name}”? Transactions that were already imported stay.`, { danger: true, confirmLabel: 'Remove' })) return;
                    run(null, async () => { await api(`/bank/connections/${c.id}`, { method: 'DELETE' }); reload(); });
                } }, 'Remove'))),
        el('label', { class: 'inline-label small', title: c.pin_stored ? '' : 'Store the PIN (Settings) to allow automatic sync' },
            autoToggle,
            cfg.sync_interval_hours > 0
                ? `Sync automatically every ${cfg.sync_interval_hours} hours${c.pin_stored ? '' : ' (needs a stored PIN)'}`
                : 'Automatic sync is turned off on this server'),
        c.links.length ? links : el('p', { class: 'muted small' }, 'No accounts loaded yet. Click “Sync now”.'));
}

function linkRow(lk, reload, onAccountsChanged) {
    const select = el('select', { style: { minWidth: '200px' } },
        el('option', { value: '' }, '— not synced —'),
        el('option', { value: 'new' }, '+ Create a new account'),
        accountOptions(lk.account_id ?? ''));
    select.value = lk.account_id ? String(lk.account_id) : '';
    select.addEventListener('change', () => run(null, async () => {
        const body = select.value === 'new' ? { create_account: true } : { account_id: select.value ? Number(select.value) : null };
        await api(`/bank/links/${lk.id}`, { method: 'PATCH', body });
        await loadRefs();
        onAccountsChanged();
        reload();
    }));
    const from = el('input', { type: 'date', value: lk.sync_from || '', disabled: !lk.account_id, style: { width: 'auto' } });
    from.addEventListener('change', () => run(null, async () => {
        await api(`/bank/links/${lk.id}`, { method: 'PATCH', body: { sync_from: from.value } });
        reload();
    }));
    return el('tr', {},
        el('td', {}, lk.iban || lk.account_number),
        el('td', {}, select),
        el('td', { title: 'Transactions from this date on are fetched. It starts after your newest existing transaction so CSV imports and bank sync do not overlap. More than 90 days back usually needs a TAN.' }, from),
        el('td', {}, lk.synced_until ? fmtDate(lk.synced_until) : el('span', { class: 'muted' }, 'never')));
}

// ---------------------------------------------------------------- dialogs

function pinFields(cfg) {
    const pin = el('input', { type: 'password', autocomplete: 'off', required: true, id: 'bankPin' });
    const store = el('input', { type: 'checkbox', disabled: !cfg.can_store_pin, id: 'bankStorePin' });
    const storeLabel = el('label', { class: 'inline-label small', for: 'bankStorePin' }, store,
        cfg.can_store_pin
            ? 'Store the PIN encrypted on this server (needed for automatic sync)'
            : 'Storing the PIN needs SECRET_KEY in the server’s .env');
    return { pin, store, nodes: [el('div', { class: 'form-group' }, el('label', { for: 'bankPin' }, 'Online-banking PIN'), pin), el('div', { class: 'form-group' }, storeLabel)] };
}

function addConnection(cfg) {
    return modal('Connect a bank', close => {
        const name = el('input', { required: true, value: 'Sparkasse', id: 'bankName' });
        const blz = el('input', { required: true, inputmode: 'numeric', placeholder: '8 digits, e.g. 12030000', id: 'bankBlz' });
        const url = el('input', { required: true, type: 'url', placeholder: 'https://…/fints30', id: 'bankUrl' });
        const login = el('input', { required: true, autocomplete: 'off', id: 'bankLogin' });
        const p = pinFields(cfg);
        const save = el('button', { class: 'btn', type: 'submit' }, 'Connect');
        const g = (label, input, hint) => el('div', { class: 'form-group' }, el('label', { for: input.id }, label), input, hint ? el('p', { class: 'muted small', style: { marginTop: '4px' } }, hint) : null);
        return el('form', { onsubmit: e => {
            e.preventDefault();
            run(save, async () => {
                close(await api('/bank/connections', { method: 'POST', body: {
                    name: name.value.trim(), blz: blz.value.trim(), server_url: url.value.trim(),
                    login_name: login.value.trim(), pin: p.pin.value, store_pin: p.store.checked,
                } }));
            });
        } },
        g('Name', name),
        el('div', { class: 'grid-2', style: { gap: '0 16px' } },
            g('Bank code (BLZ)', blz), g('Login name / Anmeldename', login, 'As in online banking (Sparkasse: Anmeldename or Legitimations-ID).')),
        g('FinTS server address', url, 'Your bank publishes it in its online-banking help (search for “FinTS” or “HBCI PIN/TAN”). Sparkassen use addresses like https://banking-xx…/fints30.'),
        ...p.nodes,
        el('div', { class: 'actions' }, el('button', { type: 'button', class: 'btn-light', onclick: () => close(false) }, 'Cancel'), save));
    });
}

function editConnection(c, cfg) {
    return modal(`Settings: ${c.name}`, close => {
        const name = el('input', { required: true, value: c.name });
        const url = el('input', { required: true, type: 'url', value: c.server_url });
        const p = pinFields(cfg);
        p.pin.required = false;
        p.pin.placeholder = c.pin_stored ? 'A PIN is stored. Enter a new one to replace it.' : 'Optional: enter to store it';
        const save = el('button', { class: 'btn', type: 'submit' }, 'Save');
        return el('form', { onsubmit: e => {
            e.preventDefault();
            run(save, async () => {
                await api(`/bank/connections/${c.id}`, { method: 'PATCH', body: { name: name.value.trim(), server_url: url.value.trim() } });
                if (p.pin.value && p.store.checked) {
                    // Storing a new PIN is verified right away with a sync.
                    close({ job: await api(`/bank/connections/${c.id}/sync`, { method: 'POST', body: { pin: p.pin.value, store_pin: true } }) });
                    return;
                }
                close({});
            });
        } },
        el('div', { class: 'form-group' }, el('label', {}, 'Name'), name),
        el('div', { class: 'form-group' }, el('label', {}, 'FinTS server address'), url),
        ...p.nodes,
        c.pin_stored ? el('p', {}, el('a', { href: '#', onclick: e => {
            e.preventDefault();
            run(null, async () => { await api(`/bank/connections/${c.id}`, { method: 'PATCH', body: { forget_pin: true } }); toast('Stored PIN deleted'); close({}); });
        } }, 'Delete the stored PIN')) : null,
        el('div', { class: 'actions' }, el('button', { type: 'button', class: 'btn-light', onclick: () => close(null) }, 'Cancel'), save));
    });
}

async function startSync(c, cfg) {
    if (c.active_job) return followJob({ id: c.active_job });
    if (c.pin_stored) {
        return run(null, async () => followJob(await api(`/bank/connections/${c.id}/sync`, { method: 'POST', body: {} })));
    }
    const started = await modal(`Sync ${c.name}`, close => {
        const p = pinFields(cfg);
        const go = el('button', { class: 'btn', type: 'submit' }, 'Sync');
        return el('form', { onsubmit: e => {
            e.preventDefault();
            run(go, async () => close(await api(`/bank/connections/${c.id}/sync`, { method: 'POST', body: { pin: p.pin.value, store_pin: p.store.checked } })));
        } }, ...p.nodes, el('div', { class: 'actions' }, el('button', { type: 'button', class: 'btn-light', onclick: () => close(null) }, 'Cancel'), go));
    });
    if (started) await followJob(started);
}

/** Shows a running sync job until it is finished, answering TAN and choice questions. */
export function followJob(job) {
    return modal('Bank sync', close => {
        const body = el('div');
        let stopped = false;
        let lastKey = '';
        const cancelBtn = el('button', { type: 'button', class: 'btn-light' }, 'Cancel');
        const actions = el('div', { class: 'actions' }, cancelBtn);
        cancelBtn.addEventListener('click', async () => {
            if (['done', 'failed', 'cancelled'].includes(job.state)) { stopped = true; close(job); return; }
            await api(`/bank/jobs/${job.id}/cancel`, { method: 'POST' }).catch(() => {});
        });

        const render = j => {
            // Only rebuild the form when the question changes, so typing is not interrupted.
            const key = `${j.state}|${j.message}|${j.challenge?.text || ''}`;
            if (key === lastKey) return;
            lastKey = key;
            clear(body);
            if (j.state === 'running') {
                body.append(el('p', {}, '⏳ ', j.message));
            } else if (j.state === 'need_choice') {
                const sel = el('select', {}, options(j.choice.options.map(o => [o.id, o.label]), j.choice.options[0]?.id));
                const ok = el('button', { class: 'btn' }, 'Continue');
                ok.addEventListener('click', () => run(ok, () => api(`/bank/jobs/${j.id}/answer`, { method: 'POST', body: { value: sel.value } })));
                body.append(el('p', { style: { marginBottom: '10px' } }, j.choice.title), sel,
                    el('p', { class: 'muted small', style: { margin: '8px 0' } }, 'For a Sparkasse with the S-pushTAN app choose “pushTAN”. The choice is remembered.'),
                    ok);
            } else if (j.state === 'need_tan') {
                const ch = j.challenge || {};
                body.append(el('p', { style: { whiteSpace: 'pre-line', marginBottom: '10px' } }, ch.text || j.message));
                if (ch.image) body.append(el('img', { src: ch.image, alt: 'TAN challenge', style: { maxWidth: '240px', display: 'block', margin: '0 auto 12px' } }));
                if (ch.decoupled) {
                    body.append(el('div', { class: 'status info' }, '📱 ', j.message, ' This window continues automatically.'));
                } else {
                    if (ch.flicker) body.append(el('p', { class: 'muted small' }, 'Flicker codes cannot be shown here; please use chipTAN manuell or an app-based method.'));
                    const tan = el('input', { inputmode: 'numeric', autocomplete: 'one-time-code', placeholder: 'TAN' });
                    const ok = el('button', { class: 'btn' }, 'Send TAN');
                    ok.addEventListener('click', () => run(ok, () => api(`/bank/jobs/${j.id}/answer`, { method: 'POST', body: { value: tan.value } })));
                    tan.addEventListener('keydown', e => { if (e.key === 'Enter') ok.click(); });
                    body.append(el('div', { class: 'row' }, el('div', { class: 'grow' }, tan), ok));
                    setTimeout(() => tan.focus(), 0);
                }
            } else {
                const cls = j.state === 'done' ? 'success' : 'error';
                body.append(el('div', { class: `status ${cls}`, style: { marginTop: 0 } }, j.message));
                const r = j.result;
                if (r?.accounts?.length) {
                    body.append(el('table', { class: 'data', style: { marginTop: '10px' } },
                        el('thead', {}, el('tr', {}, ['Account', 'New', 'Already known'].map(h => el('th', {}, h)))),
                        el('tbody', {}, r.accounts.map(a => el('tr', {}, el('td', {}, a.account), el('td', { class: 'num' }, a.imported), el('td', { class: 'num' }, a.duplicates))))));
                }
                if (r && j.kind === 'connect') {
                    body.append(el('p', { class: 'muted small', style: { marginTop: '10px' } },
                        'Next: choose below which Proud Ledger account each bank account syncs into, then click “Sync now”.'));
                }
                cancelBtn.textContent = 'Close';
            }
        };

        const poll = async () => {
            while (!stopped) {
                try {
                    job = await api(`/bank/jobs/${job.id}`);
                } catch (e) {
                    job = { ...job, state: 'failed', message: e.message };
                }
                render(job);
                if (['done', 'failed', 'cancelled'].includes(job.state)) {
                    if (job.state === 'done' && job.result?.imported) loadRefs().catch(() => {});
                    return;
                }
                await new Promise(res => setTimeout(res, 1500));
            }
        };
        render(job);
        poll();
        return el('div', {}, body, actions);
    });
}
