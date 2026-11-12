(function () {
    const $ = (id) => document.getElementById(id);
    const fmtMoney = (v) => v == null ? '—' : (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    const TF_TO_STRATEGY = {
        "60":   "quick",
        "300":  "rulebook",
        "900":  "rulebook",
        "3600": "rulebook",
    };
    const STRATEGY_LABEL = {
        "quick":    "Quick M1 (EMA+RSI+MACD+BB+Stoch)",
        "rulebook": "Rulebook (SMC)",
    };

    function statusPill(st) {
        const pill = $('bot-status-pill');
        if (!st.controller.started) { pill.className = 'badge bg-danger fs-6'; pill.textContent = '⛔ Thread dead'; return; }
        if (st.controller.paused)   { pill.className = 'badge bg-warning text-dark fs-6'; pill.textContent = '⏸ Paused'; return; }
        pill.className = 'badge bg-success fs-6'; pill.textContent = '🟢 Running';
    }

    function updateStrategyLabel(tf) {
        const strat = TF_TO_STRATEGY[String(tf)] || "rulebook";
        $('lbl-strategy').textContent = STRATEGY_LABEL[strat] || strat;
    }

    async function refresh() {
        try {
            const r = await fetch('/api/bot/status', { credentials: 'same-origin' });
            if (r.status === 401) { location.href = '/login'; return; }
            if (!r.ok) throw new Error('status ' + r.status);
            const d = await r.json();
            statusPill(d);
            $('mode').textContent    = d.mode;
            $('account').textContent = d.account;
            $('mtg').textContent     = d.mtg_enabled ? 'ON' : 'OFF';
            $('asset-count').textContent = (d.whitelist || []).length;

            const rk = d.risk || {};
            $('r-balance').textContent = '$' + (rk.balance ?? 0).toFixed(2);
            const pnl = rk.day_pnl ?? 0;
            $('r-day-pnl').textContent = fmtMoney(pnl);
            $('r-day-pnl').className = 'h5 mb-0 ' + (pnl > 0 ? 'text-success' : pnl < 0 ? 'text-danger' : '');
            $('r-consec').textContent = rk.consec_losses ?? 0;
            $('r-cooldown').textContent = rk.cooldown_until ? rk.cooldown_until.slice(11, 16) + ' UTC' : '—';
            $('readonly-banner').classList.toggle('d-none', !rk.read_only);

            const sum = d.summary || {};
            for (const k of ['all', 'base', 'mtg']) {
                const a = sum[k] || {};
                $('s-' + k + '-winrate').textContent = a.win_rate == null ? '—' : a.win_rate + '%';
                $('s-' + k + '-pf').textContent = a.profit_factor == null ? '—' : a.profit_factor;
                const cell = $('s-' + k + '-pnl');
                cell.textContent = fmtMoney(a.pnl ?? 0);
                cell.className = (a.pnl ?? 0) > 0 ? 'text-success' : (a.pnl ?? 0) < 0 ? 'text-danger' : '';
            }

            const tbody = $('signals-body');
            if (!d.recent || d.recent.length === 0) {
                tbody.innerHTML = '<tr><td colspan="10" class="text-center text-secondary">No signals yet</td></tr>';
            } else {
                tbody.innerHTML = d.recent.map(s => {
                    const sideCls = s.direction === 'BUY' ? 'success' : 'danger';
                    const stCls = ({WIN:'success',LOSS:'danger',DRAW:'secondary',PENDING:'info',VOID:'warning'})[s.status] || 'secondary';
                    const pnlCls = (s.pnl ?? 0) > 0 ? 'text-success' : (s.pnl ?? 0) < 0 ? 'text-danger' : '';
                    const step = s.step === 0 ? 'base' : 'MTG' + s.step;
                    return `<tr>
                        <td class="small">${esc((s.ts || '').replace('T',' ').slice(0,19))}</td>
                        <td>${esc(s.asset)}</td>
                        <td><span class="badge bg-${sideCls}">${esc(s.direction)}</span></td>
                        <td><span class="badge bg-secondary">${esc(step)}</span></td>
                        <td>${s.entry_price ?? '—'}</td>
                        <td>${s.stake != null ? '$' + Number(s.stake).toFixed(2) : '—'}</td>
                        <td>${s.payout_pct ?? '—'}</td>
                        <td>${s.score ?? '—'}</td>
                        <td><span class="badge bg-${stCls}">${esc(s.status)}</span></td>
                        <td class="${pnlCls}">${fmtMoney(s.pnl ?? 0)}</td>
                    </tr>`;
                }).join('');
            }
            $('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
        } catch (e) { console.error(e); }
    }

    async function post(url) {
        const r = await fetch(url, { method: 'POST', credentials: 'same-origin' });
        if (r.status === 401) { location.href = '/login'; return; }
        await refresh();
    }

    document.addEventListener('DOMContentLoaded', () => {
        $('btn-start').onclick   = () => post('/api/bot/start');
        $('btn-stop').onclick    = () => post('/api/bot/stop');
        $('btn-refresh').onclick = refresh;

        $('sel-timeframe').addEventListener('change', () => {
            updateStrategyLabel($('sel-timeframe').value);
        });

        fetch('/api/settings', { credentials: 'same-origin' })
            .then(r => r.json())
            .then(s => {
                if (s.web_timeframe_seconds) {
                    $('sel-timeframe').value = String(s.web_timeframe_seconds);
                    updateStrategyLabel(s.web_timeframe_seconds);
                }
            })
            .catch(() => {});

        $('btn-apply-settings').onclick = async () => {
            const body = { web_timeframe_seconds: parseInt($('sel-timeframe').value, 10) };
            const el = $('settings-status');
            el.textContent = 'Applying…'; el.className = 'small text-secondary';
            try {
                const r = await fetch('/api/settings', {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body),
                });
                const j = await r.json();
                if (r.ok) {
                    el.textContent = `Applied: ${j.applied.strategy_mode} @ ${j.applied.web_timeframe_seconds / 60}min`;
                    el.className = 'small text-success';
                    updateStrategyLabel(j.applied.web_timeframe_seconds);
                    setTimeout(() => { el.textContent = ''; }, 5000);
                } else {
                    el.textContent = j.error || 'Failed';
                    el.className = 'small text-danger';
                }
            } catch (e) {
                el.textContent = 'Network error';
                el.className = 'small text-danger';
            }
            refresh();
        };

        refresh();
        setInterval(refresh, 5000);
    });
})();
