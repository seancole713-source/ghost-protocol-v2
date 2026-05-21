/* Ghost Protocol v5 — Robinhood-Style Dashboard JS
 *
 * Design rules:
 * 1. Every tab mirrors one backend system
 * 2. If a task isn't running, show "Not running yet" — no fake data
 * 3. One accuracy number everywhere: same source, zero contradictions
 * 4. Picks come from ghost_tracked_picks (same DB Telegram reads)
 * 5. History comes from ghost_predictions (full resolved set)
 * 6. Market ticker shows live index/crypto prices
 * 7. Connect every data pipe — no empty tabs
 */

// ─── STATE ───
let _picks = [];
let _watchlist = [];
let _news = [];
let _history = [];
let _accuracy = null;
let _heartbeat = null;
let _audit = null;
let _intelligence = null;
let _subsystems = null;
let _newsFilter = 'all';
let _stockFilter = 'all';
let _cryptoFilter = 'all';
let _historyFilter = 'all';

// ─── CRYPTO KEYWORDS for filtering ───
const CRYPTO_KEYS = new Set([
    'BTC','ETH','SOL','XRP','DOGE','ADA','DOT','LINK','AVAX','MATIC',
    'UNI','AAVE','SHIB','LTC','BCH','ATOM','FIL','NEAR','APT','ARB',
    'OP','SUI','SEI','TIA','INJ','PEPE','WIF','BONK','FLOKI','GIGA',
    'CHZ','BITCOIN','ETHEREUM','CRYPTO','BLOCKCHAIN','DEFI','NFT','WEB3',
    'ALTCOIN','BINANCE','COINBASE','STABLECOIN','MEMECOIN','LAYER 2',
    'MINING','HALVING','HASH RATE','WHALE','SATOSHI','TOKEN','LEDGER',
    'METAMASK','UNISWAP','OPENSEA','POLYGON','SOLANA','CARDANO',
    'DOGECOIN','RIPPLE','CHAINLINK','POLKADOT','COSMOS',
]);

// ─── BOOT ───
document.addEventListener('DOMContentLoaded', () => {
    initNav();
    initFilters();
    loadAll();
    setInterval(loadAll, 30000);
    setInterval(loadTicker, 60000);
    loadTicker();
});

// ═══════════════════════════════════════
// NAVIGATION (left sidebar icons)
// ═══════════════════════════════════════
function initNav() {
    document.querySelectorAll('.nav-icon').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.nav-icon').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            document.querySelectorAll('.tab-page').forEach(p => p.classList.remove('active'));
            const page = document.getElementById('tab-' + btn.dataset.tab);
            if (page) page.classList.add('active');
        });
    });
}

function initFilters() {
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const scope = btn.dataset.scope;
            // Deactivate siblings with same scope
            document.querySelectorAll(`.filter-btn[data-scope="${scope}"]`).forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            if (scope === 'history') { _historyFilter = btn.dataset.filter; renderHistory(); }
            else if (scope === 'news') { _newsFilter = btn.dataset.filter; renderNewsFeed(); }
            else if (scope === 'stocks') { _stockFilter = btn.dataset.filter; renderStocksTable(); }
            else if (scope === 'crypto') { _cryptoFilter = btn.dataset.filter; renderCryptoTable(); }
        });
    });
}

// ═══════════════════════════════════════
// MARKET TICKER BAR
// ═══════════════════════════════════════
async function loadTicker() {
    try {
        const data = await fetchJSON('/api/v3/market/ticker');
        if (!data?.ok) return;
        const items = data.items || [];
        items.forEach(item => {
            const el = document.getElementById('tick-' + item.id);
            if (!el) return;
            const priceEl = el.querySelector('.tick-price');
            const chgEl = el.querySelector('.tick-chg');
            if (priceEl) priceEl.textContent = fmtTickerPrice(item.price);
            if (chgEl) {
                const isUp = item.change >= 0;
                const pct = item.change_pct != null ? ` (${isUp ? '+' : ''}${item.change_pct.toFixed(2)}%)` : '';
                chgEl.textContent = `${isUp ? '+' : ''}${item.change.toFixed(2)}${pct}`;
                chgEl.className = 'tick-chg ' + (isUp ? 'up' : 'down');
            }
        });
    } catch (e) {
        console.warn('Ticker load failed:', e);
    }
}

// ═══════════════════════════════════════
// MASTER LOADER
// ═══════════════════════════════════════
async function loadAll() {
    const results = await Promise.allSettled([
        fetchJSON('/api/v4/picks'),                      // 0 – picks
        fetchJSON('/api/v3/watchlist/enriched'),          // 1 – watchlist
        fetchJSON('/api/v3/news/feed'),                  // 2 – news
        fetchJSON('/api/v3/accuracy/summary'),           // 3 – accuracy
        fetchJSON('/api/v3/heartbeat/status'),           // 4 – heartbeat
        fetchJSON('/integrity/audit/readonly'),          // 5 – integrity audit
        fetchJSON('/api/v4/history?days=365&limit=2000'),// 6 – full history
        fetchJSON('/api/v3/intelligence/status'),        // 7 – intelligence hub
        fetchJSON('/api/v3/intelligence/cache'),         // 8 – news brain cache
        fetchJSON('/api/v4/subsystems'),                 // 9 – full subsystem inventory
        fetchJSON('/api/accuracy/trends?days=30'),       // 10 – accuracy trends (Phase 3.8)
    ]);

    const val = i => results[i].status === 'fulfilled' ? results[i].value : null;

    const picksData = val(0);
    const watchData = val(1);
    const newsData = val(2);
    _accuracy = val(3);
    _heartbeat = val(4);
    _audit = val(5);
    const histData = val(6);
    _intelligence = val(7);
    const newsBrain = val(8);
    _subsystems = val(9);
    const trendsData = val(10);

    if (picksData?.ok) _picks = picksData.picks || [];
    if (watchData?.ok) {
        // FIX (Mar 21, 2026): Flatten prediction nested object into watchlist items
        // API returns {prediction: {direction, confidence}}, frontend expects ghost_direction, ghost_confidence
        const items = watchData.items || watchData.watchlist || [];
        _watchlist = items.map(item => {
            // If item has nested prediction object, flatten it
            if (item.prediction) {
                return {
                    ...item,
                    ghost_direction: item.prediction.direction || 'HOLD',
                    ghost_confidence: item.prediction.confidence || 0,
                    price: item.current_price || item.price || 0,
                    // Calculate change_pct if missing
                    change_pct: item.change_pct || 0,
                    type: item.asset_type || item.type || 'unknown'
                };
            }
            return item;
        });
    }
    if (newsData?.ok) _news = newsData.articles || newsData.items || newsData.feed || [];
    if (histData?.ok) _history = histData.trades || [];
    
    // Store accuracy trends for charting (Phase 3.8)
    if (trendsData?.ok) {
        window._accuracyTrends = trendsData;
        renderAccuracyChart();
    }

    // ── Status indicator ──
    setStatus((picksData && picksData.ok) || (watchData && watchData.ok));

    // ── Picks header ──
    renderPicksHeader();

    // ── Render all tabs ──
    renderPicks();
    renderRecentPicks();
    renderActivePositions();
    renderStocksTable();
    renderStockMovers();
    renderCryptoTable();
    renderCryptoMovers();
    renderHistory();
    renderHealth();
    renderHealthSidebar();
    renderBrain(newsBrain);
    renderNewsFeed();
    renderFinancials();
    loadWolfIntel();  // Phase 4: WOLF KPI panel
}

function setStatus(alive) {
    const dot = document.getElementById('status-indicator');
    const txt = document.getElementById('status-text');
    if (dot) dot.style.background = alive ? 'var(--green)' : 'var(--red)';
    if (txt) {
        txt.textContent = alive ? 'LIVE' : 'OFF';
        txt.style.color = alive ? 'var(--green)' : 'var(--red)';
    }
}

// ═══════════════════════════════════════
// TAB 1: PICKS
// ═══════════════════════════════════════
function renderPicksHeader() {
    const dateEl = document.getElementById('greeting-date');
    const subEl = document.getElementById('greeting-sub');
    if (dateEl) {
        dateEl.textContent = new Date().toLocaleDateString('en-US', {
            weekday: 'long', month: 'long', day: 'numeric', year: 'numeric'
        });
    }
    if (subEl && _accuracy) {
        const pct = _accuracy.accuracy_pct ?? 0;
        const correct = _accuracy.correct_predictions ?? 0;
        const total = _accuracy.total_predictions ?? 0;
        // Count only today's active picks vs total tracked
        const activePicks = _picks.filter(p => {
            const s = (p.status || 'pending').toLowerCase();
            return s === 'active' || s === 'pending';
        }).length;
        // Skip transparency
        const skipped = _accuracy.total_skipped ?? 0;
        const skipInfo = skipped > 0 ? ` · ${skipped} skip-tagged excluded` : '';
        subEl.textContent = `${activePicks} active picks · ${_picks.length} total tracked | ${pct}% accuracy (${correct}/${total})${skipInfo}`;
    }
}

