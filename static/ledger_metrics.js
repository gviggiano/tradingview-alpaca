/*
 * Trade ordering, pagination, and risk stats for the Ledger dashboard.
 * Mirrors the pure functions in dashboard.py. The page re-runs these on the
 * filtered book so source and period changes stay local.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  }
  root.LedgerMetrics = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const TRADES_PAGE_SIZE = 10;
  const DRAWDOWN_EPSILON = 1e-9;

  function parseNumber(value) {
    if (value == null) return null;
    const text = String(value).trim().replace(/,/g, '');
    if (text === '' || text === '—' || text === '-' || text === 'Open') {
      return null;
    }
    const number = Number(text);
    return Number.isFinite(number) ? number : null;
  }

  function num(value) {
    const parsed = parseNumber(value);
    return parsed == null ? 0 : parsed;
  }

  function sourceOf(trade) {
    return (trade && trade.source) || 'live';
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function closedTime(trade) {
    const date = parseDate(trade && trade.closedAt);
    return date ? date.getTime() : null;
  }

  function filledTime(trade) {
    const date = parseDate(trade && trade.filledAt);
    return date ? date.getTime() : null;
  }

  function compareTimes(aTime, bTime) {
    if (aTime == null && bTime == null) return 0;
    if (aTime == null) return -1;
    if (bTime == null) return 1;
    return aTime - bTime;
  }

  function sortClosedChronological(trades) {
    return [...trades].sort((a, b) => compareTimes(closedTime(a), closedTime(b)));
  }

  function sortClosedNewestFirst(trades) {
    return [...trades].sort((a, b) => compareTimes(closedTime(b), closedTime(a)));
  }

  function sortOpenNewestFirst(trades) {
    return [...trades].sort((a, b) => compareTimes(filledTime(b), filledTime(a)));
  }

  function nextShown(shown, total, action, pageSize = TRADES_PAGE_SIZE) {
    total = Math.max(0, Number(total) || 0);
    if (action === 'init' || action === 'reset' || action === 'filter') {
      return Math.min(total, pageSize);
    }
    if (action === 'all') return total;
    if (action === 'more') {
      const current = Math.max(0, Number(shown) || 0);
      return Math.min(total, current + pageSize);
    }
    throw new Error('Unknown trade window action: ' + action);
  }

  function paginateTrades(tradesNewestFirst, shown, pageSize = TRADES_PAGE_SIZE) {
    const total = tradesNewestFirst.length;
    const visible = Math.max(0, Math.min(total, Number(shown) || 0));
    const remaining = total - visible;
    return {
      trades: tradesNewestFirst.slice(0, visible),
      shown: visible,
      total,
      remaining,
      showMore: remaining > 0,
      nextCount: Math.min(pageSize, remaining),
    };
  }

  function presentTrades(trades, shown, pageSize = TRADES_PAGE_SIZE) {
    const newest = sortClosedNewestFirst(trades);
    const numbers = new Map(
      sortClosedChronological(trades).map((trade, index) => [trade, index + 1])
    );
    const page = paginateTrades(newest, shown, pageSize);
    const rows = page.trades.map(trade => ({
      trade,
      closedAt: trade.closedAt == null ? null : trade.closedAt,
      number: numbers.get(trade),
      pnl: trade.pnl,
      source: sourceOf(trade),
      symbol: trade.symbol,
    }));
    return Object.assign({}, page, { rows });
  }

  function equitySeries(trades, usePct) {
    const ordered = sortClosedChronological(trades);
    let running = 0;
    const points = ordered.map(trade => {
      running += usePct ? num(trade.pnlPct) : num(trade.pnl);
      return running;
    });
    return { trades: ordered, points };
  }

  function isLosingTrade(trade) {
    const outcome = String((trade && trade.outcome) || '').trim().toLowerCase();
    return num(trade && trade.pnl) < 0 || outcome === 'sl';
  }

  function maxLosingStreak(trades) {
    let best = 0;
    let streak = 0;
    for (const trade of sortClosedChronological(trades)) {
      if (isLosingTrade(trade)) {
        streak += 1;
        if (streak > best) best = streak;
      } else {
        streak = 0;
      }
    }
    return best;
  }

  function drawdownPct(drawdown, peak) {
    if (Math.abs(drawdown) < DRAWDOWN_EPSILON) return 0;
    if (peak > 0) return (drawdown / peak) * 100;
    return null;
  }

  function cleanDrawdown(value) {
    return Math.abs(value) < DRAWDOWN_EPSILON ? 0 : value;
  }

  function drawdownStats(trades) {
    const ordered = sortClosedChronological(trades);
    let equity = 0;
    let peak = 0;
    let maxDd = 0;
    let maxDdPct = null;

    for (const trade of ordered) {
      equity += num(trade.pnl);
      if (equity > peak) peak = equity;
      const drawdown = cleanDrawdown(peak - equity);
      const pct = drawdownPct(drawdown, peak);
      if (drawdown > maxDd) {
        maxDd = drawdown;
        maxDdPct = pct;
      } else if (
        Math.abs(drawdown - maxDd) <= DRAWDOWN_EPSILON &&
        pct != null &&
        (maxDdPct == null || pct > maxDdPct)
      ) {
        maxDdPct = pct;
      }
    }

    const current = cleanDrawdown(peak - equity);
    if (!ordered.length) {
      return {
        maxDrawdown: 0,
        maxDrawdownPct: null,
        currentDrawdown: 0,
        currentDrawdownPct: null,
        peakEquity: 0,
        equity: 0,
      };
    }

    return {
      maxDrawdown: maxDd,
      maxDrawdownPct: maxDdPct,
      currentDrawdown: current,
      currentDrawdownPct: drawdownPct(current, peak),
      peakEquity: peak,
      equity,
    };
  }

  function payoffAndExpectancy(trades) {
    if (!trades.length) {
      return {
        payoffRatio: null,
        payoffInfinite: false,
        expectancy: null,
        expectancyPct: null,
        avgWin: null,
        avgLoss: null,
      };
    }

    const wins = [];
    const losses = [];
    let pnlSum = 0;
    let pctSum = 0;
    for (const trade of trades) {
      const pnl = num(trade.pnl);
      pnlSum += pnl;
      pctSum += num(trade.pnlPct);
      if (pnl > 0) wins.push(pnl);
      else if (pnl < 0) losses.push(pnl);
    }

    const avgWin = wins.length
      ? wins.reduce((sum, value) => sum + value, 0) / wins.length
      : null;
    const avgLoss = losses.length
      ? Math.abs(losses.reduce((sum, value) => sum + value, 0) / losses.length)
      : null;

    let payoffRatio = null;
    let payoffInfinite = false;
    if (avgWin != null && avgLoss) {
      payoffRatio = avgWin / avgLoss;
    } else if (avgWin != null && avgLoss == null) {
      payoffInfinite = true;
    } else if (avgWin == null && avgLoss != null) {
      payoffRatio = 0;
    }

    return {
      payoffRatio,
      payoffInfinite,
      expectancy: pnlSum / trades.length,
      expectancyPct: pctSum / trades.length,
      avgWin,
      avgLoss,
    };
  }

  function riskReport(trades) {
    const ordered = [...trades];
    const report = Object.assign(
      { count: ordered.length, maxLosingStreak: maxLosingStreak(ordered) },
      drawdownStats(ordered),
      payoffAndExpectancy(ordered)
    );
    if (!ordered.length) {
      report.maxDrawdownPct = null;
      report.currentDrawdownPct = null;
    }
    return report;
  }

  function riskBySource(trades) {
    const grouped = { sim: [], live: [] };
    for (const trade of trades) {
      const source = sourceOf(trade);
      if (!grouped[source]) grouped[source] = [];
      grouped[source].push(trade);
    }
    const report = {};
    for (const source of Object.keys(grouped)) {
      report[source] = riskReport(grouped[source]);
    }
    return report;
  }

  function sourceHealth(trades) {
    function pack(source) {
      const subset = trades.filter(trade => sourceOf(trade) === source);
      let last = null;
      let lastMs = null;
      let pnl = 0;
      for (const trade of subset) {
        pnl += num(trade.pnl);
        const ms = closedTime(trade);
        if (ms != null && (lastMs == null || ms > lastMs)) {
          lastMs = ms;
          last = trade.closedAt;
        }
      }
      return { count: subset.length, pnl, lastClosedAt: last };
    }

    let last = null;
    let lastMs = null;
    for (const trade of trades) {
      const ms = closedTime(trade);
      if (ms != null && (lastMs == null || ms > lastMs)) {
        lastMs = ms;
        last = trade.closedAt;
      }
    }

    return {
      sim: pack('sim'),
      live: pack('live'),
      lastClosedAt: last,
    };
  }

  function openPositionRisk(position) {
    const entry = parseNumber(position && position.entryPrice) || 0;
    const qty = parseNumber(position && position.qty) || 0;
    const notional = entry * qty;
    const stop = parseNumber(position && position.sl);
    if (stop == null) {
      return { notional, risk: null, riskPct: null };
    }
    const side = String((position && position.side) || 'buy').trim().toLowerCase();
    const risk = side === 'sell' || side === 'short'
      ? (stop - entry) * qty
      : (entry - stop) * qty;
    return {
      notional,
      risk,
      riskPct: notional ? (risk / notional) * 100 : null,
    };
  }

  function summarizeOpenRisk(positions) {
    let notional = 0;
    let risk = 0;
    let withSl = 0;
    let count = 0;
    for (const position of positions) {
      count += 1;
      const row = openPositionRisk(position);
      notional += row.notional;
      if (row.risk != null) {
        risk += row.risk;
        withSl += 1;
      }
    }
    return {
      notional,
      risk: withSl ? risk : null,
      positions: count,
      withSl,
    };
  }

  return {
    TRADES_PAGE_SIZE,
    sortClosedChronological,
    sortClosedNewestFirst,
    sortOpenNewestFirst,
    nextShown,
    paginateTrades,
    presentTrades,
    equitySeries,
    isLosingTrade,
    maxLosingStreak,
    riskReport,
    riskBySource,
    sourceHealth,
    openPositionRisk,
    summarizeOpenRisk,
  };
});
