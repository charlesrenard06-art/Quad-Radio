(() => {
  const colorKey = 'gr-site-theme-color';
  const modeKey = 'gr-site-theme-mode';
  const root = document.documentElement;

  function applyColor(value) {
    if (!/^#[0-9a-f]{6}$/i.test(value)) return;
    const r = parseInt(value.slice(1, 3), 16);
    const g = parseInt(value.slice(3, 5), 16);
    const b = parseInt(value.slice(5, 7), 16);
    root.style.setProperty('--accent', value);
    root.style.setProperty('--accent-rgb', `${r}, ${g}, ${b}`);
    document.querySelectorAll('[data-theme-color]').forEach(control => {
      control.value = value;
    });
  }

  function applyMode(mode) {
    mode = mode === 'light' ? 'light' : 'dark';
    root.dataset.themeMode = mode;
    if (document.body) {
      document.body.classList.toggle('notes-light', mode === 'light');
    }
    document.querySelectorAll('#theme-toggle, .theme-mode-toggle').forEach(button => {
      button.textContent = mode === 'dark' ? 'Light mode' : 'Dark mode';
      button.setAttribute('aria-label', 'Switch to ' + (mode === 'dark' ? 'light' : 'dark') + ' mode');
    });
  }

  let savedColor = '#ff405c';
  let savedMode = 'dark';
  try {
    savedColor = localStorage.getItem(colorKey) || savedColor;
    savedMode = localStorage.getItem(modeKey) || savedMode;
  } catch (_) {}

  applyColor(savedColor);
  applyMode(savedMode);

  let buttons = [...document.querySelectorAll('#theme-toggle, .theme-mode-toggle')];
  if (!buttons.length) {
    const nav = document.querySelector('.site-nav');
    if (nav) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'theme-mode-toggle';
      button.textContent = savedMode === 'dark' ? 'Light mode' : 'Dark mode';
      nav.appendChild(button);
      buttons = [button];
    }
  }

  buttons.forEach(button => {
    button.addEventListener('click', () => {
      const next = root.dataset.themeMode === 'dark' ? 'light' : 'dark';
      applyMode(next);
      try { localStorage.setItem(modeKey, next); } catch (_) {}
    });
  });

  document.querySelectorAll('[data-theme-color]').forEach(control => {
    control.addEventListener('input', () => {
      applyColor(control.value);
      try { localStorage.setItem(colorKey, control.value); } catch (_) {}
    });
  });

  window.addEventListener('storage', event => {
    if (event.key === colorKey && event.newValue) applyColor(event.newValue);
    if (event.key === modeKey && event.newValue) applyMode(event.newValue);
  });
})();