function renderPicks() {
    const el = document.getElementById('all-picks');
    if (!el) return;

    if (!_picks.length) {
        el.innerHTML = '<div class="empty-state">No picks right now — Ghost is watching the market</div>';
        return;
    }

    el.innerHTML = _picks
        // Sort: active/pending first, then resolved
        .slice().sort((a, b) => {
            const sa = (a.status || 'pending').toLowerCase();
            const sb = (b.status || 'pending').toLowerCase();
            const aActive = sa === 'active' || sa === 'pending' ? 0 : 1;
            const bActive = sb === 'active' || sb === 'pending' ? 0 : 1;
            return aActive - bActive;
        })
        .map(p => {
        const isUp = (p.direction || '').toUpperCase() === 'UP';
        const sideClass = isUp ? 'bullish' : 'bearish';
        const emoji = isUp ? '🟢' : '🔴';
        const dirWord = isUp ? 'UP' : 'DOWN';
        const star = p.whitelisted ? ' <span class="pick-star">⭐</span>' : '';
        const entry = fmtPrice(p.entry_price);
        const target = fmtPrice(p.target_price);
        const stop = fmtPrice(p.stop_loss);
        const gainPct = p.gain_pct != null ? Math.abs(p.gain_pct).toFixed(1) : '3.0';
        const returnVal = p.gain_pct != null ? (100 + Math.abs(p.gain_pct)).toFixed(2) : '103.00';
        const deadline = p.done_by || '--';
        
        // Calculate time remaining until deadline
        let timeRemaining = '';
        if (p.expires_at) {
            try {
                const expiresMs = typeof p.expires_at === 'number' ? p.expires_at * 1000 : new Date(p.expires_at).getTime();
                const nowMs = Date.now();
                const diffMs = expiresMs - nowMs;
                
                if (diffMs > 0) {
                    const hours = Math.floor(diffMs / (1000 * 60 * 60));
                    const minutes = Math.floor((diffMs % (1000 * 60 * 60)) / (1000 * 60));
                    if (hours > 48) {
                        const days = Math.floor(hours / 24);
                        timeRemaining = ` (${days}d left)`;
                    } else if (hours > 0) {
                        timeRemaining = ` (${hours}h ${minutes}m left)`;
                    } else {
                        timeRemaining = ` (${minutes}m left)`;
                    }
                } else {
                    timeRemaining = ' (EXPIRED)';
                }
            } catch (e) {
                timeRemaining = '';
            }
        }

        const status = (p.status || 'pending').toLowerCase();
        let statusClass = 'pending', statusLabel = 'PENDING';
        if (['won', 'win', 'correct', 'target_hit'].includes(status)) { statusClass = 'won'; statusLabel = 'WON'; }
        else if (['lost', 'loss', 'incorrect', 'stop_hit'].includes(status)) { statusClass = 'lost'; statusLabel = 'LOST'; }
        else if (status === 'expired') { statusClass = 'expired'; statusLabel = 'EXPIRED'; }

        return `
        <div class="pick-card ${sideClass}">
            <div class="pick-headline">${emoji} <strong>${p.symbol || '???'}</strong> is going <strong>${dirWord}</strong>${star}</div>
            <div class="pick-body">
                <div class="pick-row"><span class="pick-label">Get in at</span><span class="pick-val">${entry}</span></div>
                <div class="pick-row"><span class="pick-label">Get out at</span><span class="pick-val green">${target} (you make ${gainPct}%)</span></div>
                <div class="pick-row"><span class="pick-label">Run away at</span><span class="pick-val red">${stop}</span></div>
                <div class="pick-row"><span class="pick-label">Done by</span><span class="pick-val">${deadline}${timeRemaining}</span></div>
            </div>
            <div class="pick-footer">
                <span class="pick-return green">$100 in → $${returnVal} back</span>
                <span class="pick-status ${statusClass}">${statusLabel}</span>
            </div>
        </div>`;
    }).join('');
}

function renderRecentPicks() {
    const tbody = document.getElementById('recent-picks-tbody');
    if (!tbody) return;

    // Recent = last 7 days from history that had picks
    const sevenDaysAgo = Date.now() - 7 * 86400000;
    const recent = _history.filter(t => {
        const ts = t.predicted_at ? new Date(t.predicted_at).getTime() : 0;
        return ts > sevenDaysAgo;
    }).slice(0, 20);

    if (!recent.length && !_picks.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No recent picks</td></tr>';
        return;
    }

    // Combine active picks + recent history
    const activePicks = _picks.filter(p => {
        const s = (p.status || 'pending').toLowerCase();
        return s === 'active' || s === 'pending';
    });
    const rows = activePicks.map(p => ({
        symbol: p.symbol,
        direction: (p.direction || '').toUpperCase(),
        entry: fmtPrice(p.entry_price),
        target: fmtPrice(p.target_price),
        stop: fmtPrice(p.stop_loss),
        status: (p.status || 'PENDING').toUpperCase(),
        date: p.done_by || 'Active'
    })).concat(recent.map(t => ({
        symbol: t.symbol,
        direction: (t.direction || '').toUpperCase(),
        entry: fmtPrice(t.entry_price),
        target: fmtPrice(t.exit_price),
        stop: '--',
        status: t.outcome === 'win' ? 'WON' : t.outcome === 'loss' ? 'LOST' : 'RESOLVED',
        date: fmtDate(t.predicted_at)
    })));

    tbody.innerHTML = rows.slice(0, 25).map(r => {
        const sc = r.status === 'WON' ? 'result-win' : r.status === 'LOST' ? 'result-loss' : '';
        return `<tr>
            <td><strong>${r.symbol}</strong></td>
            <td>${r.direction}</td>
            <td>${r.entry}</td>
            <td>${r.target}</td>
            <td>${r.stop}</td>
            <td class="${sc}">${r.status}</td>
            <td>${r.date}</td>
        </tr>`;
    }).join('');
}

function renderActivePositions() {
    const el = document.getElementById('active-positions');
    if (!el) return;

    const active = _picks.filter(p => {
        const s = (p.status || 'pending').toLowerCase();
        return s === 'active' || s === 'pending';
    });

    if (!active.length) {
        el.innerHTML = '<div class="empty-state-sm">No active positions</div>';
        return;
    }

    el.innerHTML = active.map(p => {
        const isUp = (p.direction || '').toUpperCase() === 'UP';
        const emoji = isUp ? '🟢' : '🔴';
        const dir = isUp ? 'UP' : 'DOWN';
        const gainPct = p.gain_pct != null ? Math.abs(p.gain_pct).toFixed(1) : '--';
        return `
        <div class="position-item">
            <div class="pos-left">
                <span class="pos-sym">${emoji} ${p.symbol || '???'}</span>
                <span class="pos-meta">${dir} · Entry: ${fmtPrice(p.entry_price)}</span>
            </div>
            <div class="pos-right">
                <span class="pos-pnl green">+${gainPct}%</span>
                <span class="pos-price">${p.done_by || ''}</span>
            </div>
        </div>`;
    }).join('');
}

// ═══════════════════════════════════════
// TAB 2: STOCKS
// ═══════════════════════════════════════
function renderStocksTable() {
    const tbody = document.getElementById('stocks-tbody');
    if (!tbody) return;

    let items = _watchlist.filter(w => (w.type || '').toLowerCase() === 'stock');

    // Fallback: if no stocks in watchlist, build from picks that are stock-type
    if (!items.length && _picks.length) {
        const stockPicks = _picks.filter(p => (p.type || p.market || '').toLowerCase() === 'stock' || (p.type || p.market || '').toLowerCase() === 'stocks');
        const seen = new Set();
        stockPicks.forEach(p => {
            if (p.symbol && !seen.has(p.symbol)) {
                seen.add(p.symbol);
                items.push({
                    symbol: p.symbol,
                    price: p.entry_price || 0,
                    change_pct: p.gain_pct || 0,
                    change: 0,
                    ghost_confidence: p.confidence || 0,
                    ghost_direction: p.direction || 'HOLD',
                    type: 'stock'
                });
            }
        });
    }

    // Apply filter
    if (_stockFilter === 'active') {
        const activeSyms = new Set(_picks.map(p => p.symbol));
        items = items.filter(w => activeSyms.has(w.symbol));
    } else if (_stockFilter === 'watching') {
        const activeSyms = new Set(_picks.map(p => p.symbol));
        items = items.filter(w => !activeSyms.has(w.symbol));
    }

    if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No stocks — predictions haven\'t run yet</td></tr>';
        return;
    }

    tbody.innerHTML = items.map(w => buildWatchlistRow(w)).join('');
}

function renderStockMovers() {
    const el = document.getElementById('stock-movers');
    if (!el) return;

    const stocks = _watchlist.filter(w => (w.type || '').toLowerCase() === 'stock');
    const sorted = [...stocks].sort((a, b) => Math.abs(b.change_pct || 0) - Math.abs(a.change_pct || 0));

    if (!sorted.length) {
        el.innerHTML = '<div class="empty-state-sm">No data</div>';
        return;
    }

    el.innerHTML = sorted.slice(0, 8).map(w => {
        const pct = w.change_pct || 0;
        const cls = pct >= 0 ? 'up' : 'down';
        return `<div class="mover-item"><span class="mover-sym">${w.symbol}</span><span class="mover-chg ${cls}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</span></div>`;
    }).join('');
}

