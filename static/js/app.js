// static/js/app.js
(() => {
    document.addEventListener('DOMContentLoaded', () => {
        const btnToday = document.getElementById('modeTodayBtn');
        const btn7 = document.getElementById('mode7Btn');
        const btn30 = document.getElementById('mode30Btn');
        const refreshBtn = document.getElementById('refreshBtn');
        const limitSelect = document.getElementById('limitSelect');
        const lastUpdatedEl = document.getElementById('last-updated');
        const currentBuyEl = document.getElementById('currentBuy');
        const currentSellEl = document.getElementById('currentSell');
        const currentBuyDiffEl = document.getElementById('currentBuyDiff');
        const currentSellDiffEl = document.getElementById('currentSellDiff');
        const currentWorldGoldEl = document.getElementById('currentWorldGold');
        const currentWorldGoldDiffEl = document.getElementById('currentWorldGoldDiff');
        const worldGoldDetailsEl = document.getElementById('worldGoldDetails');
        const currentTypeBadge = document.getElementById('currentTypeBadge');
        const typeBtns = document.querySelectorAll('.type-btn');

        if (!btnToday || !btn7 || !btn30 || !refreshBtn || !limitSelect) {
            console.warn('One or more UI elements not found — check IDs in HTML');
            return;
        }

        let chart = null;
        let creatingChart = false;
        let currentMode = '7d';
        let currentType = 'SJC';
        const CHART_CANVAS = document.getElementById('priceChart');

        // ---- Type selector ----
        typeBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                typeBtns.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentType = btn.dataset.type;
                if (currentTypeBadge) currentTypeBadge.textContent = currentType;
                updateChart();
            });
        });

        function setActiveMode(mode) {
            currentMode = mode;
            [btnToday, btn7, btn30].forEach(b => b.classList.remove('active'));
            if (mode === 'today') btnToday.classList.add('active');
            else if (mode === '30d') btn30.classList.add('active');
            else btn7.classList.add('active');

            if (!limitSelect.dataset.userModified) {
                if (mode === '7d') limitSelect.value = '7';
                else if (mode === '30d') limitSelect.value = '30';
            }
            limitSelect.disabled = (mode === 'today');
        }

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

        async function fetchPrices(limit = 30, type = null) {
            const modeParam = currentMode;
            const goldType = type || currentType;
            const url = `${window.API.PRICES}?mode=${modeParam}&limit=${limit}&type=${goldType}`;
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

        function formatDiffStr(diff) {
            if (!diff || diff === 0) return '';
            const sign = diff > 0 ? '▲' : '▼';
            // Default: Up is green, Down is red
            const color = diff > 0 ? '#34d399' : '#f87171';
            return `<span style="color: ${color}; font-weight: 500; font-size: 0.9em; margin-left: 4px;">${sign} ${formatNumber(Math.abs(diff))}</span>`;
        }

        function getPrevDiffValue(arr, key, currentVal) {
            if (!arr || !currentVal) return null;
            for (let i = arr.length - 2; i >= 0; i--) {
                const p = arr[i];
                if (p && p[key] !== null && p[key] !== currentVal) return p[key];
            }
            return null;
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
                    el.textContent = 'Không có dữ liệu để hiển thị. Hãy nhấn "Tải lại" để nạp dữ liệu.';
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

        function createChart(labels, buys, sells, worldData) {
            if (!CHART_CANVAS) return;
            const ctx = CHART_CANVAS.getContext('2d');
            if (!ctx) return;

            destroyChartIfExists();
            creatingChart = true;

            const datasets = [{
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
            }];

            // World gold overlay — align to same labels using timestamps
            if (worldData && worldData.length > 0) {
                const worldMap = {};
                worldData.forEach(p => { worldMap[tsToLabel(p.timestamp)] = p.buy; });
                const worldLine = labels.map(l => worldMap[l] ?? null);
                datasets.push({
                    label: '🌍 Vàng Thế Giới (VNĐ/chỉ)',
                    data: worldLine,
                    fill: false,
                    tension: 0.25,
                    pointRadius: 3,
                    borderWidth: 2,
                    borderDash: [5, 3],
                    borderColor: '#34d399',
                    pointBackgroundColor: '#34d399',
                    backgroundColor: 'rgba(52,211,153,0.06)',
                    spanGaps: true
                });
            }

            chart = new Chart(ctx, {
                type: 'line',
                data: { labels, datasets },
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
                            ticks: {
                                color: '#CFE6FF',
                                source: 'data',
                                autoSkip: false,
                                maxRotation: 45,
                                minRotation: 45
                            },
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
                const userLimit = parseInt(limitSelect.value, 10);
                let limit;
                if (currentMode === 'today') {
                    limit = 0;
                } else if (!isNaN(userLimit) && userLimit > 0) {
                    limit = userLimit;
                } else {
                    limit = (currentMode === '30d') ? 30 : 7;
                }

                // Fetch gold type + world gold in parallel
                const [data, worldData] = await Promise.all([
                    fetchPrices(limit),
                    fetchPrices(limit, 'WORLD')
                ]);

                if (!data || data.length === 0) {
                    destroyChartIfExists();
                    showEmptyMessage(true);
                    if (currentBuyEl) currentBuyEl.textContent = '—';
                    if (currentSellEl) currentSellEl.textContent = '—';
                    if (currentBuyDiffEl) currentBuyDiffEl.innerHTML = '';
                    if (currentSellDiffEl) currentSellDiffEl.innerHTML = '';
                    if (currentWorldGoldEl) currentWorldGoldEl.textContent = '—';
                    if (currentWorldGoldDiffEl) currentWorldGoldDiffEl.innerHTML = '';
                    if (worldGoldDetailsEl) worldGoldDetailsEl.innerHTML = '';
                    if (lastUpdatedEl) lastUpdatedEl.textContent = '—';
                    pending = false;
                    return;
                }
                showEmptyMessage(false);

                const labels = data.map(p => tsToLabel(p.timestamp));
                const buys = data.map(p => (p.buy === null ? null : Number(p.buy)));
                const sells = data.map(p => (p.sell === null ? null : Number(p.sell)));
                const last = data[data.length - 1];

                if (currentBuyEl) {
                    currentBuyEl.textContent = last && last.buy ? formatNumber(last.buy) : '—';
                    if (currentBuyDiffEl) {
                        const prevBuy = getPrevDiffValue(data, 'buy', last ? last.buy : null);
                        currentBuyDiffEl.innerHTML = (last && last.buy && prevBuy) ? formatDiffStr(last.buy - prevBuy) : '';
                    }
                }
                if (currentSellEl) {
                    currentSellEl.textContent = last && last.sell ? formatNumber(last.sell) : '—';
                    if (currentSellDiffEl) {
                        const prevSell = getPrevDiffValue(data, 'sell', last ? last.sell : null);
                        currentSellDiffEl.innerHTML = (last && last.sell && prevSell) ? formatDiffStr(last.sell - prevSell) : '';
                    }
                }
                if (currentWorldGoldEl) {
                    const lastWorld = worldData && worldData.length > 0 ? worldData[worldData.length - 1] : null;
                    if (lastWorld && lastWorld.buy) {
                        currentWorldGoldEl.textContent = formatNumber(lastWorld.buy);
                        if (currentWorldGoldDiffEl) {
                            const prevWorldBuy = getPrevDiffValue(worldData, 'buy', lastWorld.buy);
                            currentWorldGoldDiffEl.innerHTML = prevWorldBuy ? formatDiffStr(lastWorld.buy - prevWorldBuy) : '';
                        }
                        if (worldGoldDetailsEl) {
                            const USD_TO_VND = 26000;
                            const OZ_TO_CHI = 37.5 / 31.1034768 / 10;
                            const usdStr = (lastWorld.buy / (USD_TO_VND * OZ_TO_CHI)).toLocaleString(undefined, {minimumFractionDigits: 1, maximumFractionDigits: 1});
                            
                            let diffStr = '';
                            if (last && last.sell) {
                                const diff = last.sell - lastWorld.buy;
                                const sign = diff > 0 ? '+' : '';
                                diffStr = `<br>Chênh lệch giá bán: <span style="color:${diff > 0 ? '#f87171' : '#34d399'}">${sign}${formatNumber(diff)}</span>`;
                            }
                            worldGoldDetailsEl.innerHTML = `${usdStr} USD/oz${diffStr}`;
                        }
                    } else {
                        currentWorldGoldEl.textContent = '—';
                        if (worldGoldDetailsEl) worldGoldDetailsEl.innerHTML = '';
                    }
                }
                if (lastUpdatedEl) lastUpdatedEl.textContent = last ? 'Cập nhật: ' + new Date(last.timestamp * 1000).toLocaleString() : '—';

                if (!chart) {
                    createChart(labels, buys, sells, worldData);
                } else {
                    try {
                        chart.data.labels = labels;
                        chart.data.datasets[0].data = buys;
                        chart.data.datasets[1].data = sells;
                        // Update or add world gold dataset
                        if (worldData && worldData.length > 0) {
                            const worldMap = {};
                            worldData.forEach(p => { worldMap[tsToLabel(p.timestamp)] = p.buy; });
                            const worldLine = labels.map(l => worldMap[l] ?? null);
                            if (chart.data.datasets[2]) {
                                chart.data.datasets[2].data = worldLine;
                            } else {
                                chart.data.datasets.push({
                                    label: '🌍 Vàng Thế Giới (VNĐ/chỉ)',
                                    data: worldLine,
                                    fill: false,
                                    tension: 0.25,
                                    pointRadius: 3,
                                    borderWidth: 2,
                                    borderDash: [5, 3],
                                    borderColor: '#34d399',
                                    pointBackgroundColor: '#34d399',
                                    spanGaps: true
                                });
                            }
                        }
                        chart.update();
                    } catch (e) {
                        console.warn('chart update failed, recreating', e);
                        destroyChartIfExists();
                        createChart(labels, buys, sells, worldData);
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

        // initial
        setActiveMode('7d');
        updateChart();
        setInterval(updateChart, 60_000);


        // ================================================================
        // ⛽ OIL PRICE MODULE
        // ================================================================
        const oilTableBody = document.getElementById('oilTableBody');
        const oilLatestDate = document.getElementById('oilLatestDate');
        const oilTableView = document.getElementById('oilTableView');
        const oilChartView = document.getElementById('oilChartView');
        const oilTableEmpty = document.getElementById('oilTableEmpty');
        const oilChartEmpty = document.getElementById('oilChartEmpty');
        const oilCanvas = document.getElementById('oilChart');
        const oilModeTableBtn = document.getElementById('oilModeTableBtn');
        const oilMode7Btn = document.getElementById('oilMode7Btn');
        const oilMode30Btn = document.getElementById('oilMode30Btn');
        const oilBackfillBtn = document.getElementById('oilBackfillBtn');

        let oilChart = null;
        let oilMode = 'table'; // 'table' | '7d' | '30d'
        let oilData = null;   // last fetched response

        function setOilMode(mode) {
            oilMode = mode;
            [oilModeTableBtn, oilMode7Btn, oilMode30Btn].forEach(b => b && b.classList.remove('active'));
            if (mode === 'table') {
                oilModeTableBtn && oilModeTableBtn.classList.add('active');
                oilTableView && (oilTableView.style.display = '');
                oilChartView && (oilChartView.style.display = 'none');
            } else {
                if (mode === '7d') oilMode7Btn && oilMode7Btn.classList.add('active');
                else oilMode30Btn && oilMode30Btn.classList.add('active');
                oilTableView && (oilTableView.style.display = 'none');
                oilChartView && (oilChartView.style.display = '');
            }
        }

        function changeClass(raw) {
            if (!raw) return 'neutral';
            // PVOil change field may be like "+500đ" or "-200đ" or "0"
            const s = raw.trim();
            if (s.startsWith('+') || (s !== '0' && !s.startsWith('-') && s !== '' && !s.startsWith('0'))) return 'up';
            if (s.startsWith('-')) return 'down';
            return 'neutral';
        }

        function changeIcon(cls) {
            if (cls === 'up') return '▲';
            if (cls === 'down') return '▼';
            return '—';
        }

        function renderOilTable(items) {
            if (!oilTableBody) return;
            if (!items || items.length === 0) {
                if (oilTableEmpty) oilTableEmpty.style.display = '';
                const tbl = document.getElementById('oilTable');
                if (tbl) tbl.style.display = 'none';
                return;
            }
            if (oilTableEmpty) oilTableEmpty.style.display = 'none';
            const tbl = document.getElementById('oilTable');
            if (tbl) tbl.style.display = '';
            oilTableBody.innerHTML = items.map(item => {
                const cls = changeClass(item.change);
                const icon = changeIcon(cls);
                const priceStr = item.price ? Number(item.price).toLocaleString('vi-VN') : '—';
                return `<tr>
                    <td>${item.name}</td>
                    <td class="oil-price">${priceStr}đ</td>
                    <td><span class="oil-change ${cls}">${icon} ${item.change || '—'}</span></td>
                </tr>`;
            }).join('');
        }

        // Pick a set of distinct colors for chart lines
        const OIL_COLORS = [
            '#60a5fa', '#f59e0b', '#34d399', '#f87171',
            '#a78bfa', '#fb923c', '#38bdf8', '#e879f9'
        ];

        function renderOilChart(history) {
            if (!oilCanvas) return;
            if (!history || history.length === 0) {
                if (oilChartEmpty) oilChartEmpty.style.display = '';
                oilCanvas.style.display = 'none';
                return;
            }
            if (oilChartEmpty) oilChartEmpty.style.display = 'none';
            oilCanvas.style.display = '';

            const labels = history.map(d => d.date);

            // Collect unique product names from all days
            const namesSet = new Set();
            history.forEach(d => d.items.forEach(it => namesSet.add(it.name)));
            const names = [...namesSet];

            // Build index: date → {name → price}
            const byDateName = {};
            history.forEach(d => {
                byDateName[d.date] = {};
                d.items.forEach(it => { byDateName[d.date][it.name] = it.price; });
            });

            const datasets = names.map((name, i) => ({
                label: name,
                data: labels.map(d => byDateName[d][name] ?? null),
                fill: false,
                tension: 0.2,
                pointRadius: 3,
                borderWidth: 2,
                borderColor: OIL_COLORS[i % OIL_COLORS.length],
                pointBackgroundColor: OIL_COLORS[i % OIL_COLORS.length],
                spanGaps: true
            }));

            if (oilChart) {
                try {
                    oilChart.data.labels = labels;
                    oilChart.data.datasets = datasets;
                    oilChart.update();
                    return;
                } catch (e) {
                    oilChart.destroy();
                    oilChart = null;
                }
            }

            const ctx = oilCanvas.getContext('2d');
            oilChart = new Chart(ctx, {
                type: 'line',
                data: { labels, datasets },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: true, labels: { color: '#E6EEF8', boxWidth: 12, font: { size: 12 } } },
                        tooltip: {
                            interaction: { mode: 'nearest', intersect: false },
                            callbacks: {
                                label: ctx => {
                                    const v = ctx.raw;
                                    if (v == null) return ctx.dataset.label + ': —';
                                    return ctx.dataset.label + ': ' + Number(v).toLocaleString('vi-VN') + 'đ';
                                }
                            }
                        }
                    },
                    scales: {
                        x: { ticks: { color: '#CFE6FF', maxRotation: 45, minRotation: 45 }, grid: { color: 'rgba(255,255,255,0.03)' } },
                        y: { ticks: { color: '#CFE6FF', callback: v => Number(v).toLocaleString('vi-VN') }, grid: { color: 'rgba(255,255,255,0.03)' } }
                    }
                }
            });
        }

        async function fetchAndRenderOil() {
            const apiMode = oilMode === 'table' ? '7d' : oilMode;
            try {
                const resp = await fetch(`${window.API.OIL_PRICES}?mode=${apiMode}`);
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                oilData = await resp.json();
            } catch (e) {
                console.error('fetchOilPrices error', e);
                return;
            }
            if (oilLatestDate && oilData.latest_date) {
                oilLatestDate.textContent = oilData.latest_date;
            }
            if (oilMode === 'table') {
                renderOilTable(oilData.latest || []);
            } else {
                renderOilChart(oilData.history || []);
            }
        }

        if (oilModeTableBtn) oilModeTableBtn.addEventListener('click', () => { setOilMode('table'); fetchAndRenderOil(); });
        if (oilMode7Btn) oilMode7Btn.addEventListener('click', () => { setOilMode('7d'); fetchAndRenderOil(); });
        if (oilMode30Btn) oilMode30Btn.addEventListener('click', () => { setOilMode('30d'); fetchAndRenderOil(); });

        // Initial oil load + auto-refresh every 30 min
        fetchAndRenderOil();
        setInterval(fetchAndRenderOil, 30 * 60 * 1000);
    });
})();
