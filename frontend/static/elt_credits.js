/* elt_credits.js — 积分计费前端助手（Task 8）
 *
 * 职责：
 * 1. billableFetch(opType, url, options)：自动携带每次点击新生成的 Idempotency-Key
 *    （同一 in-flight 动作的手动重试可复用 options.idempotencyKey 传入旧 key；
 *     新的用户主动动作不传 → 自动新 UUID），统一处理 402/409；
 * 2. rates()/quoteText(opType)：拉取并缓存 /api/credits/rates，供按钮展示当前费率；
 * 3. annotate(root)：扫描 [data-billable-op] 元素，把当前费率附加到按钮提示。
 *
 * 单用户/公开库：/api/credits/rates 404 或返回 mode=off 时全部静默降级——
 * 不带 key 也能正常工作（后端 no-op），费率标注不显示。
 */
window.eltCredits = (() => {
  let _ratesPromise = null;

  async function rates() {
    if (_ratesPromise) return _ratesPromise;
    _ratesPromise = (async () => {
      try {
        const resp = await fetch('/api/credits/rates');
        if (!resp.ok) return null;
        const data = await resp.json();
        if (!data || data.mode === 'off' || !data.rates) return null;
        return data.rates;
      } catch (_e) {
        return null;
      }
    })();
    return _ratesPromise;
  }

  function quoteText(rateMap, opType) {
    const rate = rateMap && rateMap[opType];
    if (!rate) return '';
    const points = rate.points != null ? rate.points : (rate.unit_points != null ? rate.unit_points : null);
    if (points == null) return '';
    if (points === 0) return '免费';
    const unit = rate.unit || 'per_call';
    if (unit === 'per_minute') return `约 ${points} 积分/分钟`;
    if (unit === 'per_1000_chars') return `约 ${points} 积分/千字`;
    return `约 ${points} 积分/次`;
  }

  async function annotate(root) {
    const scope = root || document;
    const rateMap = await rates();
    if (!rateMap) return;
    scope.querySelectorAll('[data-billable-op]').forEach((el) => {
      const text = quoteText(rateMap, el.getAttribute('data-billable-op'));
      if (!text) return;
      el.title = el.title ? `${el.title}（${text}）` : text;
      if (!el.querySelector('.elt-rate-hint')) {
        const hint = document.createElement('span');
        hint.className = 'elt-rate-hint';
        hint.style.cssText = 'margin-left:4px;font-size:11px;opacity:.65;font-weight:400;';
        hint.textContent = text;
        el.appendChild(hint);
      }
    });
  }

  function _billingMessage(status, payload) {
    const detail = payload && (payload.detail || payload.error_info || payload);
    if (status === 402 && detail && typeof detail === 'object') {
      return `积分不足：本次操作需要 ${detail.required} 积分，当前可用 ${detail.available}。`;
    }
    if (status === 409) {
      if (detail && typeof detail === 'object' && detail.code === 'key_released') {
        return '上次操作未成功且已退回积分，请重新点击再试。';
      }
      return '操作冲突：请勿重复提交；若非重复请点击重试。';
    }
    if (status === 400) return '请求缺少幂等键，请刷新页面后重试。';
    return '';
  }

  async function billableFetch(opType, url, options) {
    const opts = Object.assign({}, options || {});
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body && !headers['Content-Type'] && !(opts.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
    }
    // 稳定 per-click UUID：调用方显式传 idempotencyKey 表示 in-flight 重试复用
    headers['Idempotency-Key'] = opts.idempotencyKey || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
    delete opts.idempotencyKey;
    opts.headers = headers;
    const resp = await fetch(url, opts);
    if (resp.status === 402 || resp.status === 409) {
      let payload = null;
      try { payload = await resp.clone().json(); } catch (_e) { /* ignore */ }
      const message = _billingMessage(resp.status, payload);
      const err = new Error(message || `billing error ${resp.status}`);
      err.billing = { status: resp.status, payload, operationType: opType };
      throw err;
    }
    return resp;
  }

  return { rates, quoteText, annotate, billableFetch };
})();
