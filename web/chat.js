/**
 * 年报 AI 研究助手 —— 对话面板组件前端
 *
 * 这个文件由 ui/chat_component.py 读取后作为 st.components.v2.component(js=...) 传入。
 * 判定「内联还是文件路径」靠启发式：含换行的字符串才会被当成内联代码，所以不要压缩成一行。
 *
 * 三条必须遵守的 Streamlit 约定（streamlit==1.66.0 实测）：
 *  1. default export 在每次 data 或主题变化时都会重跑，cleanup 只在卸载时跑一次。
 *     所以本文件必须幂等：整棵 UI 从 data 重建；事件一律用 el.onclick = 赋值，
 *     绝不用 addEventListener（会一层层叠加）。
 *  2. parentElement 是 ShadowRoot。不要写 parentElement.innerHTML，那会覆盖
 *     Streamlit 注入的 html/css；要新建子元素再设它的内容。
 *  3. setTriggerValue 只能在点击/输入等用户事件里调用。渲染路径里调用会导致
 *     每次 rerun 都触发，形成无限循环。
 *
 * 安全：组件内的 HTML 是不消毒的，而 data 里带着用户提问和模型输出。
 * 因此所有动态文本一律用 textContent / createTextNode 构建，绝不拼进 innerHTML。
 */

const ICONS = {
  mark: '<svg class="mark-sm" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="1" y="1" width="22" height="22" rx="7" stroke="currentColor" stroke-opacity=".3" stroke-width="1.3"/><rect x="6" y="13.2" width="3" height="4.8" rx="1.1" fill="currentColor" opacity=".42"/><rect x="10.5" y="10" width="3" height="8" rx="1.1" fill="currentColor" opacity=".68"/><rect x="15" y="6.4" width="3" height="11.6" rx="1.1" fill="currentColor"/></svg>',
  sun: '<circle cx="12" cy="12" r="4.2" stroke="currentColor" stroke-width="1.6"/><path d="M12 2.6v2.2M12 19.2v2.2M4.2 12H2M22 12h-2.2M6.4 6.4 4.9 4.9M19.1 19.1l-1.5-1.5M6.4 17.6l-1.5 1.5M19.1 4.9l-1.5 1.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  moon: '<path d="M20 13.5A8 8 0 0 1 10.5 4a8 8 0 1 0 9.5 9.5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
  shield: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 5 6v6c0 4.4 3 8.2 7 9 4-.8 7-4.6 7-9V6l-7-3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M9 9h10v10H9zM5 15V5h10" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 3v5h5" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
};

const HERO_SUGS = [
  { key: "rev",    tag: "财务摘要",   text: "营业收入和归母净利润分别是多少？" },
  { key: "margin", tag: "经营分析",   text: "毛利率同比变化的原因是什么？" },
  { key: "risk",   tag: "客户与风险", text: "前五大客户与供应商集中度如何？" },
  { key: "rd",     tag: "研发投入",   text: "研发投入的金额与资本化比例？" },
];

/* 拼进 innerHTML 的动态字符串一律先转义。
   放在模块作用域：mount() 里首次渲染消息时就会用到图表，写在函数体的靠后位置
   会落进暂时性死区（const 不提升），第一次渲染直接抛 ReferenceError。 */
const esc = (v) => String(v).replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

/* 每个组件实例的临时 UI 状态。模块按内容哈希缓存，所以这些状态在 rerun 之间能留下来。
   用 component.key 而不是 parentElement 做键：shadow root 万一被重建，状态也不会丢。 */
const S = new Map();
function localState(key) {
  let s = S.get(key);
  if (!s) {
    s = { draft: "", scrollTop: 0, atBottom: true, drawer: false, turnIdx: -1, srcIdx: 0, activeCite: null, collapsed: new Set(), msgsSig: null, drawerSig: null, themeOverride: null };
    S.set(key, s);
  }
  return s;
}