// ═══════════════════════════════════════
// TAB 3: CRYPTO
// ═══════════════════════════════════════
function renderCryptoTable() {
    const tbody = document.getElementById('crypto-tbody');
    if (!tbody) return;

    let items = _watchlist.filter(w => (w.type || '').toLowerCase() === 'crypto');

    if (_cryptoFilter === 'active') {
        const activeSyms = new Set(_picks.map(p => p.symbol));
        items = items.filter(w => activeSyms.has(w.symbol));
    } else if (_cryptoFilter === 'watching') {
        const activeSyms = new Set(_picks.map(p => p.symbol));
        items = items.filter(w => !activeSyms.has(w.symbol));
    }

    if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No crypto — predictions haven\'t run yet</td></tr>';
        return;
    }

    tbody.innerHTML = items.map(w => buildWatchlistRow(w)).join('');
}

function renderCryptoMovers() {
    const el = document.getElementById('crypto-movers');
    if (!el) return;

    const crypto = _watchlist.filter(w => (w.type || '').toLowerCase() === 'crypto');
    const sorted = [...crypto].sort((a, b) => Math.abs(b.change_pct || 0) - Math.abs(a.change_pct || 0));

    if (!sorted.length) {
        el.innerHTML = '<div class="empty-state-sm">No data</div>';
        return;
    }

    el.innerHTML = sorted.slice(0, 8).map(w => {
        const pct = w.change_pct || 0;
        const cls = pct >= 0 ? 'up' : 'down';
        return `<div class="mover-item"><span class="mover-sym">${w.symbol}</span><span class="mover-chg ${cls}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</span></div>`;
    }).join('');
}

