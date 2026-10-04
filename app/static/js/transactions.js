import {
    accountOptions, api, categoryById, categoryOptions, centsToInput, clear, confirmDialog, el, fmtDate, fmtMoney, fmtSigned,
    loadRefs, modal, options, parseMoney, run, showError, state, toast, todayIso, toQuery,
} from './api.js';
import { filterBar, filterQuery } from './filters.js';

const local = { q: '', category_id: '', tag: '', kind: '', page: 1 };
const PAGE_SIZE = 50;

export async function render(root) {
    const list = el('div');
    const pager = el('div', { class: 'pager' });
    const reload = () => { local.page = 1; loadList(list, pager); };

    let searchTimer;
    const search = el('input', {
        type: 'search', placeholder: 'Search description, payer, notes…', value: local.q,
        oninput: () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { local.q = search.value; reload(); }, 300); },
    });
    const cat = el('select', { onchange: () => { local.category_id = cat.value; reload(); } },
        options([[0, 'Uncategorized'], ...state.categories.map(c => [c.id, c.name])], local.category_id, 'All categories'));
    const tag = el('select', { onchange: () => { local.tag = tag.value; reload(); } }, options(state.tags, local.tag, 'All tags'));
    const kind = el('select', { onchange: () => { local.kind = kind.value; reload(); } },
        options([['income', 'Income'], ['expense', 'Expenses']], local.kind, 'Income & expenses'));

    const bar = await filterBar(reload, [cat, tag, kind]);
    const exportLink = fmt => el('a', { class: 'btn-light', href: '#', onclick: e => {
        e.preventDefault();
        location.href = `/api/export/${fmt}?` + toQuery(query());
    } }, `Export ${fmt.toUpperCase()}`);

    root.append(
        bar,
        el('div', { class: 'card' },
            el('div', { class: 'spread' },
                el('h2', {}, 'Transactions'),
                el('div', { class: 'row' },
                    el('button', { class: 'btn', onclick: () => editTransaction(null).then(ok => ok && loadList(list, pager)) }, '+ Add Transaction'),
                    exportLink('csv'), exportLink('xlsx'),
                    el('button', { class: 'btn-danger', onclick: e => deleteAll(e.currentTarget, list, pager) }, 'Delete All'))),
            el('div', { style: { marginBottom: '12px' } }, search),
            list, pager));
    await loadList(list, pager);
}

function query() {
    return { ...filterQuery(), q: local.q, category_id: local.category_id, tag: local.tag, kind: local.kind };
}

async function loadList(list, pager) {
    const data = await api('/transactions', { query: { ...query(), page: local.page, page_size: PAGE_SIZE } });
    clear(list);
    clear(pager);
    if (!data.items.length) {
        list.append(el('p', { class: 'empty' }, 'No transactions found'));
        return;
    }
    const refresh = () => loadList(list, pager);
    data.items.forEach(t => list.append(row(t, refresh)));
    const pages = Math.ceil(data.total / PAGE_SIZE);
    pager.append(
        el('button', { class: 'btn-light btn-sm', disabled: local.page <= 1, onclick: () => { local.page--; refresh(); } }, '← Prev'),
        el('span', { class: 'muted' }, `Page ${local.page} of ${pages} · ${data.total} transactions`),
        el('button', { class: 'btn-light btn-sm', disabled: local.page >= pages, onclick: () => { local.page++; refresh(); } }, 'Next →'));
}

