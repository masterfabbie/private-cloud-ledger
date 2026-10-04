import {
    api, categoryById, categoryOptions, centsToInput, clear, confirmDialog, el, fmtDate, fmtMoney, fmtSigned, loadRefs,
    modal, options, parseMoney, run, state, toast,
} from './api.js';

const KINDS = [['expense', 'Expense'], ['income', 'Income'], ['transfer', 'Transfer (excluded from totals)']];
const FIELDS = [['payer', 'Payer / Payee'], ['description', 'Description'], ['iban', 'IBAN']];
const MATCHES = [['contains', 'contains'], ['equals', 'equals'], ['regex', 'matches regex']];
const SIGNS = [['any', 'Income and expenses'], ['expense', 'Expenses only'], ['income', 'Income only']];
const AMOUNT_OPS = [['any', 'Any amount'], ['eq', 'Exactly'], ['between', 'Between'], ['min', 'At least'], ['max', 'At most']];

function amountOp(r) {
    const lo = r?.amount_min_cents, hi = r?.amount_max_cents;
    if (lo == null && hi == null) return 'any';
    if (lo != null && hi != null) return lo === hi ? 'eq' : 'between';
    return lo != null ? 'min' : 'max';
}

/** Human-readable amount condition, e.g. "amount between 10,00 € and 20,00 €". */
export function describeAmount(r) {
    const lo = r.amount_min_cents, hi = r.amount_max_cents;
    switch (amountOp(r)) {
        case 'eq': return `amount is ${fmtMoney(lo)}`;
        case 'between': return `amount between ${fmtMoney(lo)} and ${fmtMoney(hi)}`;
        case 'min': return `amount at least ${fmtMoney(lo)}`;
        case 'max': return `amount at most ${fmtMoney(hi)}`;
        default: return '';
    }
}

export async function render(root) {
    const cats = el('div');
    const rules = el('div');
    root.append(el('div', { class: 'grid-2' },
        el('div', { class: 'card' },
            el('div', { class: 'spread' }, el('h2', {}, 'Categories'),
                el('button', { class: 'btn', onclick: () => editCategory(null).then(ok => ok && refresh()) }, '+ Add')),
            cats),
        el('div', { class: 'card' },
            el('div', { class: 'spread' }, el('h2', {}, 'Auto-categorization rules'),
                el('button', { class: 'btn', onclick: () => editRule(null).then(ok => ok && refresh()) }, '+ Add')),
            el('p', { class: 'muted small', style: { marginBottom: '12px' } },
                'Rules run on every import, top to bottom; the first match wins. Tip: change a category in the transaction list and ',
                'the app offers to create a rule for you.'),
            el('div', { class: 'row', style: { marginBottom: '14px' } },
                el('button', { class: 'btn-light btn-sm', onclick: e => rerun(e.currentTarget, false) }, 'Apply to uncategorized'),
                el('button', { class: 'btn-light btn-sm', onclick: e => rerun(e.currentTarget, true) }, 'Re-apply to all transactions')),
            rules)));

    async function refresh() {
        await loadRefs();
        renderCategories(cats, refresh);
        await renderRules(rules, refresh);
    }
    await refresh();
}

function rerun(btn, all) {
    run(btn, async () => {
        const r = await api('/rules/rerun', { method: 'POST', query: { all_transactions: all } });
        toast(`${r.updated} transactions updated`);
    });
}

function renderCategories(box, refresh) {
    clear(box).append(el('table', { class: 'data' }, el('tbody', {}, state.categories.map(c => el('tr', {},
        el('td', {}, el('span', { class: 'dot', style: { background: c.color, marginRight: '8px' } }), c.name),
        el('td', { class: 'muted small' }, c.kind),
        el('td', { class: 'num' },
            el('button', { class: 'btn-light btn-sm', onclick: () => editCategory(c).then(ok => ok && refresh()) }, 'Edit'), ' ',
            el('button', { class: 'btn-danger btn-sm', onclick: () => deleteCategory(c).then(ok => ok && refresh()) }, 'Delete')))))));
}

function editCategory(c) {
    return modal(c ? 'Edit category' : 'Add category', close => {
        const name = el('input', { required: true, value: c?.name || '' });
        const color = el('input', { type: 'color', value: c?.color || '#667eea' });
        const kind = el('select', {}, options(KINDS, c?.kind || 'expense'));
        const save = el('button', { class: 'btn', type: 'submit' }, 'Save');
        return el('form', { onsubmit: e => {
            e.preventDefault();
            run(save, async () => {
                const body = { name: name.value.trim(), color: color.value, kind: kind.value };
                await api(c ? `/categories/${c.id}` : '/categories', { method: c ? 'PUT' : 'POST', body });
                close(true);
            });
        } },
        el('div', { class: 'form-group' }, el('label', {}, 'Name'), name),
        el('div', { class: 'row form-group' }, el('div', {}, el('label', {}, 'Color'), color), el('div', { class: 'grow' }, el('label', {}, 'Kind'), kind)),
        el('div', { class: 'actions' }, el('button', { type: 'button', class: 'btn-light', onclick: () => close(false) }, 'Cancel'), save));
    });
}

