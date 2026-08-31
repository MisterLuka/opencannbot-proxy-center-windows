(function () {
  const root = document.querySelector('[data-job-id]');
  if (!root) return;
  const jobId = root.dataset.jobId;
  const redirectDelay = 1400;

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function render(job) {
    const card = document.getElementById('restart-card');
    if (card) {
      card.classList.toggle('success', Boolean(job.done && job.success));
      card.classList.toggle('failed', Boolean(job.done && !job.success));
    }
    const percent = Math.max(0, Math.min(100, Number(job.progress || 0)));
    setText('job-percent', `${percent}%`);
    const bar = document.getElementById('job-progress');
    if (bar) bar.style.width = `${percent}%`;
    setText('job-chip', job.done ? (job.success ? '已完成' : '执行失败') : '正在执行');
    setText('job-title', job.title || '正在处理代理实例');
    setText('job-message', job.message || '请稍候');
    setText('job-duration', `${Number(job.elapsed || 0).toFixed(1)} 秒`);
    setText('job-log', (job.logs || []).join('\n') || '等待任务日志...');

    (job.steps || []).forEach((step, index) => {
      const row = document.querySelector(`[data-step-index="${index}"]`);
      if (!row) return;
      row.classList.toggle('done', step.state === 'done');
      row.classList.toggle('active', step.state === 'active');
      row.classList.toggle('failed', step.state === 'failed');
      const state = row.querySelector('[data-role="step-state"]');
      if (state) state.textContent = step.state_label || '等待中';
      const detail = row.querySelector('[data-role="step-detail"]');
      if (detail) detail.textContent = step.detail || '';
    });

    const error = document.getElementById('job-error');
    if (error) {
      error.classList.toggle('show', Boolean(job.done && !job.success));
      error.textContent = job.error || '任务执行失败，请检查实例日志。';
    }

    const retry = document.getElementById('job-retry');
    if (retry) retry.classList.toggle('hidden', !(job.done && !job.success));

    if (job.done && job.success) {
      setTimeout(() => { window.location.href = job.redirect_url || '/'; }, redirectDelay);
    }
  }

  async function poll() {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { cache: 'no-store' });
      if (!response.ok) throw new Error('job request failed');
      const job = await response.json();
      render(job);
      if (!job.done) setTimeout(poll, 600);
    } catch (_) {
      setText('job-message', '无法读取任务状态，正在重试...');
      setTimeout(poll, 1200);
    }
  }

  poll();
})();