function row(t, refresh) {
    const catBtn = el('button', { class: 'transaction-category', title: 'Click to change category' },
        t.category_color ? el('span', { class: 'dot', style: { background: t.category_color } }) : null,
        t.category_name || 'Uncategorized');
    catBtn.addEventListener('click', () => {
        const select = el('select', { class: 'transaction-category-select' }, categoryOptions(t.category_id));
        let done = false;
        const finish = async () => {
            if (done) return;
            done = true;
            const newId = select.value ? Number(select.value) : null;
            if (newId === t.category_id) { select.replaceWith(catBtn); return; }
            try {
                await api(`/transactions/${t.id}`, { method: 'PATCH', body: { category_id: newId } });
                if (newId) await offerRule(t, newId);
            } catch (e) { showError(e); }
            refresh();
        };
        select.addEventListener('change', finish);
        select.addEventListener('blur', finish);
        catBtn.replaceWith(select);
        select.focus();
    });

    const isSplit = t.splits.length > 0;
    const splitChip = isSplit
        ? el('button', { class: 'transaction-category', title: 'Edit the split', onclick: () => splitEditor(t).then(ok => ok && refresh()) },
            `✂ Split into ${t.splits.length}`)
        : null;
    const splitLines = isSplit
        ? el('div', { class: 'split-lines' }, t.splits.map(sp => el('div', { class: 'split-line' },
            el('span', {}, el('span', { class: 'dot', style: { background: sp.category_color || '#C9CBCF', marginRight: '6px' } }),
                sp.category_name || 'Uncategorized', sp.note ? el('span', { class: 'muted' }, ` · ${sp.note}`) : null),
            el('span', { class: sp.amount_cents < 0 ? 'neg' : 'pos' }, fmtSigned(sp.amount_cents)))))
        : null;

    return el('div', { class: 'transaction-item' },
        el('div', { class: 'transaction-info' },
            el('div', { class: 'transaction-title' }, t.description),
            el('div', { class: 'transaction-meta' },
                `${fmtDate(t.booking_date)} • `, isSplit ? splitChip : catBtn,
                state.accounts.length > 1 ? ` • ${t.account_name}` : '',
                t.payer ? [el('br'), el('small', {}, `Payer/Payee: ${t.payer}`)] : null,
                t.tags.length ? [el('br'), el('small', {}, 'Tags: ', t.tags.map(x => el('span', { class: 'tag-small' }, x)))] : null,
                t.notes ? [el('br'), el('small', {}, `📝 ${t.notes}`)] : null),
            splitLines),
        el('div', { class: 'transaction-actions' },
            el('div', { class: `transaction-amount ${t.type === 'income' ? 'pos' : 'neg'}` }, fmtSigned(t.amount_cents)),
            el('button', { class: 'btn-light btn-sm', title: 'Distribute this amount over several categories', onclick: () => splitEditor(t).then(ok => ok && refresh()) }, 'Split'),
            el('button', { class: 'btn-light btn-sm', onclick: () => editTransaction(t).then(ok => ok && refresh()) }, 'Edit'),
            el('button', { class: 'btn-danger btn-sm', onclick: async () => {
                if (await confirmDialog('Are you sure you want to delete this transaction?', { danger: true, confirmLabel: 'Delete' })) {
                    run(null, async () => { await api(`/transactions/${t.id}`, { method: 'DELETE' }); refresh(); });
                }
            } }, 'Delete')));
}

/** After an inline category change, offer to apply it to similar transactions and create a rule. */
async function offerRule(t, categoryId) {
    const s = await api(`/rules/suggest/${t.id}`);
    const cat = categoryById(categoryId);
    const what = s.field === 'payer' ? `from “${s.pattern}”` : s.field === 'iban' ? `to/from ${s.pattern}` : `with this description`;
    const label = s.differently_categorized
        ? `Also set ${s.differently_categorized} similar transaction(s) ${what} to ${cat.name} and remember it?`
        : `Always categorize transactions ${what} as ${cat.name}?`;
    toast(label, {
        actionLabel: 'Yes, create rule',
        action: () => run(null, async () => {
            await api('/rules/from-transaction', { method: 'POST', body: { transaction_id: t.id, category_id: categoryId, field: s.field, apply_existing: true } });
            toast('Rule created');
            window.dispatchEvent(new HashChangeEvent('hashchange'));
        }),
    });
}

async function deleteAll(button, list, pager) {
    const ok = await confirmDialog(
        'Delete ALL transactions' + (state.filters.account_id ? ' of the selected account' : '') + '? This cannot be undone.',
        { danger: true, confirmLabel: 'Delete all', requireText: 'DELETE' });
    if (!ok) return;
    await run(button, async () => {
        const r = await api('/transactions', { method: 'DELETE', query: { confirm: 'DELETE', account_id: state.filters.account_id } });
        toast(`Deleted ${r.deleted} transactions`);
        await loadList(list, pager);
    });
}