export default function (component) {
  const { parentElement, data, key, setStateValue, setTriggerValue } = component;
  const d = data && typeof data === "object" ? data : {};
  const s = localState(key || "chat");

  const q = (sel) => parentElement.querySelector(sel);
  const root = q("#arb");
  if (!root) return;

  const thread = q("#thread");
  const threadInner = q("#threadInner");
  const input = q("#input");
  const sendBtn = q("#sendBtn");
  const panel = q("#panel");
  const scrim = q("#scrim");

  /* ---------------- 小工具 ---------------- */
  const mk = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); };

  /* ---------------- 主题 ---------------- */
  const theme = s.themeOverride || d.theme || "light";
  root.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
  q("#themeIcon").innerHTML = theme === "dark" ? ICONS.sun : ICONS.moon;

  /* ---------------- 顶栏 ---------------- */
  const doc = d.doc || {};
  q("#ctxCo").textContent = [doc.company, doc.year].filter(Boolean).join(" ") || "未选择年报";
  q("#ctxMeta").textContent = doc.pages ? doc.pages + " 页 · " + doc.chunks + " 片段" : "";
  q("#docChipText").textContent = doc.name || "";
  // 窄屏放不下长占位符，换成短句，免得被截断
  const setPlaceholder = () => {
    const narrow = window.innerWidth <= 760;
    input.placeholder = (narrow && d.placeholderShort) ? d.placeholderShort : (d.placeholder || "");
  };
  setPlaceholder();
  window.onresize = setPlaceholder;

  const lastAi = (() => {
    const ms = d.messages || [];
    for (let i = ms.length - 1; i >= 0; i--) if (ms[i].role === "ai") return i;
    return -1;
  })();
  const srcTotal = lastAi >= 0 ? ((d.messages[lastAi].sources || []).length) : 0;
  q("#srcCnt").textContent = String(srcTotal);

  /* ---------------- 提示条 ---------------- */
  const warn = q("#warn");
  if (d.warn) { q("#warnText").textContent = d.warn; warn.hidden = false; }
  else { warn.hidden = true; }

  /* ---------------- 发送状态 ---------------- */
  const busy = !!d.busy;
  // 生成期间输入框保持可用：用户可以先把下一条问题写好，只锁发送按钮，
  // 这样既不会两轮并发，也不会让人觉得界面卡死了。
  const setSend = () => { sendBtn.disabled = busy || input.value.trim().length === 0; };
  input.disabled = false;
  setSend();
  q("#hintLeft").textContent = busy ? "正在生成回答，可以先写下一条问题" : (d.hint || "");

  /* ---------------- 对话流 ---------------- */
  const msgsSig = JSON.stringify(d.messages || []);
  if (s.msgsSig !== msgsSig) {
    clear(threadInner);
    const ms = d.messages || [];
    if (!ms.length) {
      threadInner.classList.add("empty");
      threadInner.appendChild(buildHero());
    } else {
      threadInner.classList.remove("empty");
      ms.forEach((m, i) => threadInner.appendChild(m.role === "user" ? buildUser(m) : buildAi(m, i)));
    }
    s.msgsSig = msgsSig;
    // 重建后恢复滚动位置：原本贴在底部就继续贴底
    requestAnimationFrame(() => {
      if (s.atBottom) thread.scrollTop = thread.scrollHeight;
      else thread.scrollTop = Math.min(s.scrollTop, thread.scrollHeight);
    });
  }
  // 正文重建后要把当前选中的引用角标高亮补回来（它是纯前端状态，不在 data 里）
  if (s.activeCite) markCite(s.activeCite);

  /* ---------------- 抽屉 ---------------- */
  panel.classList.toggle("open", s.drawer);
  panel.setAttribute("aria-hidden", s.drawer ? "false" : "true");
  scrim.hidden = !s.drawer;
  renderDrawer();

  /* ---------------- 事件（每次渲染重新赋值，幂等） ---------------- */
  thread.onscroll = () => {
    s.scrollTop = thread.scrollTop;
    s.atBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 40;
  };

  input.oninput = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 176) + "px";
    s.draft = input.value;
    setSend();
  };
  input.onkeydown = (e) => {
    // 中文输入法组字期间回车是在选词，不能当发送
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  };
  sendBtn.onclick = submit;

  q("#themeBtn").onclick = () => {
    s.themeOverride = theme === "dark" ? "light" : "dark";
    setStateValue("theme", s.themeOverride);
    root.setAttribute("data-theme", s.themeOverride);
    q("#themeIcon").innerHTML = s.themeOverride === "dark" ? ICONS.sun : ICONS.moon;
  };
  q("#srcBtn").onclick = () => { s.drawer = !s.drawer; toggleDrawer(); };
  q("#panelClose").onclick = () => { s.drawer = false; toggleDrawer(); };
  scrim.onclick = () => { s.drawer = false; toggleDrawer(); };
  q("#menuBtn").onclick = () => {
    // 主区的 Streamlit header 被 display:none 隐藏了，但里面的展开按钮仍在 DOM 里，
    // 程序化点击照样生效（已实测）。
    // 按侧边栏的真实宽度决定方向，而不是猜「哪个按钮存在」——
    // 实测 streamlit 1.66 展开状态下侧边栏里并没有 stSidebarCollapseButton，
    // 只靠按钮有无来判断会时灵时不灵。
    const sb = document.querySelector('[data-testid="stSidebar"]');
    const collapsed = !sb || sb.getBoundingClientRect().width < 40;
    const target = collapsed
      ? document.querySelector('[data-testid="stExpandSidebarButton"]')
      : (document.querySelector('[data-testid="stSidebarCollapseButton"] button')
         || document.querySelector('[data-testid="stSidebarCollapseButton"]'));
    if (target) target.click();
  };

  function toggleDrawer() {
    panel.classList.toggle("open", s.drawer);
    panel.setAttribute("aria-hidden", s.drawer ? "false" : "true");
    scrim.hidden = !s.drawer;
    renderDrawer();
  }

  function submit() {
    if (busy) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "auto";
    s.draft = "";
    setSend();
    setTriggerValue("ask", { text: text });
  }

  /* ============================================================
     首页
     ============================================================ */
  function buildHero() {
    const hero = mk("div", "hero");
    const head = mk("div", "hero-head");
    head.appendChild(mk("div", "eyebrow", d.heroEyebrow || "年报已建索引"));
    head.appendChild(mk("h1", null, d.heroTitle || "想从这份年报里知道什么？"));
    const p = mk("p");
    p.appendChild(document.createTextNode("答案只依据年报原文生成，每处论断都标有 "));
    p.appendChild(mk("b", null, "[n]"));
    p.appendChild(document.createTextNode(" 引用。点开引用可以读到原文片段，并翻到 "));
    p.appendChild(mk("b", null, "PDF 原页"));
    p.appendChild(document.createTextNode("核对。"));
    head.appendChild(p);
    hero.appendChild(head);

    const sugs = mk("div", "sugs");
    HERO_SUGS.forEach((x) => {
      const b = mk("button", "sug");
      b.type = "button";
      b.appendChild(mk("span", "sug-tag", x.tag));
      b.appendChild(mk("span", "sug-text", x.text));
      b.onclick = () => { if (!busy) setTriggerValue("ask", { text: x.text }); };
      sugs.appendChild(b);
    });
    hero.appendChild(sugs);

    if (d.heroFoot) {
      const f = mk("div", "hero-foot");
      f.innerHTML = ICONS.shield;
      f.appendChild(mk("span", null, d.heroFoot));
      hero.appendChild(f);
    }
    return hero;
  }

  /* ============================================================
     消息
     ============================================================ */
  function buildUser(m) {
    const wrap = mk("div", "msg msg-user");
    wrap.appendChild(mk("div", "bubble", m.text || ""));
    return wrap;
  }

  function buildAi(m, idx) {
    const wrap = mk("div", "msg msg-ai");
    const gut = mk("div", "msg-gutter");
    gut.innerHTML = ICONS.mark;
    wrap.appendChild(gut);

    const body = mk("div", "msg-body");
    const meta = mk("div", "msg-meta");
    meta.appendChild(mk("span", "who", "年报助手"));
    meta.appendChild(mk("span", "sep"));
    meta.appendChild(mk("span", "mono", d.model || ""));
    if (m.demo) meta.appendChild(mk("span", "tag", "演示回答"));
    body.appendChild(meta);
    wrap.appendChild(body);

    if (m.status === "retrieving" || m.status === "generating") {
      const th = mk("div", "thinking");
      th.appendChild(mk("i", "spin"));
      th.appendChild(mk("span", null, m.statusText || "检索相关片段…"));
      body.appendChild(th);
      return wrap;
    }

    if (m.status === "error") {
      const w = mk("div", "warn");
      w.appendChild(mk("span", null, m.error || "生成失败，请重试。"));
      body.appendChild(w);
      return wrap;
    }

    const prose = mk("div", "prose");
    String(m.text || "").split(/\n{2,}/).forEach((para) => {
      if (!para.trim()) return;
      const p = mk("p");
      renderRich(p, para, (m.sources || []).length);
      prose.appendChild(p);
    });
    body.appendChild(prose);

    if (m.kpis && m.kpis.length) body.appendChild(buildCard(m));
    if (m.queries && m.queries.length) body.appendChild(buildTrace(m));
    if (m.sources && m.sources.length) body.appendChild(buildSources(m, idx));
    body.appendChild(buildActions(m));
    return wrap;
  }

  /* 把 **粗体** 和 [n] 引用渲染成节点。全程 textContent，不碰 innerHTML。 */
  function renderRich(container, text, nSources) {
    const re = /\*\*([^*]+)\*\*|\[([\d,，、\s]+)\]/g;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) container.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1] != null) {
        container.appendChild(mk("strong", null, m[1]));
      } else {
        // 与 rag/generator.py:cited_numbers 同规则：只认 1..片段数，
        // 免得正文里的 [2024] 之类被误当成引用
        const nums = m[2].split(/[,，、\s]+/)
          .filter((x) => /^\d+$/.test(x))
          .map(Number)
          .filter((n) => n >= 1 && n <= nSources);
        if (nums.length) {
          [...new Set(nums)].sort((a, b) => a - b).forEach((n) => {
            const btn = mk("button", "cite", String(n));
            btn.type = "button";
            btn.setAttribute("aria-label", "查看引用 " + n);
            btn.dataset.n = String(n);
            btn.onclick = () => openSource(n);
            container.appendChild(btn);
          });
        } else {
          container.appendChild(document.createTextNode(m[0]));
        }
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) container.appendChild(document.createTextNode(text.slice(last)));
  }

  /* ---------------- 数据卡 ---------------- */
  function buildCard(m) {
    const card = mk("div", "card");
    const head = mk("div", "card-head");
    head.appendChild(mk("span", "card-title", d.cardTitle || "主要会计数据"));
    head.appendChild(mk("span", "tag", d.cardNote || "示例数据"));
    card.appendChild(head);

    const body = mk("div", "card-body");
    const kpis = mk("div", "kpis");
    m.kpis.forEach((k) => {
      const cell = mk("div", "kpi");
      cell.appendChild(mk("span", "kpi-k", k.k));
      const v = mk("span", "kpi-v", String(k.v));
      if (k.u) v.appendChild(mk("small", null, k.u));
      cell.appendChild(v);
      if (k.d) {
        const up = !!k.up;
        const good = k.invert ? !up : up;
        cell.appendChild(mk("span", "delta " + (good ? "up" : "down"), (up ? "▲ " : "▼ ") + k.d));
      }
      kpis.appendChild(cell);
    });
    body.appendChild(kpis);

    if (m.charts && m.charts.length) {
      const charts = mk("div", "charts");
      m.charts.forEach((c) => charts.appendChild(chartNode(c)));
      body.appendChild(charts);
    }
    if (m.table && m.table.rows && m.table.rows.length) body.appendChild(buildTable(m.table));
    card.appendChild(body);
    return card;
  }

  function buildTable(t) {
    const wrap = mk("div", "tbl-wrap");
    const table = mk("table", "tbl");
    const thead = mk("thead");
    const hr = mk("tr");
    (t.head || []).forEach((h) => hr.appendChild(mk("th", null, h)));
    thead.appendChild(hr);
    table.appendChild(thead);

    const maxv = Math.max(...t.rows.map((r) => Number(r.v) || 0), 1);
    const tbody = mk("tbody");
    t.rows.forEach((r) => {
      const tr = mk("tr");
      tr.appendChild(mk("td", null, r.name));
      const td = mk("td");
      const bar = mk("span", "bar");
      const fill = mk("i");
      fill.style.width = Math.max(2, (Number(r.v) || 0) / maxv * 46) + "px";
      bar.appendChild(fill);
      bar.appendChild(mk("span", null, Number(r.v).toFixed(1)));
      td.appendChild(bar);
      tr.appendChild(td);
      tr.appendChild(mk("td", "pct", Number(r.share).toFixed(1) + "%"));
      if (r.yoy) {
        const td2 = mk("td", "delta " + (r.up ? "up" : "down"));
        td2.style.fontSize = "11.5px";
        td2.textContent = (r.up ? "▲ " : "▼ ") + String(r.yoy).replace(/^[+-]/, "");
        tr.appendChild(td2);
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  /* ---------------- 迷你柱状图 ---------------- */
  function barPath(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    return "M" + x + "," + (y + h) + "L" + x + "," + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y +
           "L" + (x + w - r) + "," + y + "Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + r) +
           "L" + (x + w) + "," + (y + h) + "Z";
  }

  function chartNode(c) {
    const box = mk("div", "chart");
    const title = mk("div", "chart-t");
    const t1 = mk("span", null, c.title);
    t1.appendChild(mk("span", "mono", "　" + c.range + " · " + c.unit));
    title.appendChild(t1);
    box.appendChild(title);

    const W = 260, top = 18, base = 92, plot = base - top;
    const x0 = 16, x1 = 250, n = c.values.length, band = (x1 - x0) / n, bw = 26;
    const vals = c.values.map(Number);
    const max = Math.max(...vals, 1);
    let svg = '<svg viewBox="0 0 ' + W + ' 118" role="img" aria-label="' + esc(c.title) + ' 近四年走势">';
    svg += '<line x1="' + x0 + '" y1="' + top + '" x2="' + x1 + '" y2="' + top + '" stroke="var(--line)" stroke-width="1" stroke-dasharray="2 3"/>';
    svg += '<line x1="' + x0 + '" y1="' + base + '" x2="' + x1 + '" y2="' + base + '" stroke="var(--line)" stroke-width="1"/>';
    vals.forEach((v, i) => {
      const bx = x0 + i * band + (band - bw) / 2;
      const h = Math.max(3, v / max * plot);
      const by = base - h;
      svg += '<path d="' + barPath(bx, by, bw, h, 4) + '" fill="var(--mark)" opacity="' + (i === n - 1 ? 1 : .55) + '"/>';
      svg += '<text x="' + (bx + bw / 2) + '" y="' + (base + 15) + '" text-anchor="middle" font-family="var(--font-mono)" font-size="9.5" fill="var(--ink-3)">' + esc(c.labels[i]) + '</text>';
      if (i === n - 1) {
        svg += '<text x="' + (bx + bw / 2) + '" y="' + (by - 7) + '" text-anchor="middle" font-family="var(--font-mono)" font-size="10.5" font-weight="500" fill="var(--ink)">' + v.toFixed(1) + '</text>';
      }
      svg += '<rect class="hit" data-i="' + i + '" x="' + (x0 + i * band) + '" y="' + top + '" width="' + band + '" height="' + plot + '" fill="transparent" style="cursor:crosshair"/>';
    });
    svg += "</svg>";
    const holder = mk("div");
    holder.innerHTML = svg;                       // 只含数字与我方生成的属性，标签已 esc
    box.appendChild(holder.firstChild);

    const tip = mk("div", "tip");
    box.appendChild(tip);
    box.querySelectorAll(".hit").forEach((hit) => {
      hit.onmouseenter = () => {
        const i = Number(hit.dataset.i);
        const t = mk("span");
        t.appendChild(document.createTextNode(c.labels[i] + " "));
        t.appendChild(mk("b", null, vals[i].toFixed(1) + " " + c.unit));
        t.appendChild(document.createElement("br"));
        t.appendChild(document.createTextNode("同比 " + ((c.yoy && c.yoy[i]) || "—")));
        clear(tip);
        tip.appendChild(t);
        const cx = x0 + i * band + band / 2;
        const hh = Math.max(3, vals[i] / max * plot);
        tip.style.left = (cx / W * 100) + "%";
        tip.style.top = ((base - hh - 10) / 118 * 100) + "%";
        tip.classList.add("show");
      };
      hit.onmouseleave = () => tip.classList.remove("show");
    });
    return box;
  }

  /* ---------------- 检索轨迹 ---------------- */
  function buildTrace(m) {
    const det = mk("details", "trace");
    det.appendChild(mk("summary", null, "检索轨迹 · " + m.hits + " 个片段 · 2 路召回"));
    const tb = mk("div", "trace-body");
    (m.queries || []).forEach((qq) => {
      const line = mk("div", "trace-line");
      line.appendChild(document.createTextNode("改写检索词 "));
      line.appendChild(mk("b", null, qq));
      tb.appendChild(line);
    });
    const line2 = mk("div", "trace-line");
    line2.appendChild(document.createTextNode("向量检索 " + (d.embedModel || "bge-small-zh-v1.5") + " "));
    line2.appendChild(mk("b", null, "top 8"));
    line2.appendChild(document.createTextNode(" ｜ 关键词检索 BM25 "));
    line2.appendChild(mk("b", null, "top 8"));
    line2.appendChild(document.createTextNode(" ｜ RRF 融合"));
    tb.appendChild(line2);
    det.appendChild(tb);
    return det;
  }

  /* ---------------- 来源列表 ---------------- */
  function buildSources(m, idx) {
    const block = mk("div", "srcs");
    block.appendChild(mk("div", "srcs-h", "引用来源 " + m.sources.length + " 段"));
    m.sources.forEach((src) => {
      const b = mk("button", "src");
      b.type = "button";
      b.appendChild(mk("span", "src-n", String(src.n)));
      const mid = mk("span", "src-mid");
      mid.appendChild(mk("span", "src-path", src.section));
      if (src.sub) mid.appendChild(mk("span", "src-sub", src.sub));
      b.appendChild(mid);
      b.appendChild(mk("span", "src-pg", pageLabel(src)));
      b.onclick = () => openSource(src.n, idx);
      block.appendChild(b);
    });
    return block;
  }

  function buildActions(m) {
    const acts = mk("div", "acts");
    const copy = mk("button", "act");
    copy.type = "button";
    copy.innerHTML = ICONS.copy;
    copy.appendChild(mk("span", null, "复制回答"));
    copy.onclick = () => {
      const plain = String(m.text || "").replace(/\[[\d,，、\s]+\]/g, "");
      const done = () => {
        copy.querySelector("span").textContent = "已复制";
        setTimeout(() => { copy.querySelector("span").textContent = "复制回答"; }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(plain).then(done, () => fallbackCopy(plain, done));
      } else fallbackCopy(plain, done);
    };
    acts.appendChild(copy);
    return acts;
  }

  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (e) { /* 忽略 */ }
    ta.remove();
  }

  /* ============================================================
     引用抽屉
     ============================================================ */
  function sourceMsg() {
    const ms = d.messages || [];
    if (s.turnIdx >= 0 && ms[s.turnIdx] && ms[s.turnIdx].sources) return ms[s.turnIdx];
    for (let i = ms.length - 1; i >= 0; i--) if (ms[i].sources && ms[i].sources.length) return ms[i];
    return null;
  }

  function openSource(n, idx) {
    if (typeof idx === "number") s.turnIdx = idx;
    const m = sourceMsg();
    if (m) {
      const found = m.sources.findIndex((x) => x.n === n);
      s.srcIdx = found < 0 ? 0 : found;
    }
    s.activeCite = n;
    s.drawer = true;
    toggleDrawer();
    markCite(n);
  }

  function markCite(n) {
    parentElement.querySelectorAll(".cite").forEach((c) => {
      c.classList.toggle("on", c.dataset.n === String(n));
    });
  }

  function pageLabel(src) {
    return src.p1 === src.p2 ? "第 " + src.p1 + " 页" : "第 " + src.p1 + "–" + src.p2 + " 页";
  }

  function renderDrawer() {
    const body = q("#panelBody");
    const m = sourceMsg();
    const sig = JSON.stringify([s.drawer, s.turnIdx, s.srcIdx, m ? m.sources : null, m ? m.page : null,
                                [...s.collapsed]]);
    if (sig === s.drawerSig) return;
    s.drawerSig = sig;

    clear(body);
    if (!m || !m.sources || !m.sources.length) {
      q("#panelCount").textContent = "0 段";
      body.appendChild(mk("div", "panel-empty", "提问之后，回答里被引用的原文片段会出现在这里，可以逐条读到原文并翻看 PDF 原页。"));
      return;
    }
    q("#panelCount").textContent = m.sources.length + " 段";

    m.sources.forEach((src, i) => {
      const card = mk("div", "pcard" + (i === s.srcIdx ? " hi" : ""));
      const head = mk("div", "pcard-head");
      head.appendChild(mk("span", "src-n", String(src.n)));
      const mid = mk("div", "pcard-mid");
      mid.appendChild(mk("span", "pcard-path", src.section));
      if (src.sub) mid.appendChild(mk("span", "pcard-sub", src.sub));
      head.appendChild(mid);
      head.appendChild(mk("span", "pcard-pg", pageLabel(src)));
      card.appendChild(head);

      card.appendChild(mk("div", "pcard-txt", src.text));

      const foot = mk("div", "pcard-foot");
      const btn = mk("button", "ghost");
      btn.type = "button";
      btn.innerHTML = ICONS.doc;
      // 服务端下发的那一页才是「已渲染」的真相；s.collapsed 只记用户手动收起过哪些
      const rendered = !!(m.page && m.page.n === src.n);
      const shown = rendered && !s.collapsed.has(src.n);
      btn.appendChild(mk("span", null, shown ? "收起原页" : "查看原页"));
      btn.onclick = () => {
        if (shown) {
          s.collapsed.add(src.n);
          s.drawerSig = null;
          renderDrawer();
        } else {
          btn.disabled = true;
          btn.querySelector("span").textContent = "渲染中…";
          s.collapsed.delete(src.n);
          setTriggerValue("open_page", { n: src.n });   // 只在点击里触发
        }
      };
      foot.appendChild(btn);
      card.appendChild(foot);

      if (shown) {
        const pw = mk("div", "paper-wrap");
        const img = mk("img", "paper-img");
        img.src = m.page.png;
        img.alt = "原文第 " + src.p1 + " 页";
        pw.appendChild(img);
        pw.appendChild(mk("div", "paper-cap", "原文第 " + src.p1 + " 页 · " + (d.doc && d.doc.name ? d.doc.name : "")));
        card.appendChild(pw);
      }

      card.onclick = (e) => {
        if (e.target.closest(".ghost") || e.target.closest(".paper-wrap")) return;
        s.srcIdx = i;
        s.drawerSig = null;
        renderDrawer();
      };
      body.appendChild(card);
    });
  }
}
