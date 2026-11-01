/**
 * 开屏页组件前端。
 *
 * 和 chat.js 同样的 Streamlit 约定（streamlit==1.66.0）：
 *  1. default export 每次 data 变化都会重跑 —— 事件一律用 el.onxxx = 赋值，保持幂等；
 *     背景动画只起一个，记在模块级 LOOP 里，重跑时只更新配色。
 *  2. parentElement 是 ShadowRoot。
 *  3. setTriggerValue 只在点击里调用（「开始聊天」）。
 *
 * 背景分层（从后往前）：透视网格（CSS）→ 流动走势线（这里的 Canvas）→ 数据标签（DOM）→ 细噪点（CSS）。
 * 走势线的构思参考 React Bits 的 Floating Lines，代码是这里重写的 Canvas 2D 版本，没有依赖。
 */

const LOOP = {
  canvas: null, root: null, raf: 0, colors: null, observer: null, visible: true,
  // 鼠标：目标位置和缓动后的位置（画布 CSS 像素），on 表示指针在页面上
  mouse: { x: 0, y: 0, tx: 0, ty: 0, k: 0, on: false },
  tags: null,
};

// 每条线的形状参数。写死而不是随机：每次打开看到的是同一幅画面，截图也稳定。
const LINES = [
  { y: 0.50, a: 0.040, f: 1.6, s: 0.10, p: 0.0, w: 0.9, hue: "a", alpha: 0.28 },
  { y: 0.56, a: 0.050, f: 1.0, s: 0.09, p: 2.1, w: 1.4, hue: "a", alpha: 0.85 },
  { y: 0.62, a: 0.060, f: 1.2, s: 0.07, p: 1.3, w: 1.0, hue: "a", alpha: 0.42 },
  { y: 0.66, a: 0.050, f: 1.4, s: 0.06, p: 3.7, w: 1.0, hue: "b", alpha: 0.50 },
  { y: 0.71, a: 0.055, f: 0.9, s: 0.08, p: 4.4, w: 1.1, hue: "a", alpha: 0.55 },
  { y: 0.76, a: 0.045, f: 1.8, s: 0.11, p: 5.2, w: 0.9, hue: "a", alpha: 0.32 },
  { y: 0.81, a: 0.040, f: 1.1, s: 0.05, p: 0.7, w: 0.8, hue: "c", alpha: 0.22 },
];
// 最亮的那条线：光点和数据标签都挂在它上面。选靠上的一条，标签才不会压到截图框
const MAIN = 1;
const DOTS = [0.12, 0.62];      // 两个光点的起始位置（横向比例）

// 数据标签：精达股份 2025 年报第 7 页「主要会计数据」，程序抽取并用披露增减复算过的真实数字，
// 和首屏截图里的回答是同一份报告。不许换成编的数。
const FACTS = [
  ["营业收入", "246.74 亿元", "+10.53%"],
  ["归母净利润", "5.99 亿元", "+6.66%"],
  ["扣非净利润", "5.70 亿元", "+11.34%"],
  ["经营现金流净额", "24.75 亿元", "+520.11%"],
  ["利润总额", "8.06 亿元", "+9.39%"],
  ["总资产", "144.67 亿元", "+14.58%"],
];
const FACT_SECONDS = 5;         // 每条标签停留多久

function palette(theme) {
  return theme === "dark"
    ? { a: [224, 178, 78], b: [214, 88, 76], c: [200, 205, 212], gain: 1.0 }
    : { a: [150, 110, 14], b: [196, 70, 60], c: [60, 64, 70], gain: 0.62 };
}

function baseY(L, x, t, h) {
  // 三层正弦：大起伏 + 中等摆动 + 细碎「价格噪声」
  const u = x * L.f * 6.0;
  return h * (L.y
    + L.a * Math.sin(u * 0.55 + t * L.s * 6.0 + L.p)
    + L.a * 0.45 * Math.sin(u * 1.35 - t * L.s * 4.2 + L.p * 1.7)
    + L.a * 0.12 * Math.sin(u * 6.5 + t * L.s * 9.0 + L.p * 2.3));
}

// 鼠标附近的线轻轻弯向光标（高斯衰减，最多挪 36px），离得远的线几乎不动
function lineY(L, xPx, w, t, h, dpr) {
  const y = baseY(L, xPx / w, t, h);
  const m = LOOP.mouse;
  if (m.k < 0.01) return y;
  const mx = m.x * dpr, my = m.y * dpr;
  const sx = w * 0.14, sy = h * 0.16;
  const g = Math.exp(-((xPx - mx) ** 2) / (2 * sx * sx)) * Math.exp(-((y - my) ** 2) / (2 * sy * sy));
  const pull = Math.max(-36 * dpr, Math.min(36 * dpr, (my - y) * 0.35));
  return y + pull * g * m.k;
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
  const dots = [];

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
      const y = lineY(L, x, w, t, h, dpr);
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

    if (i !== MAIN) return;
    // 最亮那条线上跑两个光点，数据标签就挂在光点上
    DOTS.forEach((base, j) => {
      const fx = (base + t * 0.010 * (j ? 0.85 : 1)) % 1;
      const px = fx * w;
      const py = lineY(L, px, w, t, h, dpr);
      dots.push({ fx, x: px / dpr, y: py / dpr });
      const glow = ctx.createRadialGradient(px, py, 0, px, py, 14 * dpr);
      glow.addColorStop(0, `rgba(${r},${g},${b},${0.55 * pal.gain})`);
      glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
      ctx.fillStyle = glow;
      ctx.beginPath(); ctx.arc(px, py, 14 * dpr, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = `rgba(${r},${g},${b},${0.95 * pal.gain + 0.05})`;
      ctx.beginPath(); ctx.arc(px, py, 2.2 * dpr, 0, Math.PI * 2); ctx.fill();
    });
  });
  placeTags(dots, t);
}

