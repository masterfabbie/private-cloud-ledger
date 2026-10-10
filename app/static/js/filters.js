import { accountOptions, api, el, MONTHS, options, state } from './api.js';

/** Account / year / month filter bar shared by dashboard and transactions. */
export async function filterBar(onChange, extra = []) {
    const years = await api('/transactions/years');
    const f = state.filters;
    const account = el('select', { onchange: () => { f.account_id = account.value; onChange(); } },
        accountOptions(f.account_id, 'All accounts'));
    const year = el('select', { onchange: () => { f.year = year.value; onChange(); } },
        options(years, f.year, 'All years'));
    const month = el('select', { onchange: () => { f.month = month.value; onChange(); } },
        options(MONTHS.map((m, i) => [i + 1, m]), f.month, 'All months'));
    const selects = [account, year, month, ...extra];
    const summary = el('summary', { class: 'btn-light btn-sm' });
    const setSummary = () => {
        const active = selects.filter(s => s.value !== '').length;
        summary.textContent = active ? `⚙ Filters · ${active} active` : '⚙ Filters';
    };
    selects.forEach(s => s.addEventListener('change', setSummary));
    setSummary();
    // On phones the filters fold away behind one button; on larger screens they are always shown.
    return el('details', { class: 'filter-panel', open: window.matchMedia('(min-width: 769px)').matches },
        summary, el('div', { class: 'row filters' }, selects));
}

export const filterQuery = () => ({ ...state.filters });
