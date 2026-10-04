// HTTP helper, shared state and small DOM/format utilities.

function csrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)ft_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
}

export class ApiError extends Error {
    constructor(status, message) {
        super(message);
        this.status = status;
    }
}

export async function api(path, { method = 'GET', body, query, form } = {}) {
    let url = '/api' + path;
    if (query) {
        const qs = toQuery(query);
        if (qs) url += '?' + qs;
    }
    const headers = {};
    if (method !== 'GET') headers['X-CSRF-Token'] = csrfToken();
    let payload;
    if (form) payload = form;
    else if (body !== undefined) {
        headers['Content-Type'] = 'application/json';
        payload = JSON.stringify(body);
    }
    const res = await fetch(url, { method, headers, body: payload, credentials: 'same-origin' });
    if (res.status === 401 && path !== '/auth/login' && path !== '/auth/me') {
        window.dispatchEvent(new CustomEvent('ft:unauthorized'));
    }
    if (!res.ok) {
        let msg = res.statusText;
        try {
            const data = await res.json();
            if (typeof data.detail === 'string') msg = data.detail;
            else if (Array.isArray(data.detail)) msg = data.detail.map(d => `${d.loc.slice(-1)[0]}: ${d.msg}`).join(', ');
        } catch { /* not JSON */ }
        throw new ApiError(res.status, msg);
    }
    if (res.status === 204) return null;
    return res.json();
}

export function toQuery(obj) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(obj)) {
        if (v === undefined || v === null || v === '') continue;
        if (Array.isArray(v)) v.forEach(x => p.append(k, x));
        else p.append(k, v);
    }
    return p.toString();
}

// ---- shared state

export const state = {
    user: null,
    accounts: [],
    categories: [],
    tags: [],
    // Filters shared between dashboard and transactions (month works with or without a year).
    filters: { account_id: '', year: '', month: '' },
};

export async function loadRefs() {
    const [accounts, categories, tags] = await Promise.all([api('/accounts'), api('/categories'), api('/tags')]);
    Object.assign(state, { accounts, categories, tags });
}

export const categoryById = id => state.categories.find(c => c.id === id);

// ---- themes

export const THEMES = [
    ['clean', 'Clean', 'Flat, light and neutral with one indigo accent (default)'],
    ['classic', 'Classic', 'The original purple gradient look'],
    ['midnight', 'Midnight', 'Dark, data-first dashboard'],
    ['paper', 'Paper', 'Warm, editorial, serif headings'],
];

export function getTheme() {
    try { return localStorage.getItem('ft-theme') || 'clean'; } catch { return 'clean'; }
}

export function applyTheme(name) {
    if (name && name !== 'classic') document.documentElement.dataset.theme = name;
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem('ft-theme', name || 'classic'); } catch { /* storage unavailable */ }
}

export const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---- DOM

export function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    let value;
    for (const [k, v] of Object.entries(attrs || {})) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') node.className = v;
        else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
        else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
        else if (k === 'dataset') Object.assign(node.dataset, v);
        else if (k === 'value') value = v;
        else if (k === 'checked' || k === 'selected' || k === 'disabled') node[k] = !!v;
        else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat(Infinity)) {
        if (c === null || c === undefined || c === false) continue;
        node.append(c instanceof Node ? c : String(c));
    }
    if (value !== undefined) node.value = value;
    return node;
}

export function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
}

export function options(items, selected, placeholder) {
    const out = [];
    if (placeholder !== undefined) out.push(el('option', { value: '' }, placeholder));
    for (const it of items) {
        const [value, label] = Array.isArray(it) ? it : [it, it];
        out.push(el('option', { value: String(value), selected: String(value) === String(selected ?? '') }, label));
    }
    return out;
}

export function categoryOptions(selected, placeholder = 'Select category') {
    return options(state.categories.map(c => [c.id, c.name]), selected, placeholder);
}

export function accountOptions(selected, placeholder) {
    return options(state.accounts.map(a => [a.id, a.name]), selected, placeholder);
}

// ---- formatting

const money = new Intl.NumberFormat('de-DE', { style: 'currency', currency: 'EUR' });
export const fmtMoney = cents => money.format((cents || 0) / 100);
export const fmtSigned = cents => (cents >= 0 ? '+' : '-') + money.format(Math.abs(cents) / 100);

export function fmtDate(iso) {
    if (!iso) return '';
    const [y, m, d] = iso.slice(0, 10).split('-');
    return `${d}.${m}.${y}`;
}

export function todayIso() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export const MONTHS = Array.from({ length: 12 }, (_, i) =>
    new Date(2000, i, 1).toLocaleString('en', { month: 'long' }));

/** Parse "12,50", "1.234,56" or "12.50" into cents. Returns NaN when invalid. */
export function parseMoney(text) {
    let s = String(text || '').trim().replace(/[€\s]/g, '');
    if (!s) return NaN;
    if (s.includes(',') && s.includes('.')) {
        s = s.lastIndexOf(',') > s.lastIndexOf('.') ? s.replace(/\./g, '').replace(',', '.') : s.replace(/,/g, '');
    } else if (s.includes(',')) {
        s = s.replace(',', '.');
    }
    const n = Number(s);
    return Number.isFinite(n) ? Math.round(n * 100) : NaN;
}

export const centsToInput = cents => (Math.abs(cents) / 100).toFixed(2).replace('.', ',');

// ---- feedback

let toastTimer;
export function toast(message, { error = false, action, actionLabel, timeout = 4000 } = {}) {
    document.getElementById('toast')?.remove();
    clearTimeout(toastTimer);
    const node = el('div', { id: 'toast', class: error ? 'error' : '' }, el('span', {}, message));
    if (action) {
        node.append(el('button', {
            class: 'btn btn-sm', onclick: () => { node.remove(); action(); },
        }, actionLabel || 'OK'));
    }
    document.body.append(node);
    toastTimer = setTimeout(() => node.remove(), action ? Math.max(timeout, 10000) : timeout);
}

export function showError(err) {
    console.error(err);
    toast(err.message || String(err), { error: true });
}

/** Simple modal. `build(close)` returns the content node. Resolves when closed. */
export function modal(title, build, { wide = false } = {}) {
    return new Promise(resolve => {
        const close = value => { backdrop.remove(); resolve(value); };
        const box = el('div', { class: wide ? 'modal modal-wide' : 'modal' }, el('h2', {}, title));
        const backdrop = el('div', { class: 'modal-backdrop', onclick: e => { if (e.target === backdrop) close(); } }, box);
        box.append(build(close));
        document.body.append(backdrop);
        box.querySelector('input, select, textarea, button')?.focus();
    });
}

export function confirmDialog(message, { danger = false, confirmLabel = 'OK', requireText } = {}) {
    return modal('Please confirm', close => {
        const input = requireText ? el('input', { placeholder: `Type ${requireText} to confirm` }) : null;
        return el('div', {},
            el('p', { style: { marginBottom: '16px' } }, message),
            input,
            el('div', { class: 'actions' },
                el('button', { class: 'btn-light', onclick: () => close(false) }, 'Cancel'),
                el('button', {
                    class: danger ? 'btn-danger' : 'btn',
                    onclick: () => {
                        if (input && input.value.trim() !== requireText) { input.focus(); return; }
                        close(true);
                    },
                }, confirmLabel)));
    });
}

/** Run an async action, disabling `button` meanwhile and showing errors as a toast. */
export async function run(button, fn) {
    if (button) button.disabled = true;
    try {
        return await fn();
    } catch (e) {
        showError(e);
    } finally {
        if (button) button.disabled = false;
    }
}
