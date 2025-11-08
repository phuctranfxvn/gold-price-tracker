// static/js/app.js (fixed limit handling)
(() => {
    document.addEventListener('DOMContentLoaded', () => {
        const btnToday = document.getElementById('modeTodayBtn');
        const btn7 = document.getElementById('mode7Btn');
        const btn30 = document.getElementById('mode30Btn');
        const refreshBtn = document.getElementById('refreshBtn');
        const fetch30Btn = document.getElementById('fetch30Btn') || document.getElementById('fetch7Btn'); // fallback
        const limitSelect = document.getElementById('limitSelect');
        const lastUpdatedEl = document.getElementById('last-updated');
        const currentBuyEl = document.getElementById('currentBuy');
        const currentSellEl = document.getElementById('currentSell');

        if (!btnToday || !btn7 || !btn30 || !refreshBtn || !limitSelect) {
            console.warn('One or more UI elements not found — check IDs in HTML');
            return;
        }

        let chart = null;
        let creatingChart = false;
        let currentMode = '7d';
        const CHART_CANVAS = document.getElementById('priceChart');

        function setActiveMode(mode) {
            currentMode = mode;
            [btnToday, btn7, btn30].forEach(b => b.classList.remove('active'));
            if (mode === 'today') btnToday.classList.add('active');
            else if (mode === '30d') btn30.classList.add('active');
            else btn7.classList.add('active');

            // UX: when switching mode, set the limitSelect to the sensible default for that mode
            // but **do not** override if user already changed it earlier (we check a data attribute)
            if (!limitSelect.dataset.userModified) {
                if (mode === '7d') limitSelect.value = '7';
                else if (mode === '30d') limitSelect.value = '30';
            }
            // disable select in 'today' mode
            limitSelect.disabled = (mode === 'today');
        }

        // mark when user intentionally changes select
        limitSelect.addEventListener('change', () => {
            limitSelect.dataset.userModified = '1';
        });

        function tsToLabel(ts) {
            const d = new Date(ts * 1000);
            if (currentMode === 'today') {
                return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            } else {
                return d.toLocaleDateString('vi-VN');
            }
        }

        async function fetchPrices(limit = 30) {
            const modeParam = currentMode;
            const url = `${window.API.PRICES}?mode=${modeParam}&limit=${limit}`;
            try {
                const resp = await fetch(url);
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const j = await resp.json();
                const arr = Array.isArray(j.data) ? j.data.slice() : [];
                arr.forEach(it => {
                    it.timestamp = Number(it.timestamp) || 0;
                    it.buy = (it.buy === null || it.buy === undefined) ? null : Number(it.buy);
                    it.sell = (it.sell === null || it.sell === undefined) ? null : Number(it.sell);
                });
                arr.sort((a, b) => a.timestamp - b.timestamp);
                return arr;
            } catch (err) {
                console.error('fetchPrices error', err);
                return [];
            }
        }

        function formatNumber(n) {
            if (n === null || n === undefined) return '—';
            return Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
        }

        function showEmptyMessage(show) {
            if (!CHART_CANVAS) return;
            const parent = CHART_CANVAS.parentElement;
            if (!parent) return;
            let el = parent.querySelector('.chart-empty');
            if (show) {
                if (!el) {
                    el = document.createElement('div');
                    el.className = 'chart-empty';
                    el.textContent = 'Không có dữ liệu để hiển thị. Hãy nhấn "Lấy lịch sử" để nạp dữ liệu hoặc chờ scheduler.';
                    parent.appendChild(el);
                }
                CHART_CANVAS.style.display = 'none';
            } else {
                if (el) el.remove();
                CHART_CANVAS.style.display = '';
            }
        }

        function destroyChartIfExists() {
            try {
                if (chart) {
                    chart.destroy();
                    chart = null;
                } else if (Chart && Chart.getChart) {
                    const existing = Chart.getChart(CHART_CANVAS);
                    if (existing) existing.destroy();
                }
            } catch (e) { console.warn('destroy chart error', e); chart = null; }
        }

        function createChart(labels, buys, sells) {
            if (!CHART_CANVAS) return;
            const ctx = CHART_CANVAS.getContext('2d');
            if (!ctx) return;

            destroyChartIfExists();
            creatingChart = true;
            chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Mua Vào',
                        data: buys,
                        fill: false,
                        tension: 0.25,
                        pointRadius: 4,
                        borderWidth: 3,
                        borderColor: '#60a5fa',
                        pointBackgroundColor: '#60a5fa',
                        backgroundColor: 'rgba(96,165,250,0.08)'
                    }, {
                        label: 'Bán Ra',
                        data: sells,
                        fill: false,
                        tension: 0.25,
                        pointRadius: 4,
                        borderWidth: 3,
                        borderColor: '#f59e0b',
                        pointBackgroundColor: '#f59e0b',
                        backgroundColor: 'rgba(245,158,11,0.08)'
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {
                        legend: { display: true, labels: { color: '#E6EEF8' } },
                        tooltip: {
                            interaction: { mode: 'nearest', intersect: false },
                            callbacks: {
                                label: function (ctx) {
                                    const v = ctx.raw;
                                    if (v === null || v === undefined) return ctx.dataset.label + ': —';
                                    return ctx.dataset.label + ': ' + Number(v).toLocaleString() + ' VND';
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            display: true,
                            ticks: { color: '#CFE6FF', maxRotation: 45, autoSkip: true, maxTicksLimit: 12 },
                            grid: { color: 'rgba(255,255,255,0.03)' }
                        },
                        y: {
                            display: true,
                            ticks: {
                                color: '#CFE6FF',
                                callback: function (value) { return Number(value).toLocaleString(); }
                            },
                            grid: { color: 'rgba(255,255,255,0.03)' }
                        }
                    },
                    elements: { line: { borderJoinStyle: 'round' } }
                }
            });
            setTimeout(() => { creatingChart = false; }, 120);
        }

        let pending = false;
        async function updateChart() {
            if (pending) return;
            pending = true;
            try {
                // Determine limit depending on mode with sensible defaults
                const userLimit = parseInt(limitSelect.value, 10);
                let limit;
                if (currentMode === 'today') {
                    limit = 0; // backend ignores limit for today
                } else if (!isNaN(userLimit) && userLimit > 0) {
                    // user explicitly set a limit -> respect it for both 7d and 30d
                    limit = userLimit;
                } else {
                    // user did not set an explicit limit -> use mode defaults
                    limit = (currentMode === '30d') ? 30 : 7;
                }

                // ask backend
                const data = await fetchPrices(limit);
                if (!data || data.length === 0) {
                    destroyChartIfExists();
                    showEmptyMessage(true);
                    if (currentBuyEl) currentBuyEl.textContent = '—';
                    if (currentSellEl) currentSellEl.textContent = '—';
                    if (lastUpdatedEl) lastUpdatedEl.textContent = '—';
                    pending = false;
                    return;
                }
                showEmptyMessage(false);

                const labels = data.map(p => tsToLabel(p.timestamp));
                const buys = data.map(p => (p.buy === null ? null : Number(p.buy)));
                const sells = data.map(p => (p.sell === null ? null : Number(p.sell)));
                const last = data[data.length - 1];

                if (currentBuyEl) currentBuyEl.textContent = last && last.buy ? formatNumber(last.buy) : '—';
                if (currentSellEl) currentSellEl.textContent = last && last.sell ? formatNumber(last.sell) : '—';
                if (lastUpdatedEl) lastUpdatedEl.textContent = last ? 'Cập nhật: ' + new Date(last.timestamp * 1000).toLocaleString() : '—';

                if (!chart) createChart(labels, buys, sells);
                else {
                    try {
                        chart.data.labels = labels;
                        chart.data.datasets[0].data = buys;
                        chart.data.datasets[1].data = sells;
                        chart.update();
                    } catch (e) {
                        console.warn('chart update failed, recreating', e);
                        destroyChartIfExists();
                        createChart(labels, buys, sells);
                    }
                }
            } catch (err) {
                console.error('updateChart error', err);
            } finally {
                pending = false;
            }
        }

        btnToday.addEventListener('click', () => { setActiveMode('today'); updateChart(); });
        btn7.addEventListener('click', () => { setActiveMode('7d'); updateChart(); });
        btn30.addEventListener('click', () => { setActiveMode('30d'); updateChart(); });
        refreshBtn.addEventListener('click', () => updateChart());
        limitSelect.addEventListener('change', () => {
            limitSelect.dataset.userModified = '1';
            updateChart();
        });
        if (fetch30Btn) {
            fetch30Btn.addEventListener('click', async () => {
                fetch30Btn.textContent = 'Đang lấy...';
                fetch30Btn.disabled = true;
                try {
                    const resp = await fetch(`${window.API.FETCH_HISTORY}?days=30`, { method: 'POST' });
                    const j = await resp.json();
                    alert('Hoàn tất: ' + (j.inserted || 0) + ' bản ghi được thêm');
                    await updateChart();
                } catch (err) {
                    alert('Lỗi khi lấy lịch sử: ' + err);
                }
                fetch30Btn.textContent = fetch30Btn.id === 'fetch7Btn' ? 'Lấy 7 ngày' : 'Lấy 30 ngày';
                fetch30Btn.disabled = false;
            });
        }

        // initial
        setActiveMode('7d');
        updateChart();
        setInterval(updateChart, 60_000);
    });
})();
