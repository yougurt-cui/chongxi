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
