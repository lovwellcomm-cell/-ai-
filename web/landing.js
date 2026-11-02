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
  // 跟随圆环（.lp-cursor）套在十字线交点上，hot 表示指针停在按钮上
  ring: { x: 0, y: 0, hot: false },
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
  const pull = Math.max(-48 * dpr, Math.min(48 * dpr, (my - y) * 0.42));
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
  drawCrosshair(ctx, w, h, t, dpr, pal);
  placeTags(dots, t);
}

/* 看盘十字光标：鼠标在走势线这片区域里时，画一横一竖两条金色细虚线（往边缘淡出），
   竖线和每条走势线相交处亮一个小点，像在行情图上读线。不标任何数字 —— 背景线是装饰，标了就是编数。
   强度跟着 mouse.k 渐入渐出；减少动画时根本不跑循环，也就不画。 */
function drawCrosshair(ctx, w, h, t, dpr, pal) {
  const m = LOOP.mouse;
  if (m.k < 0.02) return;
  const [r, g, b] = pal.a;
  const x = m.x * dpr, y = m.y * dpr, k = m.k;
  ctx.save();
  ctx.setLineDash([3 * dpr, 5 * dpr]);
  ctx.lineWidth = 1 * dpr;
  const v = ctx.createLinearGradient(0, y - h * 0.45, 0, y + h * 0.45);
  v.addColorStop(0, `rgba(${r},${g},${b},0)`);
  v.addColorStop(0.5, `rgba(${r},${g},${b},${0.55 * k * pal.gain})`);
  v.addColorStop(1, `rgba(${r},${g},${b},0)`);
  ctx.strokeStyle = v;
  ctx.beginPath(); ctx.moveTo(x, Math.max(0, y - h * 0.45)); ctx.lineTo(x, Math.min(h, y + h * 0.45)); ctx.stroke();
  const hz = ctx.createLinearGradient(x - w * 0.32, 0, x + w * 0.32, 0);
  hz.addColorStop(0, `rgba(${r},${g},${b},0)`);
  hz.addColorStop(0.5, `rgba(${r},${g},${b},${0.42 * k * pal.gain})`);
  hz.addColorStop(1, `rgba(${r},${g},${b},0)`);
  ctx.strokeStyle = hz;
  ctx.beginPath(); ctx.moveTo(Math.max(0, x - w * 0.32), y); ctx.lineTo(Math.min(w, x + w * 0.32), y); ctx.stroke();
  ctx.setLineDash([]);
  // 竖线与每条走势线的交点：只画离光标不太远的（太远的交点和光标没关系，满屏都是点会乱）
  LINES.forEach((L, i) => {
    const ly = lineY(L, x, w, t, h, dpr);
    const near = Math.exp(-((ly - y) ** 2) / (2 * (h * 0.22) ** 2));
    if (near < 0.08) return;
    const [lr, lg, lb] = pal[L.hue];
    const a = k * near * (i === MAIN ? 1 : 0.75);
    if (i === MAIN) {
      // 最亮那条线的交点带一圈呼吸光
      const pulse = 9 + 3 * Math.sin(t * 3.2);
      const glow = ctx.createRadialGradient(x, ly, 0, x, ly, pulse * dpr);
      glow.addColorStop(0, `rgba(${lr},${lg},${lb},${0.5 * a * pal.gain})`);
      glow.addColorStop(1, `rgba(${lr},${lg},${lb},0)`);
      ctx.fillStyle = glow;
      ctx.beginPath(); ctx.arc(x, ly, pulse * dpr, 0, Math.PI * 2); ctx.fill();
    }
    ctx.strokeStyle = `rgba(${lr},${lg},${lb},${0.9 * a * pal.gain + 0.05 * a})`;
    ctx.lineWidth = 1.2 * dpr;
    ctx.beginPath(); ctx.arc(x, ly, (i === MAIN ? 3.4 : 2.6) * dpr, 0, Math.PI * 2); ctx.stroke();
  });
  ctx.restore();
}

/* 跟随圆环和网格聚光：圆环套在十字线的交点上（用同一个缓动后的位置，跟随的「慢半拍」来自这里的缓动；
   原来圆环单独用更快的缓动，鼠标一动就和十字线分开了）。位置、强度写进 CSS 变量，由 CSS 画。 */