/** Add (t = null) or edit a transaction. Resolves true when saved. */
export function editTransaction(t) {
    return modal(t ? 'Edit Transaction' : 'Add Transaction', close => {
        let tags = t ? [...t.tags] : [];
        const type = el('select', { required: true }, options([['income', 'Income'], ['expense', 'Expense']], t ? t.type : 'expense'));
        const description = el('input', { required: true, value: t?.description || '' });
        const payer = el('input', { placeholder: 'Who paid / who was paid?', value: t?.payer || '' });
        const amount = el('input', { required: true, inputmode: 'decimal', placeholder: '0,00', value: t ? centsToInput(t.amount_cents) : '' });
        const date = el('input', { type: 'date', required: true, value: t?.booking_date || todayIso() });
        const account = el('select', { required: true }, accountOptions(t?.account_id || state.filters.account_id || state.accounts[0]?.id));
        const category = el('select', {}, categoryOptions(t?.category_id));
        const notes = el('input', { value: t?.notes || '' });
        const tagsBox = el('div', { class: 'tags-container' });
        const tagInput = el('input', { placeholder: 'e.g. work, personal, urgent', list: 'tagList' });
        const renderTags = () => {
            clear(tagsBox);
            tags.forEach(tag => tagsBox.append(el('span', { class: 'tag' }, tag,
                el('button', { type: 'button', class: 'tag-remove', onclick: () => { tags = tags.filter(x => x !== tag); renderTags(); } }, '×'))));
        };
        const addTag = () => {
            const v = tagInput.value.trim().toLowerCase();
            if (v && !tags.includes(v)) tags.push(v);
            tagInput.value = '';
            renderTags();
        };
        tagInput.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addTag(); } });
        renderTags();

        const group = (label, input) => el('div', { class: 'form-group' }, el('label', {}, label), input);
        const save = el('button', { class: 'btn', type: 'submit' }, t ? 'Save' : 'Add Transaction');
        return el('form', {
            onsubmit: e => {
                e.preventDefault();
                addTag();
                const cents = parseMoney(amount.value);
                if (Number.isNaN(cents) || cents === 0) { amount.focus(); toast('Please enter a valid amount', { error: true }); return; }
                const body = {
                    account_id: Number(account.value),
                    booking_date: date.value,
                    amount_cents: type.value === 'income' ? Math.abs(cents) : -Math.abs(cents),
                    description: description.value.trim(),
                    payer: payer.value.trim(),
                    category_id: category.value ? Number(category.value) : null,
                    notes: notes.value.trim(),
                    tags,
                };
                run(save, async () => {
                    if (t) await api(`/transactions/${t.id}`, { method: 'PATCH', body });
                    else await api('/transactions', { method: 'POST', body });
                    await loadRefs();
                    close(true);
                });
            },
        },
        el('div', { class: 'grid-2', style: { gap: '0 16px' } }, group('Type', type), group('Amount (€)', amount)),
        group('Description', description),
        group('Payer / Payee', payer),
        el('div', { class: 'grid-2', style: { gap: '0 16px' } }, group('Date', date), group('Account', account)),
        group('Category', category),
        group('Tags (press Enter to add)', el('div', {}, tagInput, el('datalist', { id: 'tagList' }, state.tags.map(x => el('option', { value: x }))), tagsBox)),
        group('Notes', notes),
        el('div', { class: 'actions' }, el('button', { type: 'button', class: 'btn-light', onclick: () => close(false) }, 'Cancel'), save));
    });
}