function deleteCategory(c) {
    return modal(`Delete “${c.name}”`, close => {
        const moveTo = el('select', {}, options(state.categories.filter(x => x.id !== c.id).map(x => [x.id, x.name]), '', 'Leave uncategorized'));
        const del = el('button', { class: 'btn-danger' }, 'Delete');
        del.addEventListener('click', () => run(del, async () => {
            await api(`/categories/${c.id}`, { method: 'DELETE', query: { move_to: moveTo.value } });
            close(true);
        }));
        return el('div', {},
            el('div', { class: 'form-group' }, el('label', {}, 'Move its transactions to'), moveTo),
            el('p', { class: 'muted small' }, 'Rules and budgets for this category are deleted too.'),
            el('div', { class: 'actions' }, el('button', { class: 'btn-light', onclick: () => close(false) }, 'Cancel'), del));
    });
}

async function renderRules(box, refresh) {
    const rules = await api('/rules');
    clear(box);
    if (!rules.length) { box.append(el('p', { class: 'muted' }, 'No rules yet.')); return; }
    const label = (list, v) => (list.find(x => x[0] === v) || [v, v])[1];
    box.append(el('table', { class: 'data' }, el('tbody', {}, rules.map(r => {
        const cat = categoryById(r.category_id);
        return el('tr', {},
            el('td', { class: 'small' },
                r.pattern ? [`${label(FIELDS, r.field)} ${label(MATCHES, r.match)} `, el('strong', {}, `“${r.pattern}”`)] : null,
                r.pattern && describeAmount(r) ? ' and ' : null,
                describeAmount(r) ? el('strong', {}, describeAmount(r)) : null,
                r.amount_sign !== 'any' ? el('span', { class: 'muted' }, ` (${label(SIGNS, r.amount_sign).toLowerCase()})`) : null,
                ' → ', cat ? el('span', {}, el('span', { class: 'dot', style: { background: cat.color, margin: '0 4px' } }), cat.name) : '?',
                r.add_tags.length ? el('span', { class: 'muted' }, ` + tags: ${r.add_tags.join(', ')}`) : null),
            el('td', { class: 'num' },
                el('button', { class: 'btn-light btn-sm', onclick: () => editRule(r).then(ok => ok && refresh()) }, 'Edit'), ' ',
                el('button', { class: 'btn-danger btn-sm', onclick: async e => {
                    const btn = e.currentTarget;
                    if (await confirmDialog('Delete this rule?', { danger: true, confirmLabel: 'Delete' })) {
                        run(btn, async () => { await api(`/rules/${r.id}`, { method: 'DELETE' }); refresh(); });
                    }
                } }, '✕')));
    }))));
}