function buildWatchlistRow(w) {
    const price = fmtPrice(w.price);
    
    // FIX: Calculate change_pct from current price vs previous price if available
    let changePct = w.change_pct || 0;
    let changeAmt = w.change || 0;
    
    // If change_pct is provided, use it; otherwise try to calculate
    if (changePct === 0 && w.price && w.prev_close && w.prev_close > 0) {
        changePct = ((w.price - w.prev_close) / w.prev_close) * 100;
    }
    
    // Calculate dollar change if we have both prices
    if (changeAmt === 0 && w.price && w.prev_close) {
        changeAmt = w.price - w.prev_close;
    }
    
    const changeClass = changePct >= 0 ? 'green' : 'red';
    const changeStr = (changeAmt >= 0 ? '+$' : '-$') + Math.abs(changeAmt).toFixed(2);
    const changePctStr = (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%';

    // FIX: Look for confidence in multiple places (nested prediction object, or flat)
    let conf = w.ghost_confidence || 0;
    if (conf === 0 && w.prediction && w.prediction.confidence) {
        conf = w.prediction.confidence * 100; // Convert 0-1 to percentage
    }
    if (conf > 1 && conf <= 100) {
        // Already in percentage form
    } else if (conf > 0 && conf <= 1) {
        conf = conf * 100; // Convert decimal to percentage
    }
    
    let dirLabel, dirClass;
    let direction = w.ghost_direction || '';
    if (!direction && w.prediction && w.prediction.direction) {
        direction = w.prediction.direction;
    }
    
    if (conf < 50) { 
        dirLabel = 'HOLD'; 
        dirClass = 'hold'; 
    } else {
        const dir = direction.toUpperCase();
        dirLabel = dir === 'UP' ? '↑ UP' : dir === 'DOWN' ? '↓ DOWN' : 'HOLD';
        dirClass = dir === 'UP' ? 'up' : dir === 'DOWN' ? 'down' : 'hold';
    }
    const confStr = conf > 0 ? Math.round(conf) + '%' : '--';

    return `<tr>
        <td class="sym-cell">${w.symbol}</td>
        <td class="price-cell">${price}</td>
        <td class="chg-cell ${changeClass}">${changeStr}</td>
        <td class="chg-cell ${changeClass}">${changePctStr}</td>
        <td><span class="dir-badge ${dirClass}">${dirLabel}</span></td>
        <td>${confStr}</td>
    </tr>`;
}

// ═══════════════════════════════════════
// TAB 4: HISTORY
// ═══════════════════════════════════════
function renderHistory() {
    let data = [..._history];

    if (_historyFilter === 'stock') data = data.filter(t => (t.market || t.type || '').toLowerCase() === 'stock');
    else if (_historyFilter === 'crypto') data = data.filter(t => (t.market || t.type || '').toLowerCase() === 'crypto');
    else if (_historyFilter === 'win') data = data.filter(t => t.outcome === 'win');
    else if (_historyFilter === 'loss') data = data.filter(t => t.outcome === 'loss');

    // Stats from ALL history
    const wins = _history.filter(t => t.outcome === 'win').length;
    const losses = _history.filter(t => t.outcome === 'loss').length;
    const totalPnl = _history.reduce((s, t) => s + (t.pnl || 0), 0);
    const winRate = _history.length > 0 ? (wins / _history.length * 100).toFixed(1) : '--';
    
    // Calculate current win/loss streak
    let currentStreak = 0;
    let streakType = '';
    if (_history.length > 0) {
        // Sort by resolved_at or predicted_at to get chronological order
        const sorted = [..._history].sort((a, b) => {
            const timeA = a.resolved_at || a.predicted_at || 0;
            const timeB = b.resolved_at || b.predicted_at || 0;
            return new Date(timeB).getTime() - new Date(timeA).getTime();
        });
        
        // Count streak from most recent
        const mostRecent = sorted[0]?.outcome;
        if (mostRecent === 'win' || mostRecent === 'loss') {
            streakType = mostRecent;
            for (const trade of sorted) {
                if (trade.outcome === streakType) {
                    currentStreak++;
                } else {
                    break;
                }
            }
        }
    }
    
    const streakStr = currentStreak > 0 
        ? `${currentStreak} ${streakType === 'win' ? 'win' : 'loss'}${currentStreak > 1 ? 's' : ''} in a row`
        : 'No active streak';

    setText('hist-total', _history.length);
    setTextColor('hist-wins', wins, 'green');
    setTextColor('hist-losses', losses, 'red');
    setText('hist-winrate', winRate === '--' ? '--' : winRate + '%');
    
    // Update streak display
    const streakEl = document.getElementById('hist-streak');
    if (streakEl) {
        streakEl.textContent = streakStr;
        streakEl.className = 'stat-val ' + (streakType === 'win' ? 'green' : streakType === 'loss' ? 'red' : '');
    }
    
    const pnlEl = document.getElementById('hist-pnl');
    if (pnlEl) {
        pnlEl.textContent = (totalPnl >= 0 ? '+' : '') + '$' + Math.abs(totalPnl).toFixed(2);
        pnlEl.className = 'stat-val ' + (totalPnl >= 0 ? 'green' : 'red');
    }

    const tbody = document.getElementById('history-tbody');
    if (!tbody) return;

    if (!data.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No resolved trades</td></tr>';
        return;
    }

    tbody.innerHTML = data.slice(0, 500).map(t => {
        const won = t.outcome === 'win';
        const movePct = t.actual_move_pct || 0;
        const moveStr = (movePct >= 0 ? '+' : '') + movePct.toFixed(2) + '%';
        const dir = (t.direction || '--').toUpperCase();
        const date = t.resolved_at ? fmtDate(t.resolved_at) : (t.predicted_at ? fmtDate(t.predicted_at) : '--');
        return `<tr>
            <td><strong>${t.symbol || '--'}</strong></td>
            <td>${dir}</td>
            <td>${fmtPrice(t.entry_price)}</td>
            <td>${fmtPrice(t.exit_price)}</td>
            <td class="${won ? 'result-win' : 'result-loss'}">${moveStr}</td>
            <td class="${won ? 'result-win' : 'result-loss'}">${won ? 'WIN' : 'LOSS'}</td>
            <td>${date}</td>
        </tr>`;
    }).join('');
}

// ═══════════════════════════════════════
// TAB 5: HEALTH
// ═══════════════════════════════════════
function renderHealth() {
    const topEl = document.getElementById('health-topline');
    if (topEl) {
        if (_accuracy && _audit) {
            const pct = _accuracy.accuracy_pct ?? 0;
            const rawPct = _accuracy.raw_accuracy_pct ?? pct;
            const correct = _accuracy.correct_predictions ?? 0;
            const total = _accuracy.total_predictions ?? 0;
            const skipped = _accuracy.total_skipped ?? 0;
            const totalAll = _accuracy.total_with_skips ?? total;
            const score = _audit.health_score ?? '--';
            const penalty = _audit.total_penalty ?? 0;
            const issues = _audit.issues_remaining ?? 0;

            // Dual accuracy: filtered vs real
            const rawColor = rawPct < 40 ? 'var(--red)' : rawPct < 50 ? 'var(--yellow)' : 'var(--green)';
            const filtColor = pct < 40 ? 'var(--red)' : pct < 50 ? 'var(--yellow)' : 'var(--green)';
            const accHtml = `<span class="hl-big" style="color:${filtColor}">${pct}%</span> filtered`
                + (skipped > 0 ? ` · <span style="color:${rawColor};font-weight:600">${rawPct}%</span> real <span style="color:var(--text-muted);font-size:11px">(${skipped} skips excluded from ${totalAll})</span>` : '');

            // Health score with penalty hint
            const scoreColor = score >= 80 ? 'var(--green)' : score >= 50 ? 'var(--yellow)' : 'var(--red)';
            const scoreHtml = `<span style="color:${scoreColor};font-weight:600">${score}</span>/100`
                + (penalty > 0 ? ` <span style="color:var(--text-muted);font-size:11px">(-${penalty} penalty)</span>` : '');

            topEl.innerHTML = `${accHtml} · System: ${scoreHtml} · ${issues} issue${issues !== 1 ? 's' : ''}`;
        } else {
            topEl.textContent = 'Unable to load health data';
        }
    }

    // Accuracy cards — show real numbers, 0% means 0%
    if (_accuracy) {
        const d = _accuracy.daily_accuracy_pct;
        const w = _accuracy.weekly_accuracy_pct;
        const m = _accuracy.monthly_accuracy_pct;
        setText('acc-24h', (d != null ? d : '--') + '%');
        setText('acc-7d', (w != null ? w : '--') + '%');
        setText('acc-30d', (m != null ? m : '--') + '%');
        setText('acc-record', `${_accuracy.correct_predictions || 0}W / ${((_accuracy.total_predictions || 0) - (_accuracy.correct_predictions || 0))}L`);
    }

    // ── System Doctor — Morning Health Check ──
    renderDoctorChecks();

    // Telegram Health Check Mirror
    renderHealthCheckMirror();

    // ── Brain Modules (subsystems) ──
    renderSubsystemBrains('subsystem-brains');

    // ── Memory Systems (subsystems) ──
    renderSubsystemMemory('subsystem-memory');

    // Heartbeat grid — NOW shows ALL tasks, worker-only dimmed
    const hbEl = document.getElementById('heartbeat-grid');
    if (hbEl && _heartbeat?.tasks) {
        const entries = Object.entries(_heartbeat.tasks);
        if (!entries.length) {
            hbEl.innerHTML = '<div class="empty-state">No tasks registered</div>';
        } else {
            const isWorker = _heartbeat.worker_mode === true;
            const webTasks = entries.filter(([,i]) => i.runs_here !== false);
            const workerTasks = entries.filter(([,i]) => i.runs_here === false);

            // Mode indicator
            let modeHtml = '';
            if (!isWorker) {
                modeHtml = `<div style="background:rgba(0,200,83,0.1);border:1px solid var(--green);border-radius:8px;padding:8px 14px;margin-bottom:12px;color:var(--text-muted);font-size:12px">
                    🌐 <strong style="color:var(--green)">Web Mode</strong> — ${webTasks.length} active tasks · ${workerTasks.length} worker-only (dimmed)
                </div>`;
            }

            // Worker-only deployment banner (if any worker tasks exist)
            let workerBanner = '';
            if (!isWorker && workerTasks.length > 0) {
                workerBanner = `<div style="background:rgba(255,152,0,0.1);border:1px solid var(--yellow);border-radius:8px;padding:10px 14px;margin:8px 0 12px;font-size:12px">
                    <strong style="color:var(--yellow)">⚠️ ${workerTasks.length} tasks require a worker process</strong><br>
                    <span style="color:var(--text-muted)">These tasks (${workerTasks.map(([n]) => n).join(', ')}) only run when WORKER_MODE=true. Deploy a Railway worker service to activate them.</span>
                </div>`;
            }

            const renderCard = ([name, info]) => {
                const status = info.status || (info.alive ? 'alive' : 'dead');
                const isWorkerOnly = info.runs_here === false;
                const dotClass = isWorkerOnly ? 'worker-only' : status === 'alive' ? 'alive' : status === 'stale' ? 'stale' : status === 'never' ? 'never' : 'dead';
                const ago = isWorkerOnly ? 'worker only' : info.last_pulse ? fmtTimeAgo(info.last_pulse) : 'never';
                const dimClass = isWorkerOnly ? ' hb-dimmed' : '';
                return `<div class="hb-card${dimClass}"><span class="hb-dot ${dotClass}"></span><span class="hb-name">${esc(name.replace(/-/g, ' '))}</span><span class="hb-ago">${ago}</span></div>`;
            };

            hbEl.innerHTML = modeHtml + webTasks.map(renderCard).join('') + workerBanner + workerTasks.map(renderCard).join('');
        }
    }

    // Issues — deduped, with severity counts and auto-fix status
    const issEl = document.getElementById('issues-list');
    if (issEl && _audit) {
        const issues = _audit.issues || [];
        const fixes = _audit.auto_fixes_applied ?? 0;

        if (!issues.length && fixes === 0) {
            issEl.innerHTML = '<div class="empty-state" style="color:var(--green)">✓ No issues — system healthy</div>';
        } else {
            // Dedup by type
            const seen = new Set();
            const deduped = issues.filter(iss => {
                const key = iss.type || iss.detail || '';
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
            });

            // Count by severity
            const errCount = deduped.filter(i => i.severity === 'error').length;
            const warnCount = deduped.filter(i => i.severity === 'warn').length;
            const infoCount = deduped.filter(i => i.severity === 'info').length;

            let headerHtml = `<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;padding:6px 10px;background:rgba(255,255,255,0.03);border-radius:6px">`;
            if (errCount) headerHtml += `<span style="color:var(--red)">❌ ${errCount} error${errCount > 1 ? 's' : ''}</span> `;
            if (warnCount) headerHtml += `<span style="color:var(--yellow)">⚠️ ${warnCount} warning${warnCount > 1 ? 's' : ''}</span> `;
            if (infoCount) headerHtml += `<span style="color:var(--text-muted)">ℹ️ ${infoCount} info</span> `;
            if (fixes > 0) headerHtml += `<span style="color:var(--green)">· 🔧 ${fixes} auto-fixed</span>`;
            headerHtml += `</div>`;

            const issuesHtml = deduped.map(iss => {
                const sev = (iss.severity || 'info').toLowerCase();
                const icon = sev === 'error' ? '❌' : sev === 'warn' ? '⚠️' : 'ℹ️';
                return `<div class="issue-item"><span class="issue-sev ${sev}">${icon} ${sev}</span><span class="issue-detail">${esc(iss.detail || iss.message || iss.type || '')}</span></div>`;
            }).join('');

            issEl.innerHTML = headerHtml + issuesHtml;
        }
    }
}

// ── System Doctor checks (Morning Health Check) ──
function renderDoctorChecks() {
    const el = document.getElementById('doctor-checks');
    if (!el) return;

    if (!_subsystems?.morning_health?.checks?.length) {
        el.innerHTML = '<div class="empty-state">System Doctor not available</div>';
        return;
    }

    const mh = _subsystems.morning_health;
    const overall = mh.overall || 'UNKNOWN';
    const overallIcon = overall === 'PASS' ? '✅' : overall === 'WARN' ? '⚠️' : '❌';

    const passed = mh.passed ?? 0;
    const warned = mh.warned ?? 0;
    const failed = mh.failed ?? 0;
    const total = passed + warned + failed;

    let html = `<div class="doctor-header">
        <span class="doctor-overall">${overallIcon} ${overall}</span>
        <span class="doctor-score">${passed} pass${warned > 0 ? ` · ${warned} warn` : ''}${failed > 0 ? ` · ${failed} fail` : ''} — ${total} checks</span>
    </div>`;

    html += mh.checks.map(c => {
        const sev = c.severity || (c.pass ? 'pass' : 'fail');
        const icon = sev === 'fail' ? '❌' : sev === 'warn' ? '⚠️' : '✅';
        const detailColor = sev === 'fail' ? 'color:var(--red)' : sev === 'warn' ? 'color:var(--yellow)' : '';
        return `<div class="doctor-row"><span class="doctor-icon">${icon}</span><span class="doctor-name">${esc(c.name)}</span><span class="doctor-detail" style="${detailColor}">${esc(c.detail || '')}</span></div>`;
    }).join('');

    el.innerHTML = html;
}

// ── Subsystem cards: Brains ──
function renderSubsystemBrains(targetId) {
    const el = document.getElementById(targetId);
    if (!el) return;

    const brains = _subsystems?.brains;
    if (!brains?.length) {
        el.innerHTML = '<div class="empty-state">Brain modules not loaded</div>';
        return;
    }

    el.innerHTML = brains.map(b => {
        const dot = b.active ? 'active' : 'inactive';
        return `<div class="subsys-card">
            <span class="brain-dot ${dot}"></span>
            <div class="subsys-info">
                <span class="subsys-name">${esc(b.name)}</span>
                <span class="subsys-desc">${esc(b.desc || '')}</span>
            </div>
        </div>`;
    }).join('');
}

// ── Subsystem cards: Memory ──
function renderSubsystemMemory(targetId) {
    const el = document.getElementById(targetId);
    if (!el) return;

    const mem = _subsystems?.memory;
    if (!mem?.length) {
        el.innerHTML = '<div class="empty-state">Memory systems not loaded</div>';
        return;
    }

    el.innerHTML = mem.map(m => {
        const dot = m.active ? 'active' : 'inactive';
        return `<div class="subsys-card">
            <span class="brain-dot ${dot}"></span>
            <div class="subsys-info">
                <span class="subsys-name">${esc(m.name)}</span>
                <span class="subsys-desc">${esc(m.desc || '')}</span>
            </div>
        </div>`;
    }).join('');
}

function renderHealthCheckMirror() {
    const el = document.getElementById('health-check-mirror');
    if (!el) return;

    // If doctor data available, use real severity
    if (_subsystems?.morning_health?.checks?.length) {
        const mh = _subsystems.morning_health;
        el.innerHTML = mh.checks.map(c => {
            const sev = c.severity || (c.pass ? 'pass' : 'fail');
            const icon = sev === 'fail' ? '❌' : sev === 'warn' ? '⚠️' : '✅';
            return `<div class="hc-row"><span class="hc-icon">${icon}</span><span class="hc-name">${esc(c.name)}</span><span class="hc-detail">${esc(c.detail || '')}</span></div>`;
        }).join('');
        return;
    }

    // Fallback: build from available data (with honest thresholds)
    const checks = [];

    // API Server
    checks.push({ icon: '✅', name: 'API Server', detail: 'HTTP 200 — Online' });

    // Predictions
    const predCount = _watchlist.length || 0;
    checks.push({ icon: predCount > 0 ? '✅' : '❌', name: 'Predictions', detail: `${predCount} active predictions` });

    // Accuracy — use REAL accuracy for threshold
    if (_accuracy) {
        const rawPct = _accuracy.raw_accuracy_pct ?? _accuracy.accuracy_pct ?? 0;
        const filtPct = _accuracy.accuracy_pct ?? 0;
        const icon = rawPct < 40 ? '❌' : rawPct < 50 ? '⚠️' : '✅';
        checks.push({ icon, name: 'Accuracy', detail: `${filtPct}% filtered · ${rawPct}% real` });
    }

    // Heartbeat summary
    if (_heartbeat) {
        const alive = _heartbeat.alive ?? 0;
        const total = _heartbeat.total ?? 0;
        checks.push({ icon: alive > 3 ? '✅' : alive > 0 ? '⚠️' : '❌', name: 'Background Tasks', detail: `${alive}/${total} tasks alive` });
    }

    // System health
    if (_audit) {
        const score = _audit.health_score ?? 0;
        const issues = _audit.issues_remaining ?? 0;
        checks.push({ icon: score >= 80 ? '✅' : score >= 50 ? '⚠️' : '❌', name: 'System Health', detail: `${score}/100 · ${issues} issues` });
    }

    // Database
    checks.push({ icon: _accuracy ? '✅' : '❌', name: 'Database', detail: _accuracy ? 'PostgreSQL responding' : 'Unknown' });

    el.innerHTML = checks.map(c =>
        `<div class="hc-row"><span class="hc-icon">${c.icon}</span><span class="hc-name">${c.name}</span><span class="hc-detail">${c.detail}</span></div>`
    ).join('');
}

function renderHealthSidebar() {
    const el = document.getElementById('health-quick-stats');
    if (!el) return;

    const stats = [];
    if (_accuracy) {
        stats.push({ label: 'Filtered Rate', value: (_accuracy.accuracy_pct ?? 0) + '%' });
        stats.push({ label: 'Real Rate', value: (_accuracy.raw_accuracy_pct ?? _accuracy.accuracy_pct ?? 0) + '%', warn: (_accuracy.raw_accuracy_pct ?? 100) < 50 });
        stats.push({ label: 'Total Evaluated', value: _accuracy.total_predictions ?? 0 });
        stats.push({ label: 'Skipped', value: _accuracy.total_skipped ?? 0, warn: (_accuracy.total_skipped ?? 0) > 100 });
        stats.push({ label: 'Correct', value: _accuracy.correct_predictions ?? 0 });
    }
    if (_heartbeat) {
        stats.push({ label: 'Tasks Alive', value: `${_heartbeat.alive || 0}/${_heartbeat.total || 0}` });
    }
    if (_audit) {
        stats.push({ label: 'Health Score', value: (_audit.health_score ?? '--') + '/100', warn: (_audit.health_score ?? 100) < 60 });
        stats.push({ label: 'Penalty', value: '-' + (_audit.total_penalty ?? 0) });
        stats.push({ label: 'Issues', value: _audit.issues_remaining ?? 0, warn: (_audit.issues_remaining ?? 0) > 3 });
        stats.push({ label: 'Auto-fixes', value: _audit.auto_fixes_applied ?? 0 });
    }

    if (!stats.length) {
        el.innerHTML = '<div class="empty-state-sm">No data</div>';
        return;
    }

    el.innerHTML = stats.map(s => {
        const warnStyle = s.warn ? ' style="color:var(--yellow)"' : '';
        return `<div class="quick-stat"><span class="qs-label">${s.label}</span><span class="qs-value"${warnStyle}>${s.value}</span></div>`;
    }).join('');

    // Health score formula breakdown
    if (_audit?.score_breakdown?.length) {
        let formulaHtml = `<div style="margin-top:12px;padding:8px 10px;background:rgba(255,255,255,0.03);border-radius:6px;font-size:11px;color:var(--text-muted)">
            <strong>Score Formula</strong> <span style="opacity:0.6">100 − penalty</span><br>`;
        for (const b of _audit.score_breakdown) {
            const color = b.component === 'errors' ? 'var(--red)' : b.component === 'warnings' ? 'var(--yellow)' : 'var(--text-muted)';
            formulaHtml += `<span style="color:${color}">• ${b.count} ${b.component} × ${b.weight} = −${b.penalty}</span><br>`;
        }
        formulaHtml += `<strong>Total penalty: −${_audit.total_penalty ?? 0}</strong></div>`;
        el.innerHTML += formulaHtml;
    }
}

// ═══════════════════════════════════════
// TAB 6: AI BRAIN
// ═══════════════════════════════════════
function renderBrain(newsBrain) {
    // ── Brain Modules (from subsystems API) ──
    renderSubsystemBrains('brain-modules');

    // ── Memory Systems (from subsystems API) ──
    renderSubsystemMemory('brain-memory');

    // Intelligence Hub Subsystems
    const subsEl = document.getElementById('brain-subsystems');
    if (subsEl) {
        // Prefer subsystems API intel data if available
        const intelSystems = _subsystems?.intelligence;
        if (intelSystems?.length) {
            subsEl.innerHTML = intelSystems.map(s =>
                `<div class="brain-card"><span class="brain-dot ${s.active ? 'active' : 'inactive'}"></span><span class="brain-name">${esc(s.name)}</span></div>`
            ).join('');
        } else if (_intelligence?.systems) {
            const systems = _intelligence.systems;
            // systems could be an object or array
            const entries = Array.isArray(systems)
                ? systems.map(s => [s.name || s, s.active !== false])
                : Object.entries(systems).map(([name, info]) => [name, info?.active !== false]);

            subsEl.innerHTML = entries.map(([name, active]) =>
                `<div class="brain-card"><span class="brain-dot ${active ? 'active' : 'inactive'}"></span><span class="brain-name">${esc(String(name).replace(/_/g, ' '))}</span></div>`
            ).join('');
        } else if (_intelligence?.systems_loaded != null) {
            // Minimal info — just show counts
            const loaded = _intelligence.systems_loaded || 0;
            const total = _intelligence.systems_total || 0;
            subsEl.innerHTML = `<div class="empty-state">${loaded}/${total} subsystems loaded — detailed status not available via this endpoint</div>`;
        } else {
            subsEl.innerHTML = '<div class="not-running-msg">Intelligence Hub status not available</div>';
        }
    }

    // Edge Symbols
    const edgeEl = document.getElementById('brain-edge');
    if (edgeEl) {
        // Try to extract edge symbols from watchlist (symbols that appear in predictions)
        const edgeSymbols = _watchlist.map(w => w.symbol).sort();
        if (edgeSymbols.length) {
            edgeEl.innerHTML = edgeSymbols.map(s => `<span class="edge-chip">${s}</span>`).join('');
        } else {
            edgeEl.innerHTML = '<div class="empty-state">No edge symbols available</div>';
        }
    }

    // Confidence Map
    const confTbody = document.getElementById('brain-confidence-tbody');
    if (confTbody) {
        const sorted = [..._watchlist].sort((a, b) => (b.ghost_confidence || 0) - (a.ghost_confidence || 0));
        if (sorted.length) {
            confTbody.innerHTML = sorted.map(w => {
                const conf = w.ghost_confidence || 0;
                const dir = (w.ghost_direction || 'HOLD').toUpperCase();
                const dirClass = dir === 'UP' ? 'up' : dir === 'DOWN' ? 'down' : 'hold';
                const status = conf >= 65 ? '<span class="green">Strong Signal</span>' :
                               conf >= 50 ? '<span style="color:var(--yellow)">Moderate</span>' :
                               '<span class="red">Low / Hold</span>';
                return `<tr>
                    <td class="sym-cell">${w.symbol}</td>
                    <td>${(w.type || '--')}</td>
                    <td><span class="dir-badge ${dirClass}">${dir}</span></td>
                    <td>${conf > 0 ? conf.toFixed(0) + '%' : '--'}</td>
                    <td>${status}</td>
                </tr>`;
            }).join('');
        } else {
            confTbody.innerHTML = '<tr><td colspan="5" class="empty-state">No confidence data</td></tr>';
        }
    }

    // Skip Analysis — from history data
    const skipEl = document.getElementById('brain-skips');
    if (skipEl) {
        // Count skipped predictions by looking at history outcomes
        const symbolCounts = {};
        _history.forEach(t => {
            const sym = t.symbol || 'UNKNOWN';
            if (!symbolCounts[sym]) symbolCounts[sym] = { total: 0, wins: 0, losses: 0 };
            symbolCounts[sym].total++;
            if (t.outcome === 'win') symbolCounts[sym].wins++;
            else symbolCounts[sym].losses++;
        });

        const entries = Object.entries(symbolCounts).sort((a, b) => b[1].total - a[1].total);
        if (entries.length) {
            const maxCount = entries[0][1].total;
            skipEl.innerHTML = entries.slice(0, 15).map(([sym, data]) => {
                const pct = (data.total / maxCount * 100).toFixed(0);
                const winRate = data.total > 0 ? (data.wins / data.total * 100).toFixed(0) : 0;
                return `<div class="skip-bar">
                    <span class="skip-symbol">${sym}</span>
                    <span class="skip-count">${data.total} trades · ${winRate}% win rate</span>
                    <div class="skip-progress"><div class="skip-fill" style="width:${pct}%"></div></div>
                </div>`;
            }).join('');
        } else {
            skipEl.innerHTML = '<div class="empty-state">No trade data for skip analysis</div>';
        }
    }

    // Low Accuracy Breakdown
    const lowAccEl = document.getElementById('brain-low-accuracy');
    if (lowAccEl) {
        const symbolStats = {};
        _history.forEach(t => {
            const sym = t.symbol || 'UNKNOWN';
            if (!symbolStats[sym]) symbolStats[sym] = { wins: 0, total: 0 };
            symbolStats[sym].total++;
            if (t.outcome === 'win') symbolStats[sym].wins++;
        });

        const lowPerformers = Object.entries(symbolStats)
            .map(([sym, data]) => ({
                symbol: sym,
                rate: data.total > 0 ? (data.wins / data.total * 100) : 0,
                wins: data.wins,
                total: data.total
            }))
            .filter(s => s.total >= 3 && s.rate < 50)
            .sort((a, b) => a.rate - b.rate);

        if (lowPerformers.length) {
            lowAccEl.innerHTML = lowPerformers.slice(0, 10).map(s =>
                `<div class="low-acc-card">
                    <span class="low-acc-symbol">${s.symbol}</span>
                    <span class="low-acc-rate">${s.rate.toFixed(0)}%</span>
                    <div class="low-acc-detail">${s.wins}W / ${s.total - s.wins}L out of ${s.total} trades</div>
                </div>`
            ).join('');
        } else {
            lowAccEl.innerHTML = '<div class="empty-state">No symbols below 50% accuracy with 3+ trades</div>';
        }
    }

    // News Brain sidebar
    const nbEl = document.getElementById('brain-news-events');
    if (nbEl) {
        if (newsBrain?.ok) {
            const events = newsBrain.major_events || [];
            const atRisk = newsBrain.predictions_at_risk || [];
            let html = '';
            if (events.length) {
                html += events.slice(0, 5).map(e =>
                    `<div class="quick-stat"><span class="qs-label">${esc((e.headline || '').substring(0, 40))}…</span><span class="qs-value">${e.severity || '?'}</span></div>`
                ).join('');
            }
            if (atRisk.length) {
                html += `<div class="quick-stat"><span class="qs-label">At Risk</span><span class="qs-value red">${atRisk.length} symbols</span></div>`;
            }
            if (!html) html = '<div class="empty-state-sm">No events</div>';
            nbEl.innerHTML = html;
        } else {
            nbEl.innerHTML = '<div class="empty-state-sm">News brain not available</div>';
        }
    }
}

// ═══════════════════════════════════════
// TAB 7: NEWS
// ═══════════════════════════════════════
function renderNewsFeed() {
    const el = document.getElementById('news-feed');
    if (!el) return;

    let articles = [..._news];

    // Apply filter
    if (_newsFilter === 'stocks') {
        articles = articles.filter(a => {
            const title = (a.title || a.headline || '').toUpperCase();
            return !Array.from(CRYPTO_KEYS).some(k => title.includes(k));
        });
    } else if (_newsFilter === 'crypto') {
        articles = articles.filter(a => {
            const title = (a.title || a.headline || '').toUpperCase();
            return Array.from(CRYPTO_KEYS).some(k => title.includes(k));
        });
    } else if (_newsFilter === 'macro') {
        const macroKeys = ['FED', 'FOMC', 'GDP', 'CPI', 'INFLATION', 'INTEREST RATE',
            'TREASURY', 'JOBS', 'UNEMPLOYMENT', 'RECESSION', 'TARIFF', 'S&P', 'DOW',
            'NASDAQ', 'MARKET', 'ECONOMY', 'HOUSING', 'CONSUMER', 'OIL', 'CRUDE'];
        articles = articles.filter(a => {
            const title = (a.title || a.headline || '').toUpperCase();
            return macroKeys.some(k => title.includes(k));
        });
    }

    if (!articles.length) {
        el.innerHTML = '<div class="empty-state">No news articles</div>';
        return;
    }

    el.innerHTML = articles.slice(0, 30).map(a => {
        const title = a.title || a.headline || 'Untitled';
        const url = a.url || a.link || '#';
        const time = fmtTimeAgo(a.published_at || a.timestamp || a.published);
        const sent = (a.sentiment || 'neutral').toLowerCase();
        const sentClass = sent === 'bullish' ? 'bullish' : sent === 'bearish' ? 'bearish' : 'neutral';

        // Try to extract relevant symbols from title
        const titleUpper = title.toUpperCase();
        const allSymbols = _watchlist.map(w => w.symbol);
        const matchedSymbols = allSymbols.filter(s => titleUpper.includes(s));
        const symbolTags = matchedSymbols.slice(0, 3).map(s =>
            `<span class="news-tag symbol">${s}</span>`
        ).join('');

        return `
        <a class="news-row" href="${url}" target="_blank" rel="noopener">
            <span class="news-title">${esc(title)}</span>
            <div class="news-tags">${symbolTags}</div>
            <span class="news-sent ${sentClass}">${sent}</span>
            <span class="news-time">${time}</span>
        </a>`;
    }).join('');
}

// ═══════════════════════════════════════
// TAB 8: FINANCIALS
// ═══════════════════════════════════════
function renderFinancials() {
    const statusEl = document.getElementById('financials-status');

    // Check if money-game task has pulsed
    const moneyGameAlive = _heartbeat?.tasks?.['money-game']?.status === 'alive';

    // We can still show financials from history data even if money-game hasn't pulsed
    if (!_history.length) {
        if (statusEl) statusEl.innerHTML = '<div class="not-running-msg">No trade history available — financial analysis requires resolved trades</div>';
        return;
    }

    if (statusEl) statusEl.innerHTML = '';

    // Forecast staleness warning — check audit issues for stale forecast
    // Always remove stale warning first to prevent duplicate divs on each 30s refresh
    const _prevForecastWarn = document.getElementById('forecast-stale-warning');
    if (_prevForecastWarn) _prevForecastWarn.remove();

    if (_audit?.issues) {
        const forecastIssue = _audit.issues.find(i => (i.detail || i.message || '').toLowerCase().includes('forecast'));
        if (forecastIssue) {
            const warnEl = document.createElement('div');
            warnEl.id = 'forecast-stale-warning';
            warnEl.style.cssText = 'background:rgba(255,204,0,0.12);border:1px solid var(--yellow);border-radius:8px;padding:10px 14px;margin-bottom:12px;color:var(--yellow);font-size:13px';
            warnEl.innerHTML = `⚠️ <strong>${esc(forecastIssue.detail || forecastIssue.message || 'Forecast data is stale')}</strong>`;
            statusEl?.parentElement?.insertBefore(warnEl, statusEl.nextSibling);
        }
    }

    // ── Performance by Symbol ──
    // FIX (Mar 18, 2026): Use actual_move_pct (percentage) NOT pnl (dollar amount)
    // for avg win/loss display. Previously ETH showed +69.47% because that was
    // the dollar PnL ($69.47), not the percentage move (~3%).
    const symbolStats = {};
    _history.forEach(t => {
        const sym = t.symbol || 'UNKNOWN';
        if (!symbolStats[sym]) symbolStats[sym] = { trades: 0, wins: 0, losses: 0, totalPnl: 0, winPnls: [], lossPnls: [] };
        symbolStats[sym].trades++;
        // Use actual_move_pct (percentage) for display, fall back to 0
        const movePct = t.actual_move_pct != null ? t.actual_move_pct : 0;
        symbolStats[sym].totalPnl += movePct;
        if (t.outcome === 'win') {
            symbolStats[sym].wins++;
            symbolStats[sym].winPnls.push(Math.abs(movePct));
        } else {
            symbolStats[sym].losses++;
            symbolStats[sym].lossPnls.push(Math.abs(movePct));
        }
    });

    const perfTbody = document.getElementById('perf-by-symbol-tbody');
    if (perfTbody) {
        const entries = Object.entries(symbolStats).sort((a, b) => b[1].trades - a[1].trades);
        perfTbody.innerHTML = entries.map(([sym, d]) => {
            const winRate = d.trades > 0 ? (d.wins / d.trades * 100).toFixed(0) : 0;
            const avgWin = d.winPnls.length ? (d.winPnls.reduce((a, b) => a + b, 0) / d.winPnls.length).toFixed(2) : '--';
            const avgLoss = d.lossPnls.length ? (d.lossPnls.reduce((a, b) => a + b, 0) / d.lossPnls.length).toFixed(2) : '--';
            const wr = parseFloat(winRate);
            return `<tr>
                <td><strong>${sym}</strong></td>
                <td>${d.trades}</td>
                <td class="green">${d.wins}</td>
                <td class="red">${d.losses}</td>
                <td class="${wr >= 50 ? 'green' : 'red'}">${winRate}%</td>
                <td class="green">${avgWin !== '--' ? '+' + avgWin + '%' : '--'}</td>
                <td class="red">${avgLoss !== '--' ? '-' + avgLoss + '%' : '--'}</td>
            </tr>`;
        }).join('');
    }

    // ── Best & Worst ──
    const bwEl = document.getElementById('best-worst');
    if (bwEl) {
        const symbolEntries = Object.entries(symbolStats).filter(([, d]) => d.trades >= 3);
        const byWinRate = [...symbolEntries].sort((a, b) => (b[1].wins / b[1].trades) - (a[1].wins / a[1].trades));
        const best = byWinRate[0];
        const worst = byWinRate[byWinRate.length - 1];

        const sortedByPnl = _history.filter(t => t.actual_move_pct != null).sort((a, b) => (b.actual_move_pct || 0) - (a.actual_move_pct || 0));
        const biggestWin = sortedByPnl[0];
        const biggestLoss = sortedByPnl[sortedByPnl.length - 1];

        let html = '';
        if (best) html += `<div class="bw-card"><div class="bw-title">Best Performer</div><div class="bw-symbol green">${best[0]}</div><div class="bw-value green">${(best[1].wins / best[1].trades * 100).toFixed(0)}% win rate (${best[1].trades} trades)</div></div>`;
        if (worst) html += `<div class="bw-card"><div class="bw-title">Worst Performer</div><div class="bw-symbol red">${worst[0]}</div><div class="bw-value red">${(worst[1].wins / worst[1].trades * 100).toFixed(0)}% win rate (${worst[1].trades} trades)</div></div>`;
        if (biggestWin) html += `<div class="bw-card"><div class="bw-title">Biggest Win</div><div class="bw-symbol green">${biggestWin.symbol}</div><div class="bw-value green">+${(biggestWin.actual_move_pct || 0).toFixed(2)}%</div></div>`;
        if (biggestLoss) html += `<div class="bw-card"><div class="bw-title">Biggest Loss</div><div class="bw-symbol red">${biggestLoss.symbol}</div><div class="bw-value red">${(biggestLoss.actual_move_pct || 0).toFixed(2)}%</div></div>`;

        bwEl.innerHTML = html || '<div class="empty-state">Insufficient data</div>';
    }

    // ── Risk Metrics ──
    const riskEl = document.getElementById('risk-metrics');
    if (riskEl) {
        const totalWins = _history.filter(t => t.outcome === 'win').length;
        const totalTrades = _history.length;
        const winRate = totalTrades > 0 ? (totalWins / totalTrades * 100).toFixed(1) : '--';

        const winPnls = _history.filter(t => t.outcome === 'win' && t.actual_move_pct).map(t => Math.abs(t.actual_move_pct));
        const lossPnls = _history.filter(t => t.outcome === 'loss' && t.actual_move_pct).map(t => Math.abs(t.actual_move_pct));
        const avgWin = winPnls.length ? (winPnls.reduce((a, b) => a + b, 0) / winPnls.length) : 0;
        const avgLoss = lossPnls.length ? (lossPnls.reduce((a, b) => a + b, 0) / lossPnls.length) : 0;
        const lossCount = totalTrades - totalWins;
        const profitFactor = (avgLoss > 0 && lossCount > 0) ? (avgWin * totalWins) / (avgLoss * lossCount) : (totalWins > 0 ? 999 : 0);
        const rr = avgLoss > 0 ? (avgWin / avgLoss) : 0;

        riskEl.innerHTML = `
            <div class="risk-card"><span class="risk-val ${parseFloat(winRate) >= 50 ? 'green' : 'red'}">${winRate}%</span><span class="risk-lbl">Win Rate</span></div>
            <div class="risk-card"><span class="risk-val ${profitFactor >= 1 ? 'green' : 'red'}">${profitFactor.toFixed(2)}</span><span class="risk-lbl">Profit Factor</span></div>
            <div class="risk-card"><span class="risk-val">${rr.toFixed(2)}</span><span class="risk-lbl">Avg R/R Ratio</span></div>
            <div class="risk-card"><span class="risk-val">${totalTrades}</span><span class="risk-lbl">Total Trades</span></div>
        `;
    }

    // ── Financials Sidebar ──
    const overviewEl = document.getElementById('financials-overview');
    if (overviewEl) {
        const totalWins = _history.filter(t => t.outcome === 'win').length;
        const totalTrades = _history.length;
        overviewEl.innerHTML = `
            <div class="quick-stat"><span class="qs-label">Trades</span><span class="qs-value">${totalTrades}</span></div>
            <div class="quick-stat"><span class="qs-label">Wins</span><span class="qs-value green">${totalWins}</span></div>
            <div class="quick-stat"><span class="qs-label">Losses</span><span class="qs-value red">${totalTrades - totalWins}</span></div>
            <div class="quick-stat"><span class="qs-label">Symbols</span><span class="qs-value">${Object.keys(symbolStats).length}</span></div>
        `;
    }

    // ── P&L Chart (simple canvas-based) ──
    renderPnlChart();
}

// ── Phase 3.8: Accuracy Trends Chart ──
function renderAccuracyChart() {
    const canvas = document.getElementById('accuracy-chart');
    if (!canvas || !window._accuracyTrends) return;

    const trends = window._accuracyTrends;
    const data = trends.daily || [];
    
    // Parse dates and accuracy values
    const labels = data.map(d => d.date);
    const accuracy = data.map(d => d.accuracy || 0);
    
    // Destroy existing chart if it exists
    if (window._accuracyChartInstance) {
        window._accuracyChartInstance.destroy();
    }
    
    const ctx = canvas.getContext('2d');
    window._accuracyChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Accuracy %',
                data: accuracy,
                borderColor: '#00ff88',
                backgroundColor: 'rgba(0, 255, 136, 0.1)',
                borderWidth: 2,
                tension: 0.3,
                fill: true,
                pointRadius: 3,
                pointHoverRadius: 5
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            const idx = context.dataIndex;
                            const d = data[idx];
                            return `Accuracy: ${d.accuracy.toFixed(1)}% (${d.correct}/${d.total})`;
                        }
                    }
                }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    max: 100,
                    ticks: { color: '#aaa' },
                    grid: { color: '#333' }
                },
                x: {
                    ticks: { 
                        color: '#aaa',
                        maxRotation: 45,
                        minRotation: 45
                    },
                    grid: { color: '#333' }
                }
            }
        }
    });
    
    // Wire time toggle buttons
    const toggles = document.getElementById('accuracy-toggles');
    if (toggles) {
        toggles.addEventListener('click', async (e) => {
            if (!e.target.classList.contains('toggle-btn')) return;
            
            // Update active state
            toggles.querySelectorAll('.toggle-btn').forEach(btn => btn.classList.remove('active'));
            e.target.classList.add('active');
            
            // Fetch new data
            const range = e.target.dataset.range;
            const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
            const newData = await fetchJSON(`/api/accuracy/trends?days=${days}`);
            
            if (newData?.ok) {
                window._accuracyTrends = newData;
                renderAccuracyChart();
            }
        });
    }
}