/** Distribute one transaction over several categories, e.g. the items of an Amazon order. */
export function splitEditor(t) {
    return modal('Split transaction', close => {
        const sign = t.amount_cents < 0 ? -1 : 1;
        const total = Math.abs(t.amount_cents);
        const rowsBox = el('div', { class: 'split-rows' });
        const remainingEl = el('strong');
        const save = el('button', { class: 'btn', type: 'button' }, 'Save split');
        const parts = [];

        const remaining = () => total - parts.reduce((sum, p) => {
            const c = parseMoney(p.amount.value);
            return sum + (Number.isNaN(c) ? 0 : Math.abs(c));
        }, 0);

        const update = () => {
            const rest = remaining();
            remainingEl.textContent = rest === 0 ? 'Fully distributed ✓' : `Remaining: ${fmtMoney(rest)}`;
            remainingEl.className = rest === 0 ? 'pos' : 'neg';
            const filled = parts.filter(p => p.amount.value.trim());
            save.disabled = rest !== 0 || filled.length < 2 || filled.some(p => Number.isNaN(parseMoney(p.amount.value)));
        };

        const addPart = (part = {}) => {
            const amount = el('input', { inputmode: 'decimal', placeholder: '0,00', value: part.amount_cents ? centsToInput(part.amount_cents) : '' });
            const category = el('select', {}, categoryOptions(part.category_id ?? '', 'Uncategorized'));
            const note = el('input', { placeholder: 'Note, e.g. the item', value: part.note || '' });
            const p = { amount, category, note };
            const restBtn = el('button', { type: 'button', class: 'btn-light btn-sm', title: 'Put the remaining amount here' }, '= rest');
            const removeBtn = el('button', { type: 'button', class: 'btn-light btn-sm', title: 'Remove this part' }, '✕');
            const line = el('div', { class: 'split-row' }, el('div', { class: 'split-amount' }, amount), category, note, restBtn, removeBtn);
            restBtn.addEventListener('click', () => {
                const current = parseMoney(amount.value);
                const rest = remaining() + (Number.isNaN(current) ? 0 : Math.abs(current));
                amount.value = rest > 0 ? centsToInput(rest) : '';
                update();
            });
            removeBtn.addEventListener('click', () => { parts.splice(parts.indexOf(p), 1); line.remove(); update(); });
            amount.addEventListener('input', update);
            parts.push(p);
            rowsBox.append(line);
            update();
            return p;
        };

        if (t.splits.length) t.splits.forEach(sp => addPart(sp));
        else { addPart({ category_id: t.category_id }); addPart(); }

        // Paste lines from the order or statement: "12,99 Netflix", "USB cable; 9.99", ...
        const paste = el('textarea', { rows: 4, placeholder: 'Paste lines with amounts, one item per line, e.g.\nKaffeebohnen 12,99\nUSB-C Kabel 9,99' });
        const pasteInfo = el('div', { class: 'muted small' });
        const pasteBtn = el('button', { type: 'button', class: 'btn-light btn-sm' }, 'Add lines as parts');
        pasteBtn.addEventListener('click', () => run(pasteBtn, async () => {
            if (!paste.value.trim()) return;
            const r = await api(`/transactions/${t.id}/split-lines`, { method: 'POST', body: { text: paste.value } });
            // Fill empty rows first, then add new ones.
            for (const part of r.parts) {
                const empty = parts.find(p => !p.amount.value.trim() && !p.note.value.trim());
                if (empty) {
                    empty.amount.value = centsToInput(part.amount_cents);
                    empty.note.value = part.note;
                    if (part.category_id) empty.category.value = String(part.category_id);
                } else {
                    addPart(part);
                }
            }
            paste.value = '';
            pasteInfo.textContent = `${r.parts.length} part(s) added` + (r.skipped.length ? `; no amount found in: ${r.skipped.join(' | ')}` : '');
            update();
        }));

        save.addEventListener('click', () => run(save, async () => {
            const body = { parts: parts.filter(p => p.amount.value.trim()).map(p => ({
                amount_cents: sign * Math.abs(parseMoney(p.amount.value)),
                category_id: p.category.value ? Number(p.category.value) : null,
                note: p.note.value.trim(),
            })) };
            await api(`/transactions/${t.id}/splits`, { method: 'PUT', body });
            close(true);
        }));
        const unsplit = t.splits.length
            ? el('button', { type: 'button', class: 'btn-light', onclick: () => run(null, async () => {
                await api(`/transactions/${t.id}/splits`, { method: 'PUT', body: { parts: [] } });
                close(true);
            }) }, 'Remove split')
            : null;

        return el('div', {},
            el('div', { class: 'spread' },
                el('div', {}, el('strong', {}, t.description), el('div', { class: 'muted small' }, `${fmtDate(t.booking_date)} · ${t.payer || t.account_name}`)),
                el('div', { class: `transaction-amount ${sign < 0 ? 'neg' : 'pos'}` }, fmtSigned(t.amount_cents))),
            rowsBox,
            el('div', { class: 'spread', style: { margin: '8px 0 16px' } },
                el('button', { type: 'button', class: 'btn-light btn-sm', onclick: () => addPart().amount.focus() }, '+ Add part'),
                remainingEl),
            el('label', {}, 'Quick entry'),
            paste,
            el('div', { class: 'row', style: { margin: '6px 0 4px' } }, pasteBtn, pasteInfo),
            el('p', { class: 'muted small' }, 'The last amount on each line is used; categories are suggested by your rules.'),
            el('div', { class: 'actions' }, unsplit, el('button', { type: 'button', class: 'btn-light', onclick: () => close(false) }, 'Cancel'), save));
    }, { wide: true });
}
