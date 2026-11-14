'use strict';
(async () => {
    const form = document.getElementById('login-form');
    const status = document.getElementById('telegram-status');
    const webApp = window.Telegram?.WebApp;
    if (!webApp) return;
    webApp.ready();
    if (!webApp.initData || form.dataset.manual === '1') return;
    form.hidden = true;
    status.hidden = false;
    status.textContent = 'Выполняется вход через Telegram…';
    try {
        const response = await fetch('/api/auth/telegram', {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({init_data: webApp.initData, next: form.elements.next.value})
        });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось войти через Telegram');
        // The destination is restricted to the same-origin SPA by the server.
        location.replace(data.next);
    } catch (error) {
        status.textContent = error.message + ' Можно войти по логину и паролю.';
        status.className = 'error';
        form.hidden = false;
    }
})();
