import { api, applyTheme, confirmDialog, el, fmtDate, getTheme, loadRefs, run, state, THEMES, toast } from './api.js';

export async function render(root) {
    const current = el('input', { type: 'password', autocomplete: 'current-password', required: true });
    const next = el('input', { type: 'password', autocomplete: 'new-password', required: true, minlength: 8 });
    const repeat = el('input', { type: 'password', autocomplete: 'new-password', required: true });
    const save = el('button', { class: 'btn', type: 'submit' }, 'Change password');
    const form = el('form', { onsubmit: e => {
        e.preventDefault();
        if (next.value !== repeat.value) { toast('The new passwords do not match', { error: true }); return; }
        run(save, async () => {
            await api('/auth/password', { method: 'POST', body: { current_password: current.value, new_password: next.value } });
            form.reset();
            toast('Password changed');
        });
    } },
    el('div', { class: 'form-group' }, el('label', {}, 'Current password'), current),
    el('div', { class: 'form-group' }, el('label', {}, 'New password (min. 8 characters)'), next),
    el('div', { class: 'form-group' }, el('label', {}, 'Repeat new password'), repeat),
    save);

    const themeBox = el('div', { class: 'row' });
    const renderThemes = () => {
        themeBox.replaceChildren(...THEMES.map(([id, name, desc]) => el('button', {
            class: `chip ${getTheme() === id ? 'active' : ''}`,
            title: desc,
            onclick: () => { applyTheme(id); renderThemes(); },
        }, name)));
    };
    renderThemes();

    root.append(el('div', { class: 'card' },
        el('h2', {}, 'Appearance'),
        el('p', { class: 'muted', style: { marginBottom: '12px' } }, 'Pick a style. It is saved in this browser.'),
        themeBox));
    root.append(el('div', { class: 'grid-2' },
        el('div', { class: 'card' }, el('h2', {}, `Account: ${state.user.username}`),
            state.user.has_password
                ? form
                : el('p', { class: 'muted' }, 'You sign in with single sign-on, so your password is managed by your identity provider.')),
        el('div', { class: 'card' },
            el('h2', {}, 'Your data'),
            el('p', { class: 'muted', style: { marginBottom: '16px' } },
                'Download a full backup of your accounts, categories, rules, budgets and transactions as JSON, ',
                'or export transactions for a spreadsheet.'),
            el('div', { class: 'row' },
                el('a', { class: 'btn', href: '/api/export/json' }, 'Download JSON backup'),
                el('a', { class: 'btn-light', href: '/api/export/xlsx' }, 'All transactions (Excel)'),
                el('a', { class: 'btn-light', href: '/api/export/csv' }, 'All transactions (CSV)')))));
    root.append(restoreCard());
    root.append(await cleanupCard());
}

const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;

const COUNT_LABELS = [
    ['accounts', 'accounts'], ['categories', 'categories'], ['transactions', 'transactions'],
    ['rules', 'rules'], ['budgets', 'budgets'], ['recurring', 'subscriptions'],
];

