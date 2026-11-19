(() => {
  const button = document.querySelector('.back-to-top');
  const top = document.getElementById('top');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let scheduled = false;

  function updateVisibility() {
    const visible = window.scrollY > window.innerHeight;
    button.classList.toggle('is-visible', visible);
    button.disabled = !visible;
    button.tabIndex = visible ? 0 : -1;
    button.setAttribute('aria-hidden', String(!visible));
    scheduled = false;
  }

  function scheduleUpdate() {
    if (!scheduled) {
      scheduled = true;
      window.requestAnimationFrame(updateVisibility);
    }
  }

  window.addEventListener('scroll', scheduleUpdate, { passive: true });
  window.addEventListener('resize', scheduleUpdate);
  window.addEventListener('pageshow', scheduleUpdate);
  button.addEventListener('click', () => {
    // Move keyboard focus before hiding the floating control during scrolling.
    top.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: reducedMotion.matches ? 'instant' : 'smooth' });
  });
  updateVisibility();
})();

(() => {
  const form = document.getElementById('lead-form');
  const status = document.getElementById('lead-status');
  const submit = form.querySelector('[type="submit"]');
  let busy = false;
  function show(message, state) { status.textContent = message; status.dataset.state = state; }
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    if (location.protocol === 'file:') {
      show('当前为本地预览，信息尚未提交。请在已部署的合作页面提交。', 'error');
      return;
    }
    const data = Object.fromEntries(new FormData(form));
    data.consent = form.elements.consent.checked;
    busy = true; submit.disabled = true; submit.textContent = '正在提交…';
    show('正在保存合作信息…', 'pending');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(form.action, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data), signal: controller.signal,
      });
      const result = await response.json().catch(() => null);
      if (!response.ok || !result || result.ok !== true || !result.id) {
        throw new Error(result && result.message || '暂时无法提交，请稍后重试。');
      }
      form.reset();
      show('合作意向已收到，感谢你的关注！我们会通过预留手机号与你联系。', 'success');
    } catch (error) {
      show(error.name === 'AbortError' ? '提交超时，尚未确认保存。请重试，相同信息不会重复登记。' : error.message || '网络异常，请稍后重试。', 'error');
    } finally {
      clearTimeout(timeout); busy = false; submit.disabled = false; submit.textContent = '提交合作意向 ↗';
    }
  });
})();
