import { api, applyTheme, clear, el, getTheme, loadRefs, run, showError, state } from './api.js';
import * as dashboard from './dashboard.js';
import * as transactions from './transactions.js';
import * as importer from './import.js';
import * as accounts from './accounts.js';
import * as categories from './categories.js';
import * as budgets from './budgets.js';
import * as recurring from './recurring.js';
import * as settings from './settings.js';
import * as admin from './admin.js';

applyTheme(getTheme());

const routes = { dashboard, transactions, import: importer, accounts, categories, budgets, recurring, settings, admin };
const view = document.getElementById('view');

async function render() {
    const name = (location.hash.replace(/^#\/?/, '').split('?')[0]) || 'dashboard';
    const page = routes[name] || dashboard;
    document.querySelectorAll('nav.tabs a').forEach(a => a.classList.toggle('active', a.dataset.route === name));
    document.querySelector('nav.tabs a.active')?.scrollIntoView({ block: 'nearest', inline: 'center' });
    clear(view);
    try {
        await page.render(view);
    } catch (e) {
        showError(e);
    }
}

function showLogin(cfg, ssoError) {
    const allowRegistration = cfg.allow_registration;
    document.getElementById('nav').classList.add('hidden');
    document.getElementById('userBox').classList.add('hidden');
    clear(view);
    let registering = false;
    const username = el('input', { id: 'username', autocomplete: 'username', required: true });
    const password = el('input', { id: 'password', type: 'password', autocomplete: 'current-password', required: true });
    const status = el('div', { class: 'status error hidden' });
    const submit = el('button', { class: 'btn', type: 'submit', style: { width: '100%' } }, 'Log in');
    const toggle = allowRegistration
        ? el('p', { class: 'muted', style: { textAlign: 'center', marginTop: '14px' } },
            el('a', { href: '#', onclick: e => {
                e.preventDefault();
                registering = !registering;
                submit.textContent = registering ? 'Create account' : 'Log in';
                e.target.textContent = registering ? 'I already have an account' : 'Create an account';
            } }, 'Create an account'))
        : null;
    const form = el('form', {
        onsubmit: e => {
            e.preventDefault();
            status.classList.add('hidden');
            run(submit, async () => {
                try {
                    await api(registering ? '/auth/register' : '/auth/login', {
                        method: 'POST', body: { username: username.value.trim(), password: password.value },
                    });
                } catch (err) {
                    status.textContent = err.message;
                    status.classList.remove('hidden');
                    return;
                }
                await start();
            });
        },
    },
    el('div', { class: 'form-group' }, el('label', { for: 'username' }, 'Username'), username),
    el('div', { class: 'form-group' }, el('label', { for: 'password' }, 'Password'), password),
    submit, status, toggle);
    const ssoStatus = ssoError ? el('div', { class: 'status error', style: { marginTop: 0, marginBottom: '16px' } }, ssoError) : null;
    const ssoButton = cfg.oidc_enabled
        ? el('a', { class: 'btn', href: '/api/auth/oidc/login', style: { display: 'block', textAlign: 'center' } },
            `Log in with ${cfg.oidc_display_name}`)
        : null;
    const divider = ssoButton && cfg.password_login
        ? el('p', { class: 'muted small', style: { textAlign: 'center', margin: '16px 0' } }, 'or use a local account')
        : null;
    if (ssoButton && cfg.password_login) {
        submit.className = 'btn-light';
    }
    view.append(el('div', { class: 'card login-box' },
        el('img', { class: 'login-logo', src: '/static/img/logo.png', alt: 'Proud Ledger – Private Cloud Ledger', width: 560, height: 341 }),
        ssoStatus, ssoButton, divider,
        cfg.password_login ? form : null));
    if (cfg.password_login) username.focus();
}

/** An SSO failure comes back as /?sso_error=…; show it once and clean up the URL. */
function takeSsoError() {
    const params = new URLSearchParams(location.search);
    const msg = params.get('sso_error');
    if (msg) history.replaceState(null, '', location.pathname + location.hash);
    return msg;
}

async function start() {
    try {
        state.user = await api('/auth/me');
    } catch {
        const cfg = await api('/auth/config')
            .catch(() => ({ allow_registration: false, password_login: true, oidc_enabled: false }));
        showLogin(cfg, takeSsoError());
        return;
    }
    await loadRefs();
    state.receipts = await api('/receipts/config').catch(() => ({ enabled: false }));
    document.getElementById('nav').classList.remove('hidden');
    document.getElementById('userBox').classList.remove('hidden');
    document.getElementById('userName').textContent = `👤 ${state.user.username}`;
    document.getElementById('adminTab').classList.toggle('hidden', !state.user.is_admin);
    await render();
}

/** Discreet version line at the bottom of every page, including the login page. */
async function showVersion() {
    try {
        const v = await api('/version');
        document.getElementById('appFooter').textContent =
            `${v.name} v${v.version}` + (v.commit ? ` · ${v.commit}` : '');
    } catch { /* footer stays hidden */ }
}

showVersion();

window.addEventListener('hashchange', () => { if (state.user) render(); });
window.addEventListener('ft:unauthorized', () => { state.user = null; start(); });
document.getElementById('logoutLink').addEventListener('click', async e => {
    e.preventDefault();
    await api('/auth/logout', { method: 'POST' }).catch(() => {});
    state.user = null;
    location.hash = '';
    start();
});

start();
