/**
 * 开屏页组件前端。
 *
 * 和 chat.js 同样的 Streamlit 约定（streamlit==1.66.0）：
 *  1. default export 每次 data 变化都会重跑 —— 事件一律用 el.onclick = 赋值，保持幂等；
 *     背景动画只起一个，记在模块级 LOOP 里，重跑时只更新配色。
 *  2. parentElement 是 ShadowRoot。
 *  3. setTriggerValue 只在点击里调用（「开始聊天」）。
 *
 * 背景是自绘的「流动走势线」：几条金色曲线由不同频率的正弦叠加而成，像慢慢起伏的价格线，
 * 构思参考 React Bits 的 Floating Lines，但代码是这里重写的 Canvas 2D 版本，没有 WebGL 依赖。
 */

const LOOP = { canvas: null, raf: 0, colors: null, observer: null, visible: true };

// 每条线的形状参数。写死而不是随机：每次打开看到的是同一幅画面，截图也稳定。
const LINES = [
  { y: 0.50, a: 0.040, f: 1.6, s: 0.10, p: 0.0, w: 0.9, hue: "a", alpha: 0.28 },
  { y: 0.56, a: 0.050, f: 1.2, s: 0.07, p: 1.3, w: 1.0, hue: "a", alpha: 0.42 },
  { y: 0.62, a: 0.060, f: 1.0, s: 0.09, p: 2.1, w: 1.4, hue: "a", alpha: 0.85 },
  { y: 0.66, a: 0.050, f: 1.4, s: 0.06, p: 3.7, w: 1.0, hue: "b", alpha: 0.50 },
  { y: 0.71, a: 0.055, f: 0.9, s: 0.08, p: 4.4, w: 1.1, hue: "a", alpha: 0.55 },
  { y: 0.76, a: 0.045, f: 1.8, s: 0.11, p: 5.2, w: 0.9, hue: "a", alpha: 0.32 },
  { y: 0.81, a: 0.040, f: 1.1, s: 0.05, p: 0.7, w: 0.8, hue: "c", alpha: 0.22 },
];

function palette(theme) {
  return theme === "dark"
    ? { a: [224, 178, 78], b: [214, 88, 76], c: [200, 205, 212], gain: 1.0 }
    : { a: [150, 110, 14], b: [196, 70, 60], c: [60, 64, 70], gain: 0.62 };
}

function lineY(L, x, t, h) {
  // 三层正弦：大起伏 + 中等摆动 + 细碎「价格噪声」
  const u = x * L.f * 6.0;
  return h * (L.y
    + L.a * Math.sin(u * 0.55 + t * L.s * 6.0 + L.p)
    + L.a * 0.45 * Math.sin(u * 1.35 - t * L.s * 4.2 + L.p * 1.7)
    + L.a * 0.12 * Math.sin(u * 6.5 + t * L.s * 9.0 + L.p * 2.3));
}

function draw(canvas, t) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
  const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  ctx.clearRect(0, 0, w, h);
  const pal = LOOP.colors || palette("dark");
  const step = Math.max(4, Math.round(6 * dpr));

  LINES.forEach((L, i) => {
    const [r, g, b] = pal[L.hue];
    const grad = ctx.createLinearGradient(0, 0, w, 0);
    const k = L.alpha * pal.gain;
    grad.addColorStop(0, `rgba(${r},${g},${b},0)`);
    grad.addColorStop(0.18, `rgba(${r},${g},${b},${k * 0.7})`);
    grad.addColorStop(0.5, `rgba(${r},${g},${b},${k})`);
    grad.addColorStop(0.82, `rgba(${r},${g},${b},${k * 0.7})`);
    grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
    ctx.beginPath();
    for (let x = 0; x <= w + step; x += step) {
      const y = lineY(L, x / w, t, h);
      if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    // 先画一道宽而淡的光，再画细线：比 shadowBlur 便宜得多
    ctx.strokeStyle = grad;
    ctx.globalAlpha = 0.14;
    ctx.lineWidth = L.w * 7 * dpr;
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.lineWidth = L.w * dpr;
    ctx.stroke();

    // 最亮那条线上跑两个光点，像在走势线上标出的数据点
    if (i === 2) {
      [0.27, 0.71].forEach((base, j) => {
        const px = ((base + t * 0.012 * (j ? 0.8 : 1)) % 1) * w;
        const py = lineY(L, px / w, t, h);
        const glow = ctx.createRadialGradient(px, py, 0, px, py, 14 * dpr);
        glow.addColorStop(0, `rgba(${r},${g},${b},${0.55 * pal.gain})`);
        glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
        ctx.fillStyle = glow;
        ctx.beginPath(); ctx.arc(px, py, 14 * dpr, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = `rgba(${r},${g},${b},${0.95 * pal.gain + 0.05})`;
        ctx.beginPath(); ctx.arc(px, py, 2.2 * dpr, 0, Math.PI * 2); ctx.fill();
      });
    }
  });
}

function stopLoop() {
  if (LOOP.raf) cancelAnimationFrame(LOOP.raf);
  LOOP.raf = 0;
  if (LOOP.observer) LOOP.observer.disconnect();
  LOOP.observer = null;
  LOOP.canvas = null;
}

function startLoop(canvas) {
  stopLoop();
  LOOP.canvas = canvas;
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) { draw(canvas, 8); return; }       // 静止画面
  const t0 = performance.now();
  const tick = (now) => {
    if (LOOP.canvas !== canvas) return;
    if (LOOP.visible && !document.hidden) draw(canvas, 8 + (now - t0) / 1000);
    LOOP.raf = requestAnimationFrame(tick);
  };
  // 滚出视野就停画（只算不画也省电）
  if ("IntersectionObserver" in window) {
    LOOP.observer = new IntersectionObserver((es) => { LOOP.visible = es.some((e) => e.isIntersecting); });
    LOOP.observer.observe(canvas);
  }
  LOOP.raf = requestAnimationFrame(tick);
}

export default function (component) {
  const { parentElement, data, setTriggerValue } = component;
  const d = data && typeof data === "object" ? data : {};
  const root = parentElement.querySelector("#lp");
  if (!root) return;

  const theme = d.theme === "light" ? "light" : "dark";
  root.setAttribute("data-theme", theme);
  LOOP.colors = palette(theme);

  root.querySelectorAll("[data-enter]").forEach((b) => {
    b.onclick = () => {
      b.disabled = true;
      setTriggerValue("enter", { at: Date.now() });
    };
  });
  root.querySelectorAll("[data-scroll]").forEach((b) => {
    b.onclick = () => {
      const target = root.querySelector("#" + b.getAttribute("data-scroll"));
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  });

  const canvas = root.querySelector("#lpBg");
  if (canvas && LOOP.canvas !== canvas) startLoop(canvas);
  else if (canvas && !LOOP.raf) draw(canvas, 8);   // 减少动画模式下换了主题，重画一次静止帧

  return () => stopLoop();
}
