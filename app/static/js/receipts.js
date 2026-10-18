// Scanning receipts: read a photo/PDF, match it to a transaction, attach it, optionally split it.
import {
    accountOptions, api, centsToInput, clear, el, fmtDate, fmtMoney, fmtSigned, modal, parseMoney, run, state, todayIso,
} from './api.js';

/** Opens the scan dialog. Resolves to { transaction, parts } after attaching, or null. */
export function scanReceiptDialog() {
    return modal('Scan receipt', close => {
        const body = el('div');
        const camera = el('input', { type: 'file', accept: 'image/*', capture: 'environment', class: 'hidden' });
        const picker = el('input', { type: 'file', accept: 'image/*,application/pdf', class: 'hidden' });
        const cfg = state.receipts || {};

        const start = () => {
            clear(body).append(
                el('p', { style: { marginBottom: '12px' } }, 'Take a photo of the receipt or choose a photo/PDF. ',
                    'Proud Ledger reads the total, date and items and finds the matching bank transaction.'),
                el('div', { class: 'row' },
                    el('button', { type: 'button', class: 'btn', onclick: () => camera.click() }, '📷 Take photo'),
                    el('button', { type: 'button', class: 'btn-light', onclick: () => picker.click() }, 'Choose file')),
                el('p', { class: 'muted small', style: { marginTop: '12px' } }, `Reader: ${cfg.label || '–'}.`,
                    cfg.leaves_server ? ' The image is sent to Anthropic (Claude) to be read.' : ' The image stays on your server.'),
                el('div', { class: 'actions' }, el('button', { type: 'button', class: 'btn-light', onclick: () => close(null) }, 'Cancel')));
        };

        const read = async input => {
            const file = input.files[0];
            input.value = '';
            if (!file) return;
            clear(body).append(el('p', {}, '⏳ Reading the receipt… this can take a little while.'));
            const form = new FormData();
            form.append('file', file);
            try {
                review(await api('/receipts/scan', { method: 'POST', form }), file);
            } catch (e) {
                start();
                body.prepend(el('div', { class: 'status error', style: { marginTop: 0, marginBottom: '12px' } }, e.message));
            }
        };
        camera.addEventListener('change', () => read(camera));
        picker.addEventListener('change', () => read(picker));

        const review = (scan, file) => {
            const r = scan.receipt;
            const merchant = el('input', { value: r.merchant || '' });
            const date = el('input', { type: 'date', value: r.date || todayIso() });
            const total = el('input', { inputmode: 'decimal', value: r.total_cents ? centsToInput(r.total_cents) : '' });
            const choices = el('div', { class: 'match-list' });
            const options = [];
            const option = (value, label, sub, checked) => {
                const radio = el('input', { type: 'radio', name: 'receiptTarget', value, checked });
                options.push(radio);
                return el('label', { class: 'match-option' }, radio, el('span', {}, label, sub ? el('div', { class: 'muted small' }, sub) : null));
            };
            scan.candidates.forEach((t, i) => choices.append(option(String(t.id),
                `${t.description}`, `${fmtDate(t.booking_date)} · ${t.account_name} · ${fmtSigned(t.amount_cents)}`, i === 0)));
            const account = el('select', {}, accountOptions(state.filters.account_id || state.accounts[0]?.id));
            choices.append(option('new', 'Create a new transaction', null, scan.candidates.length === 0),
                el('div', { class: 'match-new' }, el('label', { class: 'small' }, 'Account'), account));
            const splitBox = el('input', { type: 'checkbox', checked: scan.parts.length >= 2 });
            const preview = file.type.startsWith('image/') ? el('img', { class: 'scan-preview', src: URL.createObjectURL(file), alt: 'Receipt' }) : null;
            const save = el('button', { type: 'button', class: 'btn' }, 'Attach receipt');

            save.addEventListener('click', () => run(save, async () => {
                const target = options.find(o => o.checked)?.value;
                const cents = parseMoney(total.value);
                const body = target && target !== 'new'
                    ? { transaction_id: Number(target) }
                    : { create: { account_id: Number(account.value), booking_date: date.value, amount_cents: -Math.abs(cents),
                        description: merchant.value.trim() || 'Receipt' } };
                if (body.create && (Number.isNaN(cents) || !cents)) throw new Error('Please enter the total of the receipt.');
                const res = await api(`/receipts/${scan.token}/attach`, { method: 'POST', body });
                close({ transaction: res.transaction, parts: splitBox.checked ? scan.parts : null });
            }));

            clear(body).append(
                el('div', { class: 'scan-review' },
                    preview,
                    el('div', {},
                        el('div', { class: 'grid-2', style: { gap: '0 12px' } },
                            el('div', { class: 'form-group' }, el('label', {}, 'Merchant'), merchant),
                            el('div', { class: 'form-group' }, el('label', {}, 'Date'), date)),
                        el('div', { class: 'form-group' }, el('label', {}, 'Total (€)'), total),
                        scan.parts.length
                            ? el('details', { class: 'small', style: { marginBottom: '12px' } },
                                el('summary', {}, `${scan.parts.length} item(s) recognised`),
                                el('table', { class: 'data' }, el('tbody', {}, scan.parts.map(p => el('tr', {},
                                    el('td', {}, p.note), el('td', { class: 'num' }, fmtMoney(-p.amount_cents)))))))
                            : el('p', { class: 'muted small' }, 'No individual items recognised.'))),
                el('h3', { style: { marginTop: '8px' } }, scan.candidates.length ? 'Matching transaction' : 'No transaction with this total found'),
                choices,
                scan.parts.length >= 2 ? el('label', { class: 'inline-label', style: { margin: '12px 0' } }, splitBox, 'Split the transaction into the recognised items afterwards') : null,
                el('div', { class: 'actions' },
                    el('button', { type: 'button', class: 'btn-light', onclick: () => close(null) }, 'Cancel'), save));
        };

        start();
        return el('div', {}, body, camera, picker);
    }, { wide: true });
}