function editRule(r) {
    return modal(r ? 'Edit rule' : 'Add rule', close => {
        const field = el('select', {}, options(FIELDS, r?.field || 'payer'));
        const match = el('select', {}, options(MATCHES, r?.match || 'contains'));
        const pattern = el('input', { value: r?.pattern || '', placeholder: 'e.g. REWE' });
        const sign = el('select', {}, options(SIGNS, r?.amount_sign || 'any'));
        const op = el('select', { style: { width: 'auto' } }, options(AMOUNT_OPS, amountOp(r)));
        const toInput = c => (c == null ? '' : centsToInput(c));
        const amountA = el('input', { inputmode: 'decimal', placeholder: '0,00',
            value: toInput(amountOp(r) === 'max' ? r?.amount_max_cents : r?.amount_min_cents) });
        const amountB = el('input', { inputmode: 'decimal', placeholder: '0,00', value: toInput(r?.amount_max_cents) });
        const andLabel = el('span', { class: 'muted' }, 'and');
        const syncAmount = () => {
            const o = op.value;
            amountA.classList.toggle('hidden', o === 'any');
            amountB.classList.toggle('hidden', o !== 'between');
            andLabel.classList.toggle('hidden', o !== 'between');
        };
        op.addEventListener('change', syncAmount);
        syncAmount();
        const category = el('select', { required: true }, categoryOptions(r?.category_id));
        const tags = el('input', { value: (r?.add_tags || []).join(', '), placeholder: 'optional, comma separated' });
        const priority = el('input', { type: 'number', value: r?.priority ?? 100 });
        const save = el('button', { class: 'btn', type: 'submit' }, 'Save');
        const g = (l, i) => el('div', { class: 'form-group' }, el('label', {}, l), i);
        const applyNow = el('input', { type: 'checkbox' });
        const applyLabel = el('span', {}, 'Also apply to matching transactions now');
        const previewBox = el('div', { class: 'rule-preview' });

        /** The rule as currently entered, or an error message. */
        const collect = () => {
            let lo = null, hi = null;
            if (op.value !== 'any') {
                const a = Math.abs(parseMoney(amountA.value));
                const b = Math.abs(parseMoney(amountB.value));
                if (Number.isNaN(a) || (op.value === 'between' && Number.isNaN(b))) return { error: 'Please enter a valid amount' };
                if (op.value === 'eq') lo = hi = a;
                else if (op.value === 'min') lo = a;
                else if (op.value === 'max') hi = a;
                else [lo, hi] = [Math.min(a, b), Math.max(a, b)];
            }
            if (!pattern.value.trim() && op.value === 'any') return { error: 'Enter a text to match, an amount, or both' };
            return { body: {
                amount_min_cents: lo, amount_max_cents: hi,
                field: field.value, match: match.value, pattern: pattern.value.trim(), amount_sign: sign.value,
                category_id: Number(category.value), priority: Number(priority.value) || 100,
                add_tags: tags.value.split(',').map(t => t.trim().toLowerCase()).filter(Boolean),
            } };
        };

        let previewTimer, previewSeq = 0;
        const refreshPreview = () => {
            clearTimeout(previewTimer);
            previewTimer = setTimeout(async () => {
                const seq = ++previewSeq;
                const { body, error } = collect();
                if (error) {
                    previewBox.replaceChildren(el('div', { class: 'muted small' }, `${error} to see which transactions it matches.`));
                    applyLabel.textContent = 'Also apply to matching transactions now';
                    return;
                }
                let p;
                try {
                    p = await api('/rules/preview', { method: 'POST', body: { ...body, rule_id: r?.id ?? null, category_id: body.category_id || null } });
                } catch (e) {
                    if (seq === previewSeq) previewBox.replaceChildren(el('div', { class: 'small neg' }, e.message));
                    return;
                }
                if (seq !== previewSeq) return; // a newer preview is on its way
                const cat = categoryById(body.category_id);
                const toChange = p.matches - p.already_in_category;
                applyLabel.textContent = cat
                    ? `Also apply to the ${p.matches} matching transactions now (${toChange} would change to ${cat.name})`
                    : `Also apply to the ${p.matches} matching transactions now`;
                const parts = [el('strong', {}, p.matches === 1 ? 'Matches 1 transaction' : `Matches ${p.matches} transactions`)];
                if (cat && p.matches) parts.push(` · ${p.already_in_category} already in ${cat.name}`);
                if (p.taken_by_earlier_rules) {
                    parts.push(el('span', { class: 'neg', title: 'Rules with a lower priority number run first; the first match wins on import' },
                        ` · ${p.taken_by_earlier_rules} also match an earlier rule with a different category, which wins on import`));
                }
                previewBox.replaceChildren(...[
                    el('div', { class: 'summary' }, parts),
                    p.sample.length ? el('div', { class: 'table-wrap' }, el('table', { class: 'data' },
                        el('tbody', {}, p.sample.map(t => el('tr', {},
                            el('td', {}, fmtDate(t.booking_date)),
                            el('td', {}, t.description, t.payer ? el('div', { class: 'muted' }, t.payer) : null),
                            el('td', {}, t.category_name
                                ? [el('span', { class: 'dot', style: { background: t.category_color, marginRight: '6px' } }), t.category_name]
                                : el('span', { class: 'muted' }, 'Uncategorized')),
                            el('td', { class: `num ${t.amount_cents < 0 ? 'neg' : 'pos'}` }, fmtSigned(t.amount_cents))))))) : null,
                    p.matches > p.sample.length ? el('div', { class: 'muted small', style: { marginTop: '6px' } }, `Showing the newest ${p.sample.length}.`) : null,
                ].filter(Boolean));
            }, 300);
        };
        for (const input of [field, match, pattern, sign, op, amountA, amountB, category, priority]) {
            input.addEventListener('input', refreshPreview);
            input.addEventListener('change', refreshPreview);
        }
        refreshPreview();

        return el('form', { onsubmit: e => {
            e.preventDefault();
            const { body, error } = collect();
            if (error) { toast(error, { error: true }); return; }
            run(save, async () => {
                const saved = await api(r ? `/rules/${r.id}` : '/rules', { method: r ? 'PUT' : 'POST', body });
                if (applyNow.checked) {
                    const res = await api(`/rules/${saved.id}/apply`, { method: 'POST' });
                    toast(`Rule saved and applied: ${res.updated} transactions updated`);
                }
                close(true);
            });
        } },
        el('div', { class: 'rule-editor' },
            el('div', {},
                el('div', { class: 'grid-2', style: { gap: '0 16px' } }, g('Field', field), g('Match', match)),
                g('Text (case-insensitive, optional when an amount is set)', pattern),
                g('Amount (without sign)', el('div', { class: 'row' }, op, el('div', { class: 'grow' }, amountA), andLabel, el('div', { class: 'grow' }, amountB))),
                el('div', { class: 'grid-2', style: { gap: '0 16px' } }, g('Applies to', sign), g('Priority (lower runs first)', priority)),
                g('Set category', category),
                g('Add tags', tags)),
            el('div', {},
                el('label', {}, 'Preview'),
                previewBox,
                el('label', { class: 'inline-label' }, applyNow, applyLabel))),
        el('div', { class: 'actions' }, el('button', { type: 'button', class: 'btn-light', onclick: () => close(false) }, 'Cancel'), save));
    }, { wide: true });
}