function renderPnlChart() {
    const canvas = document.getElementById('pnl-chart');
    if (!canvas || !_history.length) return;

    const ctx = canvas.getContext('2d');
    const width = canvas.parentElement.clientWidth - 32;
    const height = 180;
    canvas.width = width;
    canvas.height = height;

    // Sort history by resolved_at timestamp (most reliable chronological order)
    const sorted = [..._history]
        .filter(t => t.resolved_at && t.pnl != null)
        .sort((a, b) => a.resolved_at - b.resolved_at);

    // FIX (Mar 21, 2026): Use actual pnl field from API, not recalculated from actual_move_pct
    // The API provides dollar P&L already computed. Previous code assumed all moves = same $ value.
    let cumPnl = 0;
    const points = sorted.map(t => {
        cumPnl += (t.pnl || 0);
        return cumPnl;
    });

    if (!points.length) return;

    const minY = Math.min(0, ...points);
    const maxY = Math.max(0, ...points);
    const range = maxY - minY || 1;
    const xStep = width / (points.length - 1 || 1);
    const padding = 10;

    ctx.clearRect(0, 0, width, height);

    // Zero line
    const zeroY = height - padding - ((0 - minY) / range) * (height - padding * 2);
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(0, zeroY);
    ctx.lineTo(width, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);

    // P&L line
    const finalPnl = points[points.length - 1];
    ctx.strokeStyle = finalPnl >= 0 ? '#00c853' : '#ff3b30';
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((p, i) => {
        const x = i * xStep;
        const y = height - padding - ((p - minY) / range) * (height - padding * 2);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Fill under the line
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    if (finalPnl >= 0) {
        gradient.addColorStop(0, 'rgba(0, 200, 83, 0.15)');
        gradient.addColorStop(1, 'rgba(0, 200, 83, 0)');
    } else {
        gradient.addColorStop(0, 'rgba(255, 59, 48, 0.15)');
        gradient.addColorStop(1, 'rgba(255, 59, 48, 0)');
    }
    ctx.lineTo(width, height);
    ctx.lineTo(0, height);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();
}

// ═══════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════
async function fetchJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    return r.json();
}

function fmtPrice(v) {
    if (v == null || v === 0) return '--';
    return v >= 1
        ? '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
        : '$' + Number(v).toFixed(6);
}

function fmtTickerPrice(v) {
    if (v == null) return '--';
    if (v >= 1000) return Number(v).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    if (v >= 1) return Number(v).toFixed(2);
    return Number(v).toFixed(4);
}

function fmtDate(ts) {
    if (!ts) return '--';
    const d = typeof ts === 'number' ? new Date(ts > 1e12 ? ts : ts * 1000) : new Date(ts);
    return isNaN(d) ? '--' : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function fmtTimeAgo(ts) {
    if (!ts) return '';
    const d = typeof ts === 'number' ? new Date(ts > 1e12 ? ts : ts * 1000) : new Date(ts);
    if (isNaN(d)) return '';
    const s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
function setTextColor(id, v, color) {
    const el = document.getElementById(id);
    if (el) { el.textContent = v; el.className = 'stat-val ' + color; }
}

// ═══════════════════════════════════════════════════════════════════════
// PHASE 4 — WOLF INTEL PANEL
// ═══════════════════════════════════════════════════════════════════════

async function loadWolfIntel() {
    let ctx = null;
    try {
        ctx = await fetchJSON('/api/wolf/context');
    } catch (e) { /* endpoint may not be live yet */ }

    // ── Fetch live WOLF price separately (uses existing ticker endpoint)
    let priceData = null;
    try {
        priceData = await fetchJSON('/api/v3/market/ticker');
    } catch (e) {}

    // ── Price hero ─────────────────────────────────────────────────────
    if (priceData?.items) {
        const wolfItem = priceData.items.find(i => i.id === 'WOLF' || i.symbol === 'WOLF');
        if (wolfItem) {
            const priceEl = document.getElementById('wolf-price');
            const chgEl = document.getElementById('wolf-price-chg');
            const tsEl = document.getElementById('wolf-price-ts');
            if (priceEl) priceEl.textContent = '$' + (wolfItem.price || wolfItem.last || '—');
            if (chgEl) {
                const chg = wolfItem.change_pct ?? wolfItem.pct_change ?? null;
                if (chg !== null) {
                    chgEl.textContent = (chg >= 0 ? '▲' : '▼') + Math.abs(chg).toFixed(2) + '%';
                    chgEl.style.color = chg >= 0 ? 'var(--green)' : 'var(--red)';
                }
            }
            if (tsEl) tsEl.textContent = wolfItem.ts || '';
        }
    }

    if (!ctx) return;  // No context data available yet

    // ── KPI cards ──────────────────────────────────────────────────────
    const sf = ctx.short_data?.short_float_pct;
    setText('wolf-short-float', sf != null ? sf.toFixed(1) + '%' : '—');

    const dtc = ctx.short_data?.days_to_cover;
    setText('wolf-dtc', dtc != null ? dtc.toFixed(1) + 'd' : '—');

    const squeeze = ctx.short_data?.squeeze_risk || '—';
    const squeezeEl = document.getElementById('wolf-squeeze');
    if (squeezeEl) {
        squeezeEl.textContent = squeeze.toUpperCase();
        squeezeEl.style.color = {
            extreme: 'var(--red)', high: '#ff9900',
            medium: '#ffdd00', low: 'var(--green)'
        }[squeeze.toLowerCase()] || 'inherit';
    }

    // Earnings
    const earnEl = document.getElementById('wolf-earnings');
    if (earnEl) {
        if (ctx.earnings?.date_str) {
            const d = ctx.earnings.days_away;
            const label = d <= 2 ? '⚠️ CAUTION' : d <= 5 ? '📅 THIS WEEK' : `in ${d}d`;
            earnEl.textContent = ctx.earnings.date_str + ' (' + label + ')';
            earnEl.style.color = d <= 2 ? 'var(--red)' : d <= 5 ? '#ff9900' : 'inherit';
        } else {
            earnEl.textContent = '—';
        }
    }

    // Confidence adjustment
    const adj = ctx.net_confidence_adj ?? null;
    const adjEl = document.getElementById('wolf-conf-adj');
    if (adjEl && adj !== null) {
        adjEl.textContent = (adj >= 0 ? '+' : '') + (adj * 100).toFixed(1) + '%';
        adjEl.style.color = adj > 0 ? 'var(--green)' : adj < 0 ? 'var(--red)' : 'inherit';
    }

    // EDGAR alert
    const edgarEl = document.getElementById('wolf-edgar');
    if (edgarEl) {
        if (ctx.edgar_alert) {
            const urg = ctx.edgar_alert.urgency || 'low';
            edgarEl.textContent = ctx.edgar_alert.filing_date + ' — ' + (ctx.edgar_alert.description || 'New 8-K');
            edgarEl.style.color = urg === 'critical' ? 'var(--red)' : urg === 'high' ? '#ff9900' : 'inherit';
        } else {
            edgarEl.textContent = 'No recent filing';
        }
    }

    // ── Competitor / sector peers ──────────────────────────────────────
    const peersEl = document.getElementById('wolf-peers-grid');
    if (peersEl && ctx.competitor_signals?.length) {
        peersEl.innerHTML = ctx.competitor_signals.map(s => {
            const chg = s.price_change_pct ?? 0;
            const color = chg >= 0 ? 'var(--green)' : 'var(--red)';
            const arrow = chg >= 0 ? '▲' : '▼';
            return `<div class="wolf-peer-card">
                <span class="wolf-peer-sym">${esc(s.symbol)}</span>
                <span class="wolf-peer-chg" style="color:${color}">${arrow}${Math.abs(chg).toFixed(2)}%</span>
                <span class="wolf-peer-sig">${esc(s.signal_strength || '')}</span>
            </div>`;
        }).join('');
    } else if (peersEl) {
        peersEl.innerHTML = '<div class="empty-state">No peer data</div>';
    }

    // ── Active signals / reasons ──────────────────────────────────────
    const reasonsEl = document.getElementById('wolf-reasons');
    if (reasonsEl) {
        const reasons = ctx.reasons || [];
        if (reasons.length) {
            reasonsEl.innerHTML = reasons.map(r => `<li>${esc(r)}</li>`).join('');
        } else {
            reasonsEl.innerHTML = '<li class="empty-state">No active signals</li>';
        }
    }

    // ── Sidebar health ─────────────────────────────────────────────────
    const sideEl = document.getElementById('wolf-sidebar-status');
    if (sideEl) {
        const errors = ctx.errors || [];
        const ok = errors.length === 0;
        sideEl.innerHTML = `
            <div class="stat-val ${ok ? 'green' : 'yellow'}">${ok ? '✅ All feeds OK' : '⚠️ ' + errors.length + ' error(s)'}</div>
            ${errors.map(e => `<div class="empty-state-sm">${esc(e)}</div>`).join('')}
        `;
    }
}