function updateCursor(root) {
  const m = LOOP.mouse, ring = LOOP.ring;
  ring.x = m.x;
  ring.y = m.y;
  root.style.setProperty("--cx", ring.x.toFixed(1) + "px");
  root.style.setProperty("--cy", ring.y.toFixed(1) + "px");
  root.style.setProperty("--ck", m.k.toFixed(3));
  root.classList.toggle("lp-hot", ring.hot);
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

/* ---------------- 截图随滚动立起来 ---------------- */

// 截图一开始向后仰、略小，滚到它进入视口下半部分时摆正。直接在动画循环里读位置，
// 不挂滚动监听：Streamlit 的滚动容器在组件外面（stMain），监听它要穿过 shadow root。
const TILT_MAX = 18;      // 度
let lastTilt = "";

function updateTilt(root) {
  // 量外层 .lp-shot（不倾斜）的位置：量 .lp-frame 的话，倾斜本身会改变它的包围盒，角度会自己来回抖
  const shot = root.querySelector(".lp-shot");
  if (!shot) return;
  const vh = window.innerHeight || 800;
  const top = shot.getBoundingClientRect().top;
  // 截图上沿从视口底部走到视口顶部附近（约 0.9 个屏高）的过程中逐渐摆正
  const p = Math.min(1, Math.max(0, (vh - top) / (vh * 0.9)));
  const e = 1 - (1 - p) ** 2;                          // 缓出：越接近摆正越慢
  const max = vh < 700 || window.innerWidth < 760 ? TILT_MAX * 0.55 : TILT_MAX;
  const tilt = (max * (1 - e)).toFixed(2) + "deg";
  if (tilt === lastTilt) return;
  lastTilt = tilt;
  root.style.setProperty("--tilt", tilt);
  root.style.setProperty("--sc", (0.94 + 0.06 * e).toFixed(4));
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
    updateTilt(root);
    updateCursor(root);
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

/* 首屏的「回答 → 原页」：从回答卡片底部「来源 · 第 7 页」那一行的右端画一条虚线到原页上标出的营业收入。
   （从正文里的引用编号出发会横穿正文，压在字上。）
   引线在倾斜的 .lp-stage 里面，用布局坐标（offsetLeft / offsetTop 链）算，不受滚动倾斜影响；
   原页自己转了 2°，终点按这个角度绕原页中心换算。手机上原页排在回答下面，不画。 */
const PAGE_ROT = 2 * Math.PI / 180;

function offsetIn(el, ancestor) {
  let x = 0, y = 0;
  for (let n = el; n && n !== ancestor; n = n.offsetParent) { x += n.offsetLeft; y += n.offsetTop; }
  return { x, y };
}

function drawStageLine(root) {
  const stage = root.querySelector(".lp-stage");
  const svg = root.querySelector(".lp-stage-line");
  if (!stage || !svg || window.innerWidth <= 760) return;
  const answer = stage.querySelector(".lp-answer");
  const src = stage.querySelector(".lp-a-src");
  const page = stage.querySelector(".lp-page");
  const mark = stage.querySelector(".lp-page-mark");
  if (!answer || !src || !page || !mark) return;
  const s = offsetIn(src, stage);
  const p = offsetIn(page, stage);
  const m = offsetIn(mark, page);
  // 标记左边缘中点，在原页里的坐标 → 绕原页中心转 2° → 加上原页的位置
  const cx = page.offsetWidth / 2, cy = page.offsetHeight / 2;
  const mx = m.x - 4 - cx, my = m.y + mark.offsetHeight / 2 - cy;
  const ex = p.x + cx + mx * Math.cos(PAGE_ROT) - my * Math.sin(PAGE_ROT);
  const ey = p.y + cy + mx * Math.sin(PAGE_ROT) + my * Math.cos(PAGE_ROT);
  const sx = answer.offsetLeft + answer.offsetWidth - 6, sy = s.y + src.offsetHeight / 2 + 6;
  svg.setAttribute("width", stage.offsetWidth);
  svg.setAttribute("height", stage.offsetHeight);
  svg.querySelector("path").setAttribute("d",
    `M${sx},${sy} C${sx + 70},${sy} ${ex - 60},${ey} ${ex},${ey}`);
  const dot = svg.querySelector("circle");
  dot.setAttribute("cx", ex);
  dot.setAttribute("cy", ey);
}

/* 「怎么用」：批注 ↔ 荧光笔。进入视野时荧光笔依次划过一次（减少动画时直接显示）；
   悬停 / 聚焦 / 点按一条批注，左边对应位置加深，桌面端再画一条引线。点按会固定住，再点一次放开。 */
const USE = { pinned: null, observer: null };

function wireUse(root) {
  const sheet = root.querySelector(".lp-sheet");
  const leader = root.querySelector(".lp-leader");
  if (!sheet || !leader) return;
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !("IntersectionObserver" in window)) {
    sheet.classList.add("drawn");
  } else if (!sheet.classList.contains("drawn")) {
    if (USE.observer) USE.observer.disconnect();
    USE.observer = new IntersectionObserver((es) => {
      if (es.some((e) => e.isIntersecting)) { sheet.classList.add("drawn"); USE.observer.disconnect(); }
    }, { threshold: 0.35 });
    USE.observer.observe(sheet);
  }
  const marks = [...sheet.querySelectorAll(".lp-mark")];
  const notes = [...root.querySelectorAll(".lp-note")];
  const path = leader.querySelector("path");
  const dot = leader.querySelector("circle");
  const wrap = leader.parentElement;
  const light = (note) => {
    const k = note && note.getAttribute("data-k");
    notes.forEach((n) => n.classList.toggle("on", n === note));
    marks.forEach((m) => m.classList.toggle("on", m.getAttribute("data-k") === k));
    const target = marks.find((m) => m.getAttribute("data-k") === k);
    if (!note || !target || window.innerWidth <= 760) { leader.classList.remove("on"); return; }
    const w = wrap.getBoundingClientRect(), t = target.getBoundingClientRect(), n = note.getBoundingClientRect();
    leader.setAttribute("width", Math.round(n.left - w.left + 10));
    leader.setAttribute("height", Math.round(Math.max(w.height, n.bottom - w.top)));
    const x1 = t.right - w.left + 4, y1 = t.top + t.height / 2 - w.top;
    const x2 = n.left - w.left - 6, y2 = n.top + 30 - w.top;
    path.setAttribute("d", `M${x1},${y1} C${x1 + 50},${y1} ${x2 - 50},${y2} ${x2},${y2}`);
    dot.setAttribute("cx", x1);
    dot.setAttribute("cy", y1);
    leader.classList.add("on");
  };
  notes.forEach((n) => {
    n.onmouseenter = () => light(n);
    n.onmouseleave = () => light(USE.pinned);
    n.onfocus = () => light(n);
    n.onclick = () => { USE.pinned = USE.pinned === n ? null : n; light(USE.pinned || n); };
  });
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

  // 「开始聊天」只进入助手；「这样问」进入助手并直接发出 data-ask 那句话（app.py 当成一次普通提问）
  root.querySelectorAll("[data-enter]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      b.disabled = true;
      const ask = b.getAttribute("data-ask");
      setTriggerValue("enter", ask ? { at: Date.now(), ask: ask } : { at: Date.now() });
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
    if (!LOOP.mouse.on) {
      LOOP.mouse.x = LOOP.ring.x = LOOP.mouse.tx;
      LOOP.mouse.y = LOOP.ring.y = LOOP.mouse.ty;
    }
    LOOP.mouse.on = e.clientY - r.top < r.height;   // 滚到网格以下就不再拉线
    // 指针停在按钮上：跟随圆环放大变淡，让位给按钮
    LOOP.ring.hot = !!(e.target && e.target.closest && e.target.closest("button"));
  } : null;
  root.onpointerleave = () => { LOOP.mouse.on = false; };

  wireUse(root);
  drawStageLine(root);
  // 字体加载完、窗口变宽变窄后排版会变，引线跟着重画
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => drawStageLine(root));
  window.onresize = () => drawStageLine(root);

  if (canvas && LOOP.canvas !== canvas) startLoop(canvas, root);
  else if (canvas && !LOOP.raf) draw(canvas, 8);   // 减少动画模式下换了主题，重画一次静止帧

  return () => stopLoop();
}
