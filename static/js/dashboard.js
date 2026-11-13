(function () {
    const $ = (id) => document.getElementById(id);
    const fmtMoney = (v) => v == null ? '—' : (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const cls = (v) => v > 0 ? 'text-success' : v < 0 ? 'text-danger' : '';
    const STRAT = {60: 'Fast Scalp (1M)', 300: 'Rulebook (SMC 5M)'};
    const label = (tf) => STRAT[tf] || 'Rulebook (SMC)';

    function pill(st) {
        const p = $('bot-status-pill');
        const [c, t] = !st.controller.started ? ['dead', 'Thread dead'] : st.controller.paused ? ['off', 'Paused'] : ['on', 'Running'];
        p.className = 'pill ' + c; p.innerHTML = '<span class="dot"></span>' + t;
    }

    async function refresh() {
        try {
            const r = await fetch('/api/bot/status', { credentials: 'same-origin' });
            if (r.status === 401) { location.href = '/login'; return; }
            const d = await r.json();
            pill(d);
            $('mode').textContent = d.mode; $('account').textContent = d.account; $('feed').textContent = d.feed || '—';
            $('asset-count').textContent = (d.whitelist || []).length;
            
            if (d.signals_today !== undefined) $('live-sig-count').textContent = d.signals_today;
            if (d.max_signals !== undefined) $('live-max-sig').textContent = d.max_signals;

            $('assets').textContent = (d.whitelist || []).slice(0, 12).join(', ') + ((d.whitelist || []).length > 12 ? ' …' : '');
            const rk = d.risk || {}, pnl = rk.day_pnl ?? 0;
            $('r-balance').textContent = '$' + (rk.balance ?? 0).toFixed(2);
            $('r-day-pnl').textContent = fmtMoney(pnl); $('r-day-pnl').className = 'h5 mb-0 ' + cls(pnl);
            $('r-consec').textContent = rk.consec_losses ?? 0;
            $('r-cooldown').textContent = rk.cooldown_until ? rk.cooldown_until.slice(11, 16) + ' UTC' : '—';
            $('readonly-banner').classList.toggle('d-none', !rk.read_only);
            
            for (const k of ['all', 'base', 'mtg']) {
                const a = (d.summary || {})[k] || {};
                $('s-' + k + '-winrate').textContent = a.win_rate == null ? '—' : a.win_rate + '%';
                $('s-' + k + '-pf').textContent = a.profit_factor ?? '—';
                const c = $('s-' + k + '-pnl'); c.textContent = fmtMoney(a.pnl ?? 0); c.className = cls(a.pnl ?? 0);
            }
            
            const tb = $('signals-body');
            tb.innerHTML = !d.recent || !d.recent.length
                ? '<tr><td colspan="10" class="text-center text-secondary">No signals yet</td></tr>'
                : d.recent.map(s => {
                    const sc = s.direction === 'BUY' ? 'success' : 'danger';
                    const st = ({WIN:'success',LOSS:'danger',DRAW:'secondary',PENDING:'info',VOID:'warning'})[s.status] || 'secondary';
                    return `<tr><td class="small">${esc((s.ts || '').replace('T',' ').slice(0,19))}</td><td>${esc(s.asset)}</td>
                      <td><span class="badge bg-${sc}">${esc(s.direction)}</span></td>
                      <td><span class="badge bg-secondary">${s.step === 0 ? 'base' : 'MTG' + s.step}</span></td>
                      <td>${s.entry_price ?? '—'}</td><td>${s.stake != null ? '$' + Number(s.stake).toFixed(2) : '—'}</td>
                      <td>${s.payout_pct ?? '—'}</td><td>${s.score ?? '—'}</td>
                      <td><span class="badge bg-${st}">${esc(s.status)}</span></td>
                      <td class="${cls(s.pnl ?? 0)}">${fmtMoney(s.pnl ?? 0)}</td></tr>`;
                }).join('');
            $('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
        } catch (e) { console.error(e); }
    }

    async function loadHistory() {
        try {
            const r = await fetch('/api/history', { credentials: 'same-origin' });
            if (!r.ok) return;
            const h = await r.json(), t = h.total || {};
            $('h-total').textContent = t.count ? `Total ${t.count} signals · win rate ${t.win_rate ?? '—'}% · P/L ${fmtMoney(t.pnl)}` : 'No finished signals yet';
            if (!h.blocks || !h.blocks.length) return;
            $('history-body').innerHTML = h.blocks.map(b => `<tr><td>#${b.n}${b.complete ? '' : ' <span class="badge bg-secondary">open</span>'}</td>
                <td>${b.count}</td><td>${b.wins}</td><td>${b.mtg_wins}</td><td>${b.losses}</td><td>${b.win_rate ?? '—'}%</td>
                <td class="${cls(b.pnl)}">${fmtMoney(b.pnl)}</td></tr>`).join('');
        } catch (e) { console.error(e); }
    }

    async function post(url) {
        const r = await fetch(url, { method: 'POST', credentials: 'same-origin' });
        if (r.status === 401) { location.href = '/login'; return; }
        await refresh();
    }
    
    const save = (body) => fetch('/api/settings', { method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });

    document.addEventListener('DOMContentLoaded', () => {
        $('btn-start').onclick = () => post('/api/bot/start');
        $('btn-stop').onclick = () => post('/api/bot/stop');
        $('btn-refresh').onclick = () => { refresh(); loadHistory(); };
        $('sel-timeframe').addEventListener('change', () => { $('lbl-strategy').textContent = label(+$('sel-timeframe').value); });
        $('sw-mtg').addEventListener('change', async () => { await save({ mtg_enabled: $('sw-mtg').checked }); refresh(); });
        
        fetch('/api/settings', { credentials: 'same-origin' }).then(r => r.json()).then(s => {
            $('sw-mtg').checked = !!s.mtg_enabled;
            if (s.web_timeframe_seconds) { $('sel-timeframe').value = String(s.web_timeframe_seconds); $('lbl-strategy').textContent = label(s.web_timeframe_seconds); }
            if (s.max_signals_per_day) { $('inp-max-sig').value = s.max_signals_per_day; }
        }).catch(() => {});

        $('btn-apply-settings').onclick = async () => {
            const el = $('settings-status'); el.textContent = 'Applying…'; el.className = 'small text-secondary';
            try {
                const r = await save({ 
                    web_timeframe_seconds: parseInt($('sel-timeframe').value, 10),
                    max_signals_per_day: parseInt($('inp-max-sig').value, 10)
                });
                const j = await r.json();
                el.textContent = r.ok ? `Applied: ${j.applied.strategy_mode} @ ${j.applied.web_timeframe_seconds / 60}min | Max: ${j.applied.max_signals_per_day}` : (j.error || 'Failed');
                el.className = 'small ' + (r.ok ? 'text-success' : 'text-danger');
            } catch (e) { el.textContent = 'Network error'; el.className = 'small text-danger'; }
            refresh();
        };
        refresh(); loadHistory();
        setInterval(refresh, 5000); setInterval(loadHistory, 15000);
    });
})();
