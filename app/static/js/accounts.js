import { api, centsToInput, clear, confirmDialog, el, fmtDate, fmtMoney, loadRefs, modal, options, parseMoney, run, state, toast } from './api.js';
import { bankSection } from './bank.js';

const KINDS = [['checking', 'Checking account'], ['savings', 'Savings account'], ['credit_card', 'Credit card'], ['cash', 'Cash']];
const kindLabel = k => (KINDS.find(x => x[0] === k) || [k, k])[1];

export async function render(root) {
    const list = el('div');
    const cardsBox = el('div', { dataset: { cards: '1' } });
    root.append(el('div', { class: 'card' },
        el('div', { class: 'spread' }, el('h2', {}, 'Accounts'),
            el('button', { class: 'btn', onclick: () => edit(null).then(ok => ok && refreshAll()) }, '+ Add account')),
        el('p', { class: 'muted', style: { marginBottom: '16px' } },
            'Add one account per bank account or card. If you enter the IBAN, transfers between your own accounts are detected ',
            'automatically and left out of income and expense totals.'),
        list));
    await load(list);
    root.append(cardsBox);
    await renderCardChecks(cardsBox);
    root.append(await bankSection(() => load(list)));
}

/** For each credit card: the settlements found on other accounts and whether the card statement has the matching payment. */
async function renderCardChecks(box) {
    const cards = state.accounts.filter(a => a.kind === 'credit_card');
    clear(box);
    if (!cards.length) return;
    const card = el('div', { class: 'card' }, el('h2', {}, 'Credit card settlements'),
        el('p', { class: 'muted', style: { marginBottom: '12px' } },
            'Settlements on your checking account are counted as transfers, so card purchases are not counted twice. ',
            'If the card statement contains the matching payment, it is shown as found.'));
    for (const c of cards) {
        const rows = await api(`/accounts/${c.id}/settlements`);
        card.append(el('h3', { style: { marginTop: '12px' } }, c.name,
            el('span', { class: 'muted small' }, c.settlement_pattern ? `  · settlement text “${c.settlement_pattern}”` : '  · no settlement text set')));
        if (!c.settlement_pattern) {
            card.append(el('p', { class: 'muted small' }, 'Edit the account and enter the text your bank uses for the card settlement on the checking account, e.g. “KREDITKARTENABRECHNUNG”.'));
            continue;
        }
        if (!rows.length) {
            card.append(el('p', { class: 'muted small' }, 'No settlement found yet on your other accounts.'));
            continue;
        }
        card.append(el('table', { class: 'data' },
            el('thead', {}, el('tr', {}, ['Date', 'From', 'Amount', 'Counted as', 'On card statement'].map(h => el('th', {}, h)))),
            el('tbody', {}, rows.map(r => el('tr', {},
                el('td', {}, fmtDate(r.date)),
                el('td', {}, r.from_account, el('div', { class: 'muted small' }, r.description)),
                el('td', { class: 'num' }, fmtMoney(r.amount_cents)),
                el('td', { class: r.is_transfer ? 'pos' : 'neg' }, r.is_transfer ? 'Transfer ✓' : 'Expense: not excluded'),
                el('td', {}, r.card_payment_date ? `✓ found (${fmtDate(r.card_payment_date)})` : el('span', { class: 'muted' }, 'not found')))))));
    }
    box.append(card);
}

let refreshAll = () => {};

async function load(list) {
    refreshAll = async () => {
        await load(list);
        const box = list.closest('main')?.querySelector('[data-cards]');
        if (box) await renderCardChecks(box);
    };
    await loadRefs();
    const balances = Object.fromEntries((await api('/stats/balances')).map(b => [b.account_id, b.balance]));
    clear(list).append(el('table', { class: 'data' },
        el('thead', {}, el('tr', {}, ['Name', 'Type', 'Bank', 'IBAN', 'Opening balance', 'Current balance', ''].map(h => el('th', {}, h)))),
        el('tbody', {}, state.accounts.map(a => el('tr', {},
            el('td', {}, a.name), el('td', { class: 'small' }, kindLabel(a.kind)), el('td', {}, a.bank), el('td', { class: 'small' }, a.iban),
            el('td', { class: 'num' }, fmtMoney(a.opening_balance_cents)),
            el('td', { class: `num ${balances[a.id] < 0 ? 'neg' : ''}` }, fmtMoney(balances[a.id])),
            el('td', { class: 'num' },
                el('button', { class: 'btn-light btn-sm', onclick: () => edit(a).then(ok => ok && refreshAll()) }, 'Edit'), ' ',
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
        const kind = el('select', {}, options(KINDS, a?.kind || 'checking'));
        const pattern = el('input', { value: a?.settlement_pattern || '', placeholder: 'e.g. KREDITKARTENABRECHNUNG' });
        const patternGroup = el('div', { class: 'form-group' },
            el('label', {}, 'Settlement text on your checking account'), pattern,
            el('p', { class: 'muted small' },
                'Part of the booking text of the monthly card settlement on the account that pays the card. ',
                'Those bookings, and the matching payment on the card statement, are counted as transfers instead of expenses.'));
        const syncKind = () => patternGroup.classList.toggle('hidden', kind.value !== 'credit_card');
        kind.addEventListener('change', syncKind);
        syncKind();
        const save = el('button', { class: 'btn', type: 'submit' }, 'Save');
        const g = (l, i, hint) => el('div', { class: 'form-group' }, el('label', {}, l), i, hint ? el('p', { class: 'muted small' }, hint) : null);
        return el('form', { onsubmit: e => {
            e.preventDefault();
            const cents = parseMoney(opening.value);
            if (Number.isNaN(cents)) { opening.focus(); return; }
            const body = {
                name: name.value.trim(), bank: bank.value.trim(), iban: iban.value.trim(), opening_balance_cents: cents,
                opening_date: openingDate.value || null, kind: kind.value, settlement_pattern: pattern.value.trim(),
            };
            run(save, async () => {
                const saved = await api(a ? `/accounts/${a.id}` : '/accounts', { method: a ? 'PUT' : 'POST', body });
                if (saved.kind === 'credit_card' && saved.settlement_pattern) {
                    const r = await api(`/accounts/${saved.id}/apply-settlements`, { method: 'POST' });
                    if (r.updated) toast(`${r.updated} existing settlement booking(s) are now counted as transfers`);
                }
                close(true);
            });
        } },
        el('div', { class: 'grid-2', style: { gap: '0 16px' } }, g('Name', name), g('Type', kind)),
        patternGroup,
        g('Bank', bank), g('IBAN', iban),
        g('Opening balance (€)', opening, 'Balance before the first imported transaction, so the balance chart matches your bank.'),
        g('Opening date', openingDate),
        el('div', { class: 'actions' }, el('button', { type: 'button', class: 'btn-light', onclick: () => close(false) }, 'Cancel'), save));
    });
}