function restoreCard() {
    const fileInput = el('input', { type: 'file', accept: '.json,application/json', id: 'restoreFile' });
    const result = el('div');
    const card = el('div', { class: 'card' },
        el('h2', {}, 'Restore from backup'),
        el('p', { class: 'muted', style: { marginBottom: '12px' } },
            'Load a JSON backup downloaded above, for example to move to a new server. ',
            'This replaces all of your accounts, categories, transactions, rules, budgets and subscriptions. ',
            'Other users are not affected. Saved CSV column settings are not part of the backup.'),
        el('div', { class: 'form-group' }, el('label', { for: 'restoreFile' }, 'Backup file'), fileInput),
        result);

    const upload = (dryRun) => {
        const form = new FormData();
        form.append('file', fileInput.files[0]);
        return api('/export/restore', { method: 'POST', form, query: { dry_run: dryRun } });
    };

    fileInput.addEventListener('change', () => {
        result.replaceChildren();
        if (!fileInput.files[0]) return;
        run(null, async () => {
            let check;
            try {
                check = await upload(true);
            } catch (e) {
                result.replaceChildren(el('div', { class: 'status error' }, e.message));
                return;
            }
            const b = check.backup, cur = check.current;
            const restoreBtn = el('button', { class: 'btn-danger' }, 'Replace my data with this backup');
            restoreBtn.addEventListener('click', async () => {
                const ok = await confirmDialog(
                    `Replace your ${cur.transactions} transactions and all other data with the ${b.transactions} transactions from this backup? `
                    + 'Download a backup of your current data first if you might need it.',
                    { danger: true, confirmLabel: 'Restore', requireText: 'RESTORE' });
                if (!ok) return;
                await run(restoreBtn, async () => {
                    await upload(false);
                    await loadRefs();
                    result.replaceChildren(el('div', { class: 'status success' },
                        `Restored ${plural(b.transactions, 'transaction')}, ${plural(b.accounts, 'account')} and ${plural(b.categories, 'category', 'categories')}.`));
                    fileInput.value = '';
                    toast('Backup restored');
                });
            });
            result.replaceChildren(
                el('div', { class: 'status info' },
                    'Backup', b.username ? ` of “${b.username}”` : '', b.exported_at ? ` from ${fmtDate(b.exported_at)}` : '', ' is valid.'),
                el('table', { class: 'data', style: { margin: '12px 0', maxWidth: '520px' } },
                    el('thead', {}, el('tr', {}, el('th', {}, 'Contents'), el('th', { class: 'num' }, 'In the backup'), el('th', { class: 'num' }, 'Your data now'))),
                    el('tbody', {}, COUNT_LABELS.map(([key, label]) => el('tr', {},
                        el('td', {}, label), el('td', { class: 'num' }, b[key]), el('td', { class: 'num' }, cur[key]))))),
                el('div', { class: 'row' },
                    restoreBtn,
                    el('a', { class: 'btn-light', href: '/api/export/json' }, 'Download current data first')));
        });
    });
    return card;
}

async function cleanupCard() {
    const body = el('div');
    const card = el('div', { class: 'card' },
        el('h2', {}, 'Clean up'),
        el('p', { class: 'muted', style: { marginBottom: '12px' } },
            'Start over with rules or subscriptions. Your transactions are not deleted, and they keep their categories.'),
        body);

    const load = async () => {
        const [rules, series] = await Promise.all([api('/rules'), api('/recurring')]);
        const row = (title, hint, count, noun, path) => {
            const btn = el('button', { class: 'btn-danger btn-sm', disabled: count === 0 }, `Delete all ${noun}`);
            btn.addEventListener('click', async () => {
                const ok = await confirmDialog(`Delete all ${count} ${noun}? This cannot be undone.`,
                    { danger: true, confirmLabel: `Delete all ${noun}`, requireText: 'DELETE' });
                if (!ok) return;
                await run(btn, async () => {
                    const r = await api(path, { method: 'DELETE', query: { confirm: 'DELETE' } });
                    toast(`Deleted ${plural(r.deleted, noun.replace(/s$/, ''), noun)}`);
                    await load();
                });
            });
            return el('div', { class: 'spread', style: { padding: '12px 0', borderTop: '1px solid var(--border)', marginBottom: 0 } },
                el('div', {}, el('strong', {}, title), el('span', { class: 'muted' }, ` · ${count}`),
                    el('div', { class: 'muted small' }, hint)),
                btn);
        };
        body.replaceChildren(
            row('Auto-categorization rules', 'New imports are no longer categorized automatically until you add rules again.',
                rules.length, 'rules', '/rules'),
            row('Subscriptions & recurring payments', 'Includes kept and dismissed ones. The next import or “Scan again” suggests them again from your transactions.',
                series.length, 'subscriptions', '/recurring'));
    };
    await load();
    return card;
}
