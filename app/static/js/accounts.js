import { api, centsToInput, clear, confirmDialog, el, fmtMoney, loadRefs, modal, parseMoney, run, state } from './api.js';
import { bankSection } from './bank.js';

export async function render(root) {
    const list = el('div');
    root.append(el('div', { class: 'card' },
        el('div', { class: 'spread' }, el('h2', {}, 'Accounts'),
            el('button', { class: 'btn', onclick: () => edit(null).then(ok => ok && load(list)) }, '+ Add account')),
        el('p', { class: 'muted', style: { marginBottom: '16px' } },
            'Add one account per bank account or card. If you enter the IBAN, transfers between your own accounts are detected ',
            'automatically and left out of income and expense totals.'),
        list));
    await load(list);
    root.append(await bankSection(() => load(list)));
}

async function load(list) {
    await loadRefs();
    const balances = Object.fromEntries((await api('/stats/balances')).map(b => [b.account_id, b.balance]));
    clear(list).append(el('table', { class: 'data' },
        el('thead', {}, el('tr', {}, ['Name', 'Bank', 'IBAN', 'Opening balance', 'Current balance', ''].map(h => el('th', {}, h)))),
        el('tbody', {}, state.accounts.map(a => el('tr', {},
            el('td', {}, a.name), el('td', {}, a.bank), el('td', { class: 'small' }, a.iban),
            el('td', { class: 'num' }, fmtMoney(a.opening_balance_cents)),
            el('td', { class: `num ${balances[a.id] < 0 ? 'neg' : ''}` }, fmtMoney(balances[a.id])),
            el('td', { class: 'num' },
                el('button', { class: 'btn-light btn-sm', onclick: () => edit(a).then(ok => ok && load(list)) }, 'Edit'), ' ',
                el('button', { class: 'btn-danger btn-sm', onclick: async e => {
                    const btn = e.currentTarget;
                    if (!await confirmDialog(`Delete account “${a.name}” and ALL its transactions?`, { danger: true, confirmLabel: 'Delete', requireText: 'DELETE' })) return;
                    run(btn, async () => { await api(`/accounts/${a.id}`, { method: 'DELETE' }); load(list); });
                } }, 'Delete')))))));
}

function edit(a) {
    return modal(a ? 'Edit account' : 'Add account', close => {
        const name = el('input', { required: true, value: a?.name || '' });
        const bank = el('input', { value: a?.bank || '' });
        const iban = el('input', { value: a?.iban || '', placeholder: 'DE…' });
        const opening = el('input', { inputmode: 'decimal', value: a ? (a.opening_balance_cents < 0 ? '-' : '') + centsToInput(a.opening_balance_cents) : '0,00' });
        const openingDate = el('input', { type: 'date', value: a?.opening_date || '' });
        const save = el('button', { class: 'btn', type: 'submit' }, 'Save');
        const g = (l, i, hint) => el('div', { class: 'form-group' }, el('label', {}, l), i, hint ? el('p', { class: 'muted small' }, hint) : null);
        return el('form', { onsubmit: e => {
            e.preventDefault();
            const cents = parseMoney(opening.value);
            if (Number.isNaN(cents)) { opening.focus(); return; }
            const body = { name: name.value.trim(), bank: bank.value.trim(), iban: iban.value.trim(), opening_balance_cents: cents, opening_date: openingDate.value || null };
            run(save, async () => {
                await api(a ? `/accounts/${a.id}` : '/accounts', { method: a ? 'PUT' : 'POST', body });
                close(true);
            });
        } },
        g('Name', name), g('Bank', bank), g('IBAN', iban),
        g('Opening balance (€)', opening, 'Balance before the first imported transaction, so the balance chart matches your bank.'),
        g('Opening date', openingDate),
        el('div', { class: 'actions' }, el('button', { type: 'button', class: 'btn-light', onclick: () => close(false) }, 'Cancel'), save));
    });
}
