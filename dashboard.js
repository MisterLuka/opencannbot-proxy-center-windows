(function () {
  const root = document.documentElement;

  function byId(id) { return document.getElementById(id); }
  function text(id, value) { const el = byId(id); if (el) el.textContent = value; }

  async function copyText(value, button) {
    try {
      await navigator.clipboard.writeText(value);
      const old = button.textContent;
      button.textContent = '已复制';
      setTimeout(() => { button.textContent = old; }, 1200);
    } catch (_) {
      button.textContent = '复制失败';
    }
  }

  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', () => copyText(button.dataset.copy || '', button));
  });

  document.querySelectorAll('[data-model-select]').forEach((select) => {
    const id = select.dataset.modelSelect;
    const button = document.querySelector(`[data-model-button="${id}"]`);
    if (!button) return;
    const sync = () => { button.disabled = select.value === select.dataset.current; };
    select.addEventListener('change', sync);
    sync();
  });

  async function refresh() {
    try {
      const response = await fetch('/api/status', { cache: 'no-store' });
      if (!response.ok) return;
      const payload = await response.json();
      text('stat-total', `${payload.summary.total} / ${payload.summary.max}`);
      text('stat-running', String(payload.summary.running));
      text('stat-ports', String(payload.summary.ports.length));
      text('stat-models', String(payload.summary.models.length));
      text('stat-total-note', payload.summary.remaining > 0 ? `还可创建 ${payload.summary.remaining} 个实例` : '已达到实例上限');
      text('stat-running-note', `${payload.summary.healthy} 个实例健康运行`);
      text('stat-ports-note', payload.summary.ports.join(' · ') || '暂无监听端口');
      text('stat-models-note', payload.summary.model_labels.join(' · ') || '暂无模型路由');

      (payload.instances || []).forEach((item) => {
        const card = document.querySelector(`[data-instance-card="${item.id}"]`);
        if (!card) return;
        card.classList.toggle('running', item.running);
        card.classList.toggle('stopped', !item.running);
        const pill = card.querySelector('[data-role="status"]');
        if (pill) {
          pill.className = 'status-pill ' + (item.external ? 'external' : item.healthy ? 'running' : item.running ? 'unhealthy' : 'stopped');
          pill.textContent = item.external ? '外部运行' : item.healthy ? '运行中' : item.running ? '响应异常' : '已停止';
        }
        const process = card.querySelector('[data-role="process"]');
        if (process) process.textContent = item.pid ? `PID ${item.pid}` : (item.external ? '外部进程' : '—');
        const model = card.querySelector('[data-role="model"]');
        if (model) model.textContent = item.model_label;
        const start = card.querySelector('[data-action="start"]');
        const restart = card.querySelector('[data-action="restart"]');
        const stop = card.querySelector('[data-action="stop"]');
        if (start) start.disabled = item.running || !payload.repo_ready || !item.vk_configured;
        if (restart) restart.disabled = !item.managed;
        if (stop) stop.disabled = !item.managed;
      });
      text('auto-refresh-label', `更新于 ${new Date().toLocaleTimeString()}`);
    } catch (_) {
      text('auto-refresh-label', '状态刷新失败');
    }
  }

  const refreshButton = byId('refresh-button');
  if (refreshButton) refreshButton.addEventListener('click', refresh);
  setInterval(refresh, 5000);
})();