/* ---------------- 数据标签 ---------------- */

function fillTag(el, fact) {
  const [label, value, change] = fact;
  el.querySelector(".lp-dt-k").textContent = label;
  el.querySelector(".lp-dt-v").textContent = value;
  el.querySelector(".lp-dt-c").textContent = "同比 " + change;
}

function placeTags(dots, t) {
  const tags = LOOP.tags;
  if (!tags || !tags.length) return;
  dots.forEach((d, j) => {
    const el = tags[j];
    if (!el) return;
    // 只在两侧出现：标签（约 250px 宽）整个落在按钮区（中间约 350px）之外、也不出画面时才显示
    const W = LOOP.canvas ? LOOP.canvas.clientWidth : 1280;
    const half = 130, mid = W / 2, keep = 175;    // keep：按钮区半宽 150 + 余量
    const show = d.x - half > 12 && d.x + half < W - 12 &&
                 (d.x + half < mid - keep || d.x - half > mid + keep);
    // 每 FACT_SECONDS 秒换一条；两个标签错开半拍，不会同时换
    const slot = Math.floor((t + j * FACT_SECONDS * 0.5) / FACT_SECONDS);
    const idx = (slot * 2 + j) % FACTS.length;
    if (el._idx !== idx) {
      el._idx = idx;
      el.classList.remove("on");                 // 先淡出，换完内容下一帧再淡入
      el._swapAt = performance.now() + 260;
    }
    if (el._swapAt && performance.now() >= el._swapAt) {
      fillTag(el, FACTS[idx]);
      el._swapAt = 0;
    }
    el.classList.toggle("on", show && !el._swapAt);
    el.style.transform = `translate(${d.x.toFixed(1)}px, ${d.y.toFixed(1)}px)`;
  });
}

/* ---------------- 动画循环 ---------------- */

function stopLoop() {
  if (LOOP.raf) cancelAnimationFrame(LOOP.raf);
  LOOP.raf = 0;
  if (LOOP.observer) LOOP.observer.disconnect();
  LOOP.observer = null;
  LOOP.canvas = null;
}

function startLoop(canvas, root) {
  stopLoop();
  LOOP.canvas = canvas;
  LOOP.root = root;
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) { draw(canvas, 8); return; }       // 静止画面
  const t0 = performance.now();
  const tick = (now) => {
    if (LOOP.canvas !== canvas) return;
    const m = LOOP.mouse;
    // 鼠标缓动：位置追目标，强度在进出页面时渐变，避免线条突然一跳
    m.x += (m.tx - m.x) * 0.08;
    m.y += (m.ty - m.y) * 0.08;
    m.k += ((m.on ? 1 : 0) - m.k) * 0.05;
    if (LOOP.visible && !document.hidden) {
      draw(canvas, 8 + (now - t0) / 1000);
      // 光晕跟着鼠标轻微偏移（-1..1），金色和红色方向相反，做出前后层次
      const r = canvas.getBoundingClientRect();
      const px = r.width ? ((m.x / r.width) * 2 - 1) * m.k : 0;
      const py = r.height ? ((m.y / r.height) * 2 - 1) * m.k : 0;
      root.style.setProperty("--px", px.toFixed(3));
      root.style.setProperty("--py", py.toFixed(3));
    }
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
  LOOP.tags = [...root.querySelectorAll(".lp-dt")];

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
  // 鼠标视差只给精确指针（鼠标、触控板）；触屏上手指一按就跳，不做
  const fine = window.matchMedia && window.matchMedia("(pointer: fine)").matches;
  root.onpointermove = fine && canvas ? (e) => {
    const r = canvas.getBoundingClientRect();
    LOOP.mouse.tx = e.clientX - r.left;
    LOOP.mouse.ty = e.clientY - r.top;
    if (!LOOP.mouse.on) { LOOP.mouse.x = LOOP.mouse.tx; LOOP.mouse.y = LOOP.mouse.ty; }
    LOOP.mouse.on = e.clientY - r.top < r.height;   // 滚到网格以下就不再拉线
  } : null;
  root.onpointerleave = () => { LOOP.mouse.on = false; };

  if (canvas && LOOP.canvas !== canvas) startLoop(canvas, root);
  else if (canvas && !LOOP.raf) draw(canvas, 8);   // 减少动画模式下换了主题，重画一次静止帧

  return () => stopLoop();
}
