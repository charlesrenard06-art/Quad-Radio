(() => {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const canvas = document.createElement("canvas");
  canvas.id = "quad-particles";
  canvas.setAttribute("aria-hidden", "true");
  document.body.prepend(canvas);

  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  let width = 0;
  let height = 0;
  let particles = [];
  let accent = "255, 64, 92";

  function readAccent() {
    const value = getComputedStyle(document.body)
      .getPropertyValue("--accent-rgb").trim();

    if (/^[\d\s,.]+$/.test(value)) accent = value;
  }

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const count = Math.min(90, Math.max(30, Math.floor(width * height / 17000)));

    particles = Array.from({ length: count }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      radius: Math.random() * 1.8 + .5,
      vx: (Math.random() - .5) * .32,
      vy: (Math.random() - .5) * .32,
      alpha: Math.random() * .5 + .2
    }));
  }

  function animate() {
    ctx.clearRect(0, 0, width, height);

    for (let i = 0; i < particles.length; i++) {
      const p = particles[i];

      p.x += p.vx;
      p.y += p.vy;

      if (p.x < -5) p.x = width + 5;
      if (p.x > width + 5) p.x = -5;
      if (p.y < -5) p.y = height + 5;
      if (p.y > height + 5) p.y = -5;

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${accent},${p.alpha})`;
      ctx.shadowBlur = 9;
      ctx.shadowColor = `rgba(${accent},.55)`;
      ctx.fill();
      ctx.shadowBlur = 0;

      for (let j = i + 1; j < particles.length; j++) {
        const q = particles[j];
        const dx = p.x - q.x;
        const dy = p.y - q.y;
        const distance = Math.sqrt(dx * dx + dy * dy);

        if (distance < 115) {
          ctx.beginPath();
          ctx.moveTo(p.x, p.y);
          ctx.lineTo(q.x, q.y);
          ctx.strokeStyle = `rgba(${accent},${.12 * (1 - distance / 115)})`;
          ctx.lineWidth = .7;
          ctx.stroke();
        }
      }
    }

    requestAnimationFrame(animate);
  }

  readAccent();
  resize();
  window.addEventListener("resize", resize);
  window.addEventListener("storage", readAccent);
  animate();
})();
