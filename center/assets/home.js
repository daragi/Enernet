(() => {
  const root = document.documentElement;
  const stage = document.querySelector("[data-home-stage]");
  const particleCanvas = document.querySelector("[data-home-particles]");
  const revealItems = document.querySelectorAll("[data-reveal]");
  const parallaxItems = document.querySelectorAll("[data-parallax]");
  const cards = document.querySelectorAll("[data-tilt-card]");
  const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");

  root.classList.add("home-enhanced");

  const reveal = () => {
    revealItems.forEach((item) => item.classList.add("is-visible"));
  };

  window.requestAnimationFrame(() => window.requestAnimationFrame(reveal));

  const startParticleField = () => {
    if (!stage || !particleCanvas) return;
    const context = particleCanvas.getContext("2d", { alpha: true });
    if (!context) return;

    const pointer = { x: 0, y: 0, active: false };
    const particles = [];
    const palette = ["30,58,98", "47,115,152", "238,51,65"];
    let width = 0;
    let height = 0;
    let frame = 0;
    let lastTime = 0;
    let running = true;

    const makeParticle = (index) => {
      const angle = Math.random() * Math.PI * 2;
      const speed = 0.1 + Math.random() * 0.2;
      return {
        x: Math.random() * width,
        y: Math.random() * height,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        driftX: Math.cos(angle) * speed,
        driftY: Math.sin(angle) * speed,
        radius: 1.35 + Math.random() * 2.1,
        color: palette[index % palette.length],
        alpha: 0.28 + Math.random() * 0.28,
        halo: index % 7 === 0,
      };
    };

    const resize = () => {
      const rect = stage.getBoundingClientRect();
      width = Math.max(1, Math.round(rect.width));
      height = Math.max(1, Math.round(rect.height));
      const dpr = Math.min(1.6, window.devicePixelRatio || 1);
      particleCanvas.width = Math.round(width * dpr);
      particleCanvas.height = Math.round(height * dpr);
      particleCanvas.style.width = `${width}px`;
      particleCanvas.style.height = `${height}px`;
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      const hardwareScale = (navigator.hardwareConcurrency || 8) <= 4 ? 0.68 : 1;
      const targetCount = Math.round(Math.min(88, Math.max(36, width * height / 17000)) * hardwareScale);
      while (particles.length < targetCount) particles.push(makeParticle(particles.length));
      if (particles.length > targetCount) particles.length = targetCount;
      particles.forEach((particle) => {
        particle.x = Math.min(width, Math.max(0, particle.x));
        particle.y = Math.min(height, Math.max(0, particle.y));
      });
    };

    const draw = (time) => {
      if (!running) return;
      const delta = Math.min(2, Math.max(0.35, (time - lastTime) / 16.67 || 1));
      lastTime = time;
      context.clearRect(0, 0, width, height);

      particles.forEach((particle) => {
        if (pointer.active) {
          const dx = particle.x - pointer.x;
          const dy = particle.y - pointer.y;
          const distance = Math.hypot(dx, dy) || 1;
          if (distance < 165) {
            const force = (165 - distance) / 165;
            particle.vx += dx / distance * force * 0.022 * delta;
            particle.vy += dy / distance * force * 0.022 * delta;
          }
        }
        particle.vx += (particle.driftX - particle.vx) * 0.006 * delta;
        particle.vy += (particle.driftY - particle.vy) * 0.006 * delta;
        const speed = Math.hypot(particle.vx, particle.vy);
        if (speed > 0.62) {
          particle.vx = particle.vx / speed * 0.62;
          particle.vy = particle.vy / speed * 0.62;
        }
        particle.x += particle.vx * delta;
        particle.y += particle.vy * delta;
        if (particle.x < -8) particle.x = width + 8;
        else if (particle.x > width + 8) particle.x = -8;
        if (particle.y < -8) particle.y = height + 8;
        else if (particle.y > height + 8) particle.y = -8;
      });

      for (let first = 0; first < particles.length; first += 1) {
        const a = particles[first];
        for (let second = first + 1; second < particles.length; second += 1) {
          const b = particles[second];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const distanceSquared = dx * dx + dy * dy;
          if (distanceSquared > 116 * 116) continue;
          const alpha = (1 - Math.sqrt(distanceSquared) / 116) * 0.09;
          context.beginPath();
          context.moveTo(a.x, a.y);
          context.lineTo(b.x, b.y);
          context.strokeStyle = `rgba(30,58,98,${alpha.toFixed(3)})`;
          context.lineWidth = 0.8;
          context.stroke();
        }
      }

      if (pointer.active) {
        const glow = context.createRadialGradient(pointer.x, pointer.y, 0, pointer.x, pointer.y, 150);
        glow.addColorStop(0, "rgba(238,51,65,0.07)");
        glow.addColorStop(1, "rgba(238,51,65,0)");
        context.fillStyle = glow;
        context.fillRect(pointer.x - 150, pointer.y - 150, 300, 300);

        particles.forEach((particle) => {
          const distance = Math.hypot(particle.x - pointer.x, particle.y - pointer.y);
          if (distance >= 145) return;
          context.beginPath();
          context.moveTo(pointer.x, pointer.y);
          context.lineTo(particle.x, particle.y);
          context.strokeStyle = `rgba(238,51,65,${((1 - distance / 145) * 0.18).toFixed(3)})`;
          context.lineWidth = 0.85;
          context.stroke();
        });
      }

      particles.forEach((particle) => {
        context.beginPath();
        context.arc(particle.x, particle.y, particle.radius, 0, Math.PI * 2);
        context.fillStyle = `rgba(${particle.color},${particle.alpha})`;
        context.fill();
        if (particle.halo) {
          context.beginPath();
          context.arc(particle.x, particle.y, particle.radius + 4.5, 0, Math.PI * 2);
          context.strokeStyle = `rgba(${particle.color},0.12)`;
          context.lineWidth = 1;
          context.stroke();
        }
      });
      frame = window.requestAnimationFrame(draw);
    };

    const updatePointer = (event) => {
      const rect = stage.getBoundingClientRect();
      pointer.x = event.clientX - rect.left;
      pointer.y = event.clientY - rect.top;
      pointer.active = true;
    };
    const clearPointer = () => { pointer.active = false; };
    const handleVisibility = () => {
      if (document.hidden) {
        running = false;
        window.cancelAnimationFrame(frame);
      } else if (!running) {
        running = true;
        lastTime = performance.now();
        frame = window.requestAnimationFrame(draw);
      }
    };

    const observer = "ResizeObserver" in window ? new ResizeObserver(resize) : null;
    observer?.observe(stage);
    window.addEventListener("resize", resize, { passive: true });
    if (finePointer.matches) {
      stage.addEventListener("pointermove", updatePointer, { passive: true });
      stage.addEventListener("pointerleave", clearPointer, { passive: true });
    }
    document.addEventListener("visibilitychange", handleVisibility);
    resize();
    frame = window.requestAnimationFrame(draw);
  };

  startParticleField();

  if (!stage || !finePointer.matches) return;

  let pointerFrame = 0;
  let pendingPointer = null;

  const renderPointer = () => {
    pointerFrame = 0;
    if (!pendingPointer) return;

    const rect = stage.getBoundingClientRect();
    const xRatio = Math.min(1, Math.max(0, (pendingPointer.clientX - rect.left) / rect.width));
    const yRatio = Math.min(1, Math.max(0, (pendingPointer.clientY - rect.top) / rect.height));
    const xOffset = xRatio - 0.5;
    const yOffset = yRatio - 0.5;

    stage.style.setProperty("--pointer-x", `${(xRatio * 100).toFixed(2)}%`);
    stage.style.setProperty("--pointer-y", `${(yRatio * 100).toFixed(2)}%`);
    stage.style.setProperty("--scene-x", `${(xOffset * 24).toFixed(2)}px`);
    stage.style.setProperty("--scene-y", `${(yOffset * 18).toFixed(2)}px`);

    parallaxItems.forEach((item) => {
      const strength = Number.parseFloat(item.dataset.parallax || "0.4");
      item.style.setProperty("--parallax-x", `${(xOffset * 22 * strength).toFixed(2)}px`);
      item.style.setProperty("--parallax-y", `${(yOffset * 18 * strength).toFixed(2)}px`);
    });
  };

  stage.addEventListener("pointermove", (event) => {
    pendingPointer = event;
    if (!pointerFrame) pointerFrame = window.requestAnimationFrame(renderPointer);
  }, { passive: true });

  stage.addEventListener("pointerleave", () => {
    pendingPointer = null;
    stage.style.setProperty("--pointer-x", "50%");
    stage.style.setProperty("--pointer-y", "50%");
    stage.style.setProperty("--scene-x", "0px");
    stage.style.setProperty("--scene-y", "0px");
    parallaxItems.forEach((item) => {
      item.style.setProperty("--parallax-x", "0px");
      item.style.setProperty("--parallax-y", "0px");
    });
  });

  const resetCard = (card) => {
    card.style.setProperty("--tilt-x", "0deg");
    card.style.setProperty("--tilt-y", "0deg");
    card.style.setProperty("--card-pointer-x", "50%");
    card.style.setProperty("--card-pointer-y", "50%");
  };

  cards.forEach((card) => {
    let cardFrame = 0;
    let cardPointer = null;

    const renderCard = () => {
      cardFrame = 0;
      if (!cardPointer) return;
      const rect = card.getBoundingClientRect();
      const xRatio = Math.min(1, Math.max(0, (cardPointer.clientX - rect.left) / rect.width));
      const yRatio = Math.min(1, Math.max(0, (cardPointer.clientY - rect.top) / rect.height));
      card.style.setProperty("--tilt-x", `${((xRatio - 0.5) * 7).toFixed(2)}deg`);
      card.style.setProperty("--tilt-y", `${((0.5 - yRatio) * 6).toFixed(2)}deg`);
      card.style.setProperty("--card-pointer-x", `${(xRatio * 100).toFixed(2)}%`);
      card.style.setProperty("--card-pointer-y", `${(yRatio * 100).toFixed(2)}%`);
    };

    card.addEventListener("pointermove", (event) => {
      cardPointer = event;
      if (!cardFrame) cardFrame = window.requestAnimationFrame(renderCard);
    }, { passive: true });

    card.addEventListener("pointerleave", () => {
      cardPointer = null;
      resetCard(card);
    });

    card.addEventListener("blur", () => resetCard(card));
  });
})();
