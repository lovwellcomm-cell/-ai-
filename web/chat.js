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
  pulse: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3 12h4l2.5-6 5 12 2.5-6H21" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 3v5h5" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
};

// 正在渲染的那条消息的网页来源（[网n] → 链接）。放在模块顶层，理由同 TABLE_LINE：
// 组件函数体里的 let 会在渲染路径先用到时撞上暂时性死区。
let WEB_LIST = [];
let CHART_SEQ = 0;           // 图表渐变的 id 序号：同一页多张图不能共用 id
// 读秒：一个模块级计时器，每秒刷新对话流里所有「已思考 n 秒」。服务器阻塞（检索、生成）时
// 组件不会重渲染，但浏览器里的计时器照走 —— 读秒的意义正在于此。
let TICKER = 0;
let TICK_ROOT = null;
const THINK_T0 = new Map();  // 文档|消息序号 → 读秒起点（本地时钟）；换阶段重建节点时沿用，不倒退

function fmtSecs(n) {
  n = Math.max(0, Math.floor(Number(n) || 0));
  return n < 60 ? n + " 秒" : Math.floor(n / 60) + " 分 " + String(n % 60).padStart(2, "0") + " 秒";
}

function tick() {
  if (!TICK_ROOT || !TICK_ROOT.isConnected) return;
  TICK_ROOT.querySelectorAll(".thinking .secs").forEach((el) => {
    el.textContent = fmtSecs((Date.now() - Number(el.dataset.t0)) / 1000);
  });
}

// 对话存档（ui/memory.py 定形状）：localStorage 里一个键，{convs: {对话键: [条目…]}, last: 文档id}。
// 只存在访客自己的浏览器里；服务器每次历史有变化就把当前公司那段送来（d.persist）。
const STORE_KEY = "arb.chat.v1";
const STORE_MAX_CONVS = 20;
let RESTORE_AT = 0;          // 上次把存档送回服务器的时间：渲染路径里触发，必须限流

function readStore() {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    const obj = raw ? JSON.parse(raw) : null;
    return obj && typeof obj === "object" && obj.convs && typeof obj.convs === "object"
      ? obj : { convs: {}, last: "" };
  } catch (e) { return { convs: {}, last: "" }; }
}

function writeStore(obj) {
  // 存不下（隐私模式、配额满）就从最久没动过的公司开始丢，丢到存得下为止
  for (let i = 0; i < STORE_MAX_CONVS + 1; i++) {
    try { window.localStorage.setItem(STORE_KEY, JSON.stringify(obj)); return; }
    catch (e) {
      const keys = Object.keys(obj.convs);
      if (!keys.length) return;
      delete obj.convs[keys[0]];
    }
  }
}

function syncStore(d, setTriggerValue) {
  if (d.restoreWanted) {
    // 刷新后的第一次渲染：把存档送回去。只送一次（服务器收到就不再要），限流防止重跑叠加。
    if (Date.now() - RESTORE_AT > 5000) {
      RESTORE_AT = Date.now();
      const obj = readStore();
      setTriggerValue("restore", { convs: obj.convs, last: obj.last || "" });
    }
    return;                                   // 恢复完成之前不写，免得空历史盖掉存档
  }
  const p = d.persist;
  const lastChanged = d.docId && readStore().last !== d.docId;
  if (!(p && typeof p.key === "string") && !lastChanged) return;
  const obj = readStore();
  if (p && typeof p.key === "string") {
    delete obj.convs[p.key];                  // 删了再加：键的顺序就是「最近用过」的顺序
    if (Array.isArray(p.entries) && p.entries.length) obj.convs[p.key] = p.entries;
    const keys = Object.keys(obj.convs);
    keys.slice(0, Math.max(0, keys.length - STORE_MAX_CONVS)).forEach((k) => delete obj.convs[k]);
  }
  if (d.docId) obj.last = d.docId;
  writeStore(obj);
}

const HERO_SUGS = [
  { key: "rev",    tag: "财务摘要",   text: "营业收入和归母净利润分别是多少？" },
  { key: "margin", tag: "经营分析",   text: "毛利率同比变化的原因是什么？" },
  { key: "risk",   tag: "客户与风险", text: "前五大客户与供应商集中度如何？" },
  { key: "rd",     tag: "研发投入",   text: "研发投入的金额与资本化比例？" },
  // 教用户用“自动找年报”：点了会去巨潮导入七匹狼，再基于它的年报回答
  { key: "switch", tag: "换家公司",   text: "七匹狼利润率变化如何？" },
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
    s = { draft: "", scrollTop: 0, atBottom: true, pinned: false, revealed: null, msgCount: 0, drawer: false, turnIdx: -1, srcIdx: 0, activeCite: null, collapsed: new Set(), srcOpen: new Set(), headSig: null, msgSigs: [], drawerSig: null, themeOverride: null };
    S.set(key, s);
  }
  return s;
}

// 回答正文里的 Markdown 表格：表格行与「|---|---|」分隔行（见 appendProse）
const TABLE_LINE = /^\s*\|.*\|\s*$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

export default function (component) {
  const { parentElement, data, key, setStateValue, setTriggerValue } = component;
  const d = data && typeof data === "object" ? data : {};
  const s = localState(key || "chat");

  const q = (sel) => parentElement.querySelector(sel);
  const root = q("#arb");
  if (!root) return;
  syncStore(d, setTriggerValue);

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
  const ctxSrc = q("#ctxSrc");
  ctxSrc.textContent = doc.source || "";
  ctxSrc.hidden = !doc.source;
  q("#ctxMeta").textContent = doc.pages ? doc.pages + " 页 · " + doc.chunks + " 片段" : "";

  /* 顶栏下拉：这家公司已导入的报告，按年份分组。侧边栏只列公司，年份和报告期在这里切。
     切换只换「依据哪份报告」，对话不断 —— 同一家公司共用一段对话。 */
  const ctxBtn = q("#ctxBtn");
  const ctxPop = q("#ctxPop");
  const reports = Array.isArray(d.reports) ? d.reports : [];
  const nReports = reports.reduce((n, g) => n + ((g.items || []).length), 0);
  const setPop = (open) => {
    ctxPop.hidden = !open;
    ctxBtn.setAttribute("aria-expanded", open ? "true" : "false");
    ctxBtn.classList.toggle("open", open);
  };
  ctxBtn.classList.toggle("has-menu", nReports > 0);
  ctxBtn.disabled = nReports === 0;
  clear(ctxPop);
  if (nReports) {
    const head = mk("div", "pop-head");
    head.appendChild(mk("span", "pop-title", doc.company || "当前公司"));
    head.appendChild(mk("span", "pop-sub", "已导入 " + nReports + " 份报告"));
    ctxPop.appendChild(head);
    reports.forEach((g) => {
      const row = mk("div", "pop-row");
      row.appendChild(mk("span", "pop-year", String(g.year || "")));
      const chips = mk("div", "pop-chips");
      (g.items || []).forEach((it) => {
        const b = mk("button", "pop-chip" + (it.current ? " on" : ""), it.label);
        b.type = "button";
        b.setAttribute("role", "menuitemradio");
        b.setAttribute("aria-checked", it.current ? "true" : "false");
        // 生成中不让切：回合挂在当前文档上，中途换文档会把进行中的回答丢掉
        b.disabled = !!d.busy && !it.current;
        b.onclick = () => {
          setPop(false);
          if (!it.current) setTriggerValue("pick_doc", { id: it.id, at: Date.now() });
        };
        chips.appendChild(b);
      });
      row.appendChild(chips);
      ctxPop.appendChild(row);
    });
    ctxPop.appendChild(mk("div", "pop-foot", "要别的年份或季度，直接在对话里问，例如「去年三季度怎么样」"));
  } else {
    setPop(false);
  }
  ctxBtn.onclick = (e) => { e.stopPropagation(); setPop(ctxPop.hidden); };
  // 点下拉以外的地方、按 Esc 收起。监听只挂一次（组件每次渲染都会重跑这个函数）。
  if (!root._ctxPopBound) {
    root._ctxPopBound = true;
    root.addEventListener("click", (e) => {
      if (!ctxPop.hidden && !e.target.closest(".ctx-wrap")) setPop(false);
    });
    root.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !ctxPop.hidden) { setPop(false); ctxBtn.focus(); }
    });
  }
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
  // 生成中不允许开新对话：中途清空会让上一轮的状态没地方落
  q("#newChatBtn").disabled = busy;
  q("#panelNewChat").disabled = busy;
  q("#hintLeft").textContent = busy ? "正在生成回答，可以先写下一条问题" : (d.hint || "");

  /* ---------------- 对话流 ---------------- */
  // 首页文案也跟着文档走：两份年报的对话历史都为空时，只比较 messages 会让
  // 切换文档后的 hero 停留在上一份年报（实测导入茅台后仍显示精达）。
  // 逐条比对、只重建变了的那条消息。
  // 以前是签名一变就清空整个对话流再全部重建：清空的那一刻滚动位置归零、下一帧才恢复，
  // 检索 → 生成 → 扩展每换一个阶段闪一次，点「查看原页」也闪（原页图片挂在最后一条消息上）。
  // 消息签名里去掉 page：原页只在右侧抽屉里显示，正文不用它，它变了不该动正文。
  const ms = d.messages || [];
  const headSig = JSON.stringify([d.doc, d.heroEyebrow, d.heroFoot, ms.length === 0]);
  // 「行业动态」按钮只挂在最后一条回答上、生成中不显示：位置和忙闲也算进签名，
  // 否则发出新问题后，上一条回答不重建，按钮会留在它下面
  // elapsed 每次 rerun 都变，但读秒由前端自己往上数，不该为它重建消息
  const msgSig = (m, i) => JSON.stringify([Object.assign({}, m, { page: undefined, elapsed: undefined }),
    i === ms.length - 1 ? (d.busy ? "last-busy" : "last") : ""]);
  let changed = false;
  // 重建前是否已经显示着消息（而不是空对话首页）
  const hadMsgs = !threadInner.classList.contains("empty") && threadInner.children.length > 0;
  if (s.headSig !== headSig) {
    // 换了文档、或者在「空对话首页」和「有消息」之间切换：整体重建
    clear(threadInner);
    s.msgSigs = [];
    s.headSig = headSig;
    changed = true;
    if (!ms.length) {
      // 新对话：消息序号从头开始，旧的「哪些来源列表展开过」不能带进来
      s.srcOpen.clear();
      threadInner.classList.add("empty");
      threadInner.appendChild(buildHero());
    } else {
      threadInner.classList.remove("empty");
    }
  }
  // 整体重建、而且之前就显示着消息（换文档、切报告期）：这一批不算「新消息」，不播动画。
  // 从空对话首页进到第一条消息时照常播。
  const fresh = changed && hadMsgs && ms.length > 0;
  if (ms.length) {
    const sigs = s.msgSigs || [];
    ms.forEach((m, i) => {
      const sig = msgSig(m, i);
      if (sigs[i] === sig && threadInner.children[i]) return;
      const node = m.role === "user" ? buildUser(m) : buildAi(m, i);
      const old = threadInner.children[i];
      // 只有真正新出现的消息播入场动画。原地更新（检索 → 生成 → 扩展换阶段）、
      // 以及换文档后的整体重建都不播 —— 每条消息都重新淡入一遍，看起来就是整页在闪。
      if (old || fresh) node.classList.add("settled");
      if (old) threadInner.replaceChild(node, old); else threadInner.appendChild(node);
      sigs[i] = sig;
      changed = true;
    });
    while (threadInner.children.length > ms.length) {
      threadInner.removeChild(threadInner.lastChild);
      changed = true;
    }
    sigs.length = ms.length;
    s.msgSigs = sigs;
  }
  if (changed) {
    // 滚动规则：
    //  · 发出新问题（消息变多、最后一条还没有正文）：滚到底，看得到自己的问题和读秒；
    //  · 回答正文第一次出现：把这一问对齐到顶部，从头读 —— 以前是贴底，长回答一出来
    //    就落在结尾。只在用户原本跟着看（贴底）时这样做，自己往上翻了就不动；
    //  · 之后的阶段（扩展分析补段落、指标卡出现）原地不动，直到下一个问题。
    const last = ms.length - 1;
    const lm = ms[last];
    const grew = ms.length > s.msgCount;
    const rk = (d.docId || "") + "|" + last;
    let reveal = null;
    if (lm && lm.role === "ai" && lm.text && s.revealed !== rk) {
      s.revealed = rk;
      if (s.atBottom || grew) reveal = threadInner.children[last - 1] || threadInner.children[last];
      s.pinned = true;
    } else if (grew) {
      s.pinned = false;
      s.atBottom = true;
    }
    const stick = () => {
      if (s.atBottom && !s.pinned) thread.scrollTop = thread.scrollHeight;
      else thread.scrollTop = Math.min(s.scrollTop, thread.scrollHeight);
    };
    if (reveal) {
      // 当场先保持原位（不跳），排版完成后再平滑滚到这一问的顶部
      thread.scrollTop = Math.min(s.scrollTop, thread.scrollHeight);
      requestAnimationFrame(() => {
        const top = thread.scrollTop + reveal.getBoundingClientRect().top - thread.getBoundingClientRect().top - 16;
        const still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        thread.scrollTo({ top: Math.max(0, top), behavior: still ? "auto" : "smooth" });
      });
    } else {
      // 当场设一次（不留一帧空档），图片和字体撑高后下一帧再补一次
      stick();
      requestAnimationFrame(stick);
    }
  }
  s.msgCount = ms.length;
  TICK_ROOT = threadInner;
  if (!TICKER) TICKER = setInterval(tick, 1000);
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

  function newChat() {
    if (busy) return;
    s.drawer = false;
    toggleDrawer();
    input.value = "";
    input.style.height = "auto";
    s.draft = "";
    setSend();
    setTriggerValue("new_chat", { at: Date.now() });
  }
  q("#newChatBtn").onclick = newChat;
  q("#panelNewChat").onclick = newChat;

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
    p.appendChild(document.createTextNode(" 也可以直接问别的公司，助手会先去找它的年报。"));
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
    // 同一公司多个年度共用一段对话时，标出这条回答依据的是哪份年报（只在跨年报时下发）
    if (m.docLabel) meta.appendChild(mk("span", "tag doc-tag", m.docLabel));
    if (typeof m.secs === "number") {
      meta.appendChild(mk("span", "sep"));
      meta.appendChild(mk("span", "mono took", "用时 " + fmtSecs(m.secs)));
    }
    body.appendChild(meta);
    if (m.notice) body.appendChild(mk("div", "notice", m.notice));
    wrap.appendChild(body);

    // 待定状态：检索和生成阶段还没有正文，只显示 spinner。
    // 但「扩展分析」阶段首轮回答已经产出，先把正文画出来再挂指示 ——
    // 用户不必等第二次模型调用跑完才看到东西。
    if (m.status === "resolving" || m.status === "retrieving" || m.status === "history" || m.status === "generating" || m.status === "analyzing" || m.status === "searching") {
      WEB_LIST = m.webSources || [];
      if (m.text) appendProse(body, m.text, (m.sources || []).length);
      const th = mk("div", "thinking");
      th.appendChild(mk("i", "spin"));
      th.appendChild(mk("span", null, m.statusText || "检索相关片段…"));
      // 起点 = 现在 − 服务器说的已用秒数；同一条消息换阶段时取更早的那个，读秒不会倒退
      const key = (d.docId || "") + "|" + idx;
      const t0 = Math.min(THINK_T0.get(key) || Infinity, Date.now() - (Number(m.elapsed) || 0) * 1000);
      THINK_T0.set(key, t0);
      const secs = mk("span", "secs", fmtSecs((Date.now() - t0) / 1000));
      secs.dataset.t0 = String(t0);
      th.appendChild(secs);
      body.appendChild(th);
      return wrap;
    }

    if (m.status === "error") {
      const w = mk("div", "warn");
      w.appendChild(mk("span", null, m.error || "生成失败，请重试。"));
      body.appendChild(w);
      return wrap;
    }

    WEB_LIST = m.webSources || [];
    appendProse(body, m.text, (m.sources || []).length, m.tableCheck);

    if (m.kpis && m.kpis.length) body.appendChild(buildCard(m));
    if (m.queries && m.queries.length) body.appendChild(buildTrace(m));
    if (m.sources && m.sources.length) body.appendChild(buildSources(m, idx));
    if (m.webSources && m.webSources.length) body.appendChild(buildWebSources(m, idx));
    body.appendChild(buildActions(m, idx));
    return wrap;
  }

  /* 把 **粗体** 和 [n] 引用渲染成节点。全程 textContent，不碰 innerHTML。 */
  function renderRich(container, text, nSources) {
    const re = /\*\*([^*]+)\*\*|\[([\d,，、\s]+)\]|\[网\s*(\d+)\]/g;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) container.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1] != null) {
        container.appendChild(mk("strong", null, m[1]));
      } else if (m[3] != null) {
        // 网页来源：可点的小标签，新窗口打开；编号对不上或地址不是 http(s) 就原样显示文字
        const src = WEB_LIST[Number(m[3]) - 1];
        const url = src ? String(src.url || "") : "";
        if (/^https?:\/\//i.test(url)) {
          const a = mk("a", "cite web-cite", "网" + m[3]);
          a.href = url;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.title = [src.title, src.site, src.date].filter(Boolean).join(" · ");
          container.appendChild(a);
        } else {
          container.appendChild(document.createTextNode(m[0]));
        }
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

  /** 把一段回答正文按空行拆成段落渲染。生成完成和「扩展分析中」两条路径共用。 */
  /* 正文：段落 + Markdown 表格。表格行以「|」开头，模型不一定在表格前后空一行，
     所以按行切分成「文字段 / 表格段」，而不是只按空行分段。
     check 是后端对表格的逐格核对（rag/fidelity.py:unverified_in_tables）。
     TABLE_LINE / TABLE_SEP 定义在模块顶层：组件函数体是从上往下执行的，渲染发生在这里之前，
     写在这里的 const 会撞上暂时性死区（实测：整条助手消息渲染失败）。 */
  function appendProse(body, text, nSources, check) {
    const prose = mk("div", "prose");
    const bad = (check && check.unverified) || [];
    let lines = [], inTable = false, nTables = 0;
    const flush = () => {
      if (!lines.length) return;
      if (inTable) {
        prose.appendChild(buildMdTable(lines, nSources, bad));
        nTables += 1;
      } else {
        lines.join("\n").split(/\n{2,}/).forEach((para) => {
          if (!para.trim()) return;
          const p = mk("p");
          renderRich(p, para, nSources);
          prose.appendChild(p);
        });
      }
      lines = [];
    };
    String(text || "").split("\n").forEach((ln) => {
      const isRow = TABLE_LINE.test(ln);
      if (isRow !== inTable) { flush(); inTable = isRow; }
      lines.push(ln);
    });
    flush();
    // 核对结论：所有表格合起来给一句，挂在正文末尾
    if (nTables && check && check.numbers) {
      const note = mk("div", "tbl-check " + (bad.length ? "bad" : "ok"));
      note.textContent = bad.length
        ? "表格中有 " + bad.length + " 个数字未能在引用片段中核对到出处（虚线标出），请点引用核对原文"
        : "表格中 " + check.numbers + " 个数字均可在引用片段中找到出处，或由片段数字算出";
      prose.appendChild(note);
    }
    body.appendChild(prose);
    return prose;
  }

  /* 单元格里是否含这个数字：前面不能紧挨数字或小数点，免得「11.23 亿元」命中「1.23 亿元」 */
  function hasNumber(cell, num) {
    const esc = num.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp("(^|[^\\d.])" + esc).test(cell);
  }

  function buildMdTable(rows, nSources, bad) {
    const wrap = mk("div", "md-tbl-wrap");
    const table = mk("table", "md-tbl");
    const split = (ln) => ln.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    // 第二行是「|---|---|」分隔行时，第一行是表头
    const hasHead = rows.length > 1 && TABLE_SEP.test(rows[1]);
    const body = hasHead ? rows.slice(2) : rows.filter((r) => !TABLE_SEP.test(r));
    if (hasHead) {
      const thead = mk("thead"), tr = mk("tr");
      split(rows[0]).forEach((h) => { const th = mk("th"); renderRich(th, h, nSources); tr.appendChild(th); });
      thead.appendChild(tr);
      table.appendChild(thead);
    }
    const tbody = mk("tbody");
    body.forEach((ln) => {
      const tr = mk("tr");
      split(ln).forEach((c, i) => {
        const td = mk("td", i === 0 ? "rowname" : null);
        renderRich(td, c, nSources);
        // 行名列不核对（后端也跳过了它）；其余单元格里含未核对数字的，虚线标出
        if (i > 0 && bad.some((b) => hasNumber(c, b))) {
          td.classList.add("unv");
          td.title = "未在引用片段中找到出处，请点引用核对原文";
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  /* ---------------- 数据卡 ---------------- */
  function buildCard(m) {
    const card = mk("div", "card");
    const head = mk("div", "card-head");
    // 优先用消息自己的标题/说明：程序抽取的真实数据带着「年报第 n 页」，
    // 全局默认值写的是「演示数据」，绝不能挂到真实数字上。
    head.appendChild(mk("span", "card-title", m.cardTitle || d.cardTitle || "主要会计数据"));
    head.appendChild(mk("span", "tag", m.cardNote || d.cardNote || "示例数据"));
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

  /* 向下的柱子（负值）：圆角在底部 */
  function barPathDown(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    return "M" + x + "," + y + "L" + (x + w) + "," + y + "L" + (x + w) + "," + (y + h - r) +
           "Q" + (x + w) + "," + (y + h) + " " + (x + w - r) + "," + (y + h) +
           "L" + (x + r) + "," + (y + h) + "Q" + x + "," + (y + h) + " " + x + "," + (y + h - r) + "Z";
  }

  /* 三年画柱、六年画折线（后端在 c.kind 里说明）。两种图共用坐标和悬停提示。
     SVG 只拼数字和我方常量，标签走 esc；提示框里的文字走 textContent。 */
  function chartNode(c) {
    const line = c.kind === "line";
    const box = mk("div", "chart " + (line ? "is-line" : "is-bar"));
    const title = mk("div", "chart-t");
    const t1 = mk("span", null, c.title);
    t1.appendChild(mk("span", "mono", "　" + (c.range || "") + " · " + (c.unit || "")));
    title.appendChild(t1);
    box.appendChild(title);
    if (c.cagr) box.appendChild(mk("div", "chart-sub " + (/-\d/.test(c.cagr) ? "down" : "up"), c.cagr));

    const W = 260, H = 128, top = 20, base = 92, plot = base - top;
    const x0 = 16, x1 = 250, n = c.values.length, band = (x1 - x0) / n, bw = 28;
    const vals = c.values.map(Number);
    // 以零为基线：有负值时（例如某年经营现金流为负）零线上移，负值向下画并用 --neg。
    const hi = Math.max(0, ...vals), lo = Math.min(0, ...vals);
    const span = (hi - lo) || 1;
    const zero = top + hi / span * plot;
    const barH = (v) => Math.max(3, Math.abs(v) / span * plot);
    const barTop = (v) => (v >= 0 ? zero - barH(v) : zero);
    // 折线两端各留 10px，首尾的点和数字不贴边
    const px = (i) => (line ? x0 + 10 + i * (x1 - x0 - 20) / Math.max(1, n - 1) : x0 + i * band + band / 2);
    const py = (v) => zero - v / span * plot;
    const id = "cg" + (++CHART_SEQ);
    const mono = 'font-family="var(--font-mono)" text-anchor="middle"';

    let svg = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(c.title + " " + (c.range || "")) + '">';
    svg += "<defs>" +
      '<linearGradient id="' + id + 'u" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--mark);stop-opacity:1"/><stop offset="1" style="stop-color:var(--mark);stop-opacity:.62"/></linearGradient>' +
      '<linearGradient id="' + id + 'p" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--mark);stop-opacity:.48"/><stop offset="1" style="stop-color:var(--mark);stop-opacity:.14"/></linearGradient>' +
      '<linearGradient id="' + id + 'n" x1="0" y1="1" x2="0" y2="0"><stop offset="0" style="stop-color:var(--neg);stop-opacity:.9"/><stop offset="1" style="stop-color:var(--neg);stop-opacity:.4"/></linearGradient>' +
      '<linearGradient id="' + id + 'a" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--mark);stop-opacity:.30"/><stop offset="1" style="stop-color:var(--mark);stop-opacity:0"/></linearGradient>' +
      "</defs>";
    // 网格：顶线 + 两条中间线，都是淡虚线；零线实线
    [0, 1 / 3, 2 / 3].forEach((f) => {
      const gy = top + f * plot;
      if (Math.abs(gy - zero) > 2) svg += '<line x1="' + x0 + '" y1="' + gy + '" x2="' + x1 + '" y2="' + gy + '" stroke="var(--line)" stroke-width="1" stroke-dasharray="2 3"/>';
    });
    svg += '<line x1="' + x0 + '" y1="' + zero + '" x2="' + x1 + '" y2="' + zero + '" stroke="var(--line-2)" stroke-width="1"/>';
    // 悬停时的列高亮（柱）/ 竖向参考线（折线），默认隐藏
    svg += line
      ? '<line class="guide" x1="0" y1="' + top + '" x2="0" y2="' + base + '" stroke="var(--accent-line)" stroke-width="1" stroke-dasharray="3 3"/>'
      : '<rect class="guide" x="0" y="' + (top - 6) + '" width="' + band + '" height="' + (plot + 6) + '" rx="6" fill="var(--accent-soft)"/>';

    if (line) {
      const pts = vals.map((v, i) => [px(i), py(v)]);
      const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join("");
      svg += '<path class="area" d="' + d + "L" + pts[n - 1][0].toFixed(1) + "," + zero + "L" + pts[0][0].toFixed(1) + "," + zero + 'Z" fill="url(#' + id + 'a)"/>';
      svg += '<path class="stroke" d="' + d + '" pathLength="1" fill="none" stroke="var(--mark)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
      vals.forEach((v, i) => {
        const [cx, cy] = pts[i];
        const col = v >= 0 ? "var(--mark)" : "var(--neg)";
        if (i === n - 1) {
          svg += '<circle class="halo" cx="' + cx + '" cy="' + cy + '" r="7.5" style="fill:' + col + '" opacity=".18"/>';
          svg += '<circle class="pt" data-i="' + i + '" cx="' + cx + '" cy="' + cy + '" r="3.6" style="fill:' + col + '" stroke="var(--surface)" stroke-width="1.5"/>';
        } else {
          svg += '<circle class="pt" data-i="' + i + '" cx="' + cx + '" cy="' + cy + '" r="2.6" fill="var(--surface)" style="stroke:' + col + '" stroke-width="1.5"/>';
        }
        svg += "<text " + mono + ' x="' + cx + '" y="' + (base + 15) + '" font-size="9.5" fill="var(--ink-3)">' + esc(c.labels[i]) + "</text>";
        // 只标首尾两个数：六个数挤在一起反而看不清走势，其余靠悬停
        if (i === 0 || i === n - 1) {
          // 标在远离相邻线段的一侧：相邻点更高，线往上走，数字就放到点下方
          const below = vals[i === 0 ? 1 : n - 2] > v;
          const ly = below ? Math.min(base - 4, cy + 15) : Math.max(top - 6, cy - 9);
          const anchor = i === 0 ? "start" : "end";
          const lx = i === 0 ? cx - 4 : cx + 4;
          svg += '<text font-family="var(--font-mono)" text-anchor="' + anchor + '" x="' + lx + '" y="' + ly + '" font-size="' + (i ? 10.5 : 9.5) + '" font-weight="' + (i ? 500 : 400) + '" fill="var(--' + (i ? "ink" : "ink-3") + ')">' + v.toFixed(2) + "</text>";
        }
      });
    } else {
      vals.forEach((v, i) => {
        const bx = x0 + i * band + (band - bw) / 2;
        const h = barH(v), by = barTop(v), last = i === n - 1;
        const fill = v < 0 ? id + "n" : id + (last ? "u" : "p");
        const path = v >= 0 ? barPath(bx, by, bw, h, 5) : barPathDown(bx, by, bw, h, 5);
        svg += '<g class="bar' + (v < 0 ? " neg" : "") + '" data-i="' + i + '" style="--i:' + i + '">';
        svg += '<path d="' + path + '" fill="url(#' + fill + ')"/>';
        // 柱顶一道亮边：比纯色块更有「实物」感
        const capY = v >= 0 ? by + .75 : by + h - .75;
        svg += '<line x1="' + (bx + 4) + '" y1="' + capY + '" x2="' + (bx + bw - 4) + '" y2="' + capY + '" stroke="' + (v >= 0 ? "var(--mark)" : "var(--neg)") + '" stroke-width="1.5" stroke-linecap="round" opacity="' + (last ? .9 : .7) + '"/>';
        svg += "</g>";
        // 数值标在柱顶上方；负值标在零线上方，避免和底部年份重叠
        const ly = v >= 0 ? by - 6 : zero - 6;
        svg += "<text " + mono + ' x="' + (bx + bw / 2) + '" y="' + ly + '" font-size="' + (last ? 10.5 : 9) + '" font-weight="' + (last ? 500 : 400) + '" fill="var(--' + (last ? "ink" : "ink-3") + ')">' + v.toFixed(2) + "</text>";
        svg += "<text " + mono + ' x="' + (bx + bw / 2) + '" y="' + (base + 14) + '" font-size="9.5" fill="var(--ink-2)">' + esc(c.labels[i]) + "</text>";
        const yoy = (c.yoy && c.yoy[i]) || "—";
        const col = yoy === "—" ? "var(--ink-3)" : (yoy.charAt(0) === "-" ? "var(--neg)" : "var(--pos)");
        svg += "<text " + mono + ' x="' + (bx + bw / 2) + '" y="' + (base + 26) + '" font-size="8.5" fill="' + col + '">' + esc(yoy) + "</text>";
      });
    }
    vals.forEach((v, i) => {
      const hx = line ? px(i) - band / 2 : x0 + i * band;
      svg += '<rect class="hit" data-i="' + i + '" x="' + hx + '" y="' + (top - 8) + '" width="' + band + '" height="' + (plot + 8) + '" fill="transparent" style="cursor:crosshair"/>';
    });
    svg += "</svg>";
    const holder = mk("div");
    holder.innerHTML = svg;                       // 只含数字与我方生成的属性，标签已 esc
    const svgEl = holder.firstChild;
    box.appendChild(svgEl);

    const tip = mk("div", "tip");
    box.appendChild(tip);
    const guide = svgEl.querySelector(".guide");
    box.querySelectorAll(".hit").forEach((hit) => {
      hit.onmouseenter = () => {
        const i = Number(hit.dataset.i);
        const t = mk("span");
        t.appendChild(document.createTextNode(c.labels[i] + " "));
        t.appendChild(mk("b", null, vals[i].toFixed(2) + " " + (c.unit || "")));
        t.appendChild(document.createElement("br"));
        t.appendChild(document.createTextNode("同比 " + ((c.yoy && c.yoy[i]) || "—")));
        clear(tip);
        tip.appendChild(t);
        const cx = px(i);
        const anchorY = line ? py(vals[i]) - 8 : Math.min(barTop(vals[i]), zero) - 16;
        tip.style.left = (cx / W * 100) + "%";
        tip.style.top = (anchorY / H * 100) + "%";
        tip.classList.add("show");
        if (line) { guide.setAttribute("x1", cx); guide.setAttribute("x2", cx); }
        else guide.setAttribute("x", x0 + i * band);
        box.classList.add("hovering");
        svgEl.querySelectorAll("[data-i]").forEach((el) => el.classList.toggle("on", Number(el.dataset.i) === i));
      };
      hit.onmouseleave = () => {
        tip.classList.remove("show");
        box.classList.remove("hovering");
      };
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
  /* 来源列表默认折叠：正文里的 [n] 已经能直接打开原文抽屉，列表平时只占地方。
     标题行写明「回答用到几段」，展开状态按消息记在 s.srcOpen 里 —— 新消息到来时
     整个对话流会重建，不记的话用户刚展开的列表会自己收回去。 */
  function foldable(title, key) {
    const det = mk("details", "srcs");
    det.open = s.srcOpen.has(key);
    det.ontoggle = () => { if (det.open) s.srcOpen.add(key); else s.srcOpen.delete(key); };
    const sum = mk("summary", "srcs-h");
    sum.appendChild(mk("span", null, title));
    sum.appendChild(mk("span", "srcs-line"));
    sum.appendChild(mk("span", "srcs-tog"));
    det.appendChild(sum);
    const list = mk("div", "srcs-list");
    det.appendChild(list);
    return [det, list];
  }

  function buildSources(m, idx) {
    const used = m.sources.filter((x) => x.cited).length;
    const [block, list] = foldable(
      "引用来源 " + m.sources.length + " 段" + (used ? " · 回答用到 " + used + " 段" : ""), "y" + idx);
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
      list.appendChild(b);
    });
    return block;
  }

  /* 外部补充的网页来源：和年报片段分开渲染，标题写明未经核验。 */
  function buildWebSources(m, idx) {
    const [block, list] = foldable(
      "公开信息来源 " + m.webSources.length + " 条 · 未经年报核验", "w" + idx);
    m.webSources.forEach((s) => {
      const a = document.createElement("a");
      a.className = "src web-src";
      const url = String(s.url || "");
      if (/^https?:\/\//i.test(url)) {
        a.href = url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      }
      a.appendChild(mk("span", "src-n", "网" + s.n));
      const mid = mk("span", "src-mid");
      mid.appendChild(mk("span", "src-path", s.title || url || "未命名来源"));
      const sub = [s.site, s.date].filter(Boolean).join(" · ");
      if (sub) mid.appendChild(mk("span", "src-sub", sub));
      a.appendChild(mid);
      list.appendChild(a);
    });
    return block;
  }

  function buildActions(m, idx) {
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
    // 「行业动态」：只挂在最后一条回答上（往前翻的旧回答点了也是问同一件事），
    // 行业动态回合本身不再挂。点了就是发一条自然语言问题，聊天记录里读得懂。
    const company = (d.doc && d.doc.company) || "";
    const isLast = idx === (d.messages || []).length - 1;
    if (company && isLast && m.kind !== "industry" && !d.busy) {
      const ind = mk("button", "act act-ind");
      ind.type = "button";
      ind.innerHTML = ICONS.pulse;
      ind.appendChild(mk("span", null, "行业动态"));
      ind.title = "联网查原材料价格、行业格局、同行进展（单独成段，未经报告核验）";
      ind.onclick = () => {
        ind.disabled = true;
        setTriggerValue("ask", { text: company + "所处行业的最新动态：原材料价格、竞争格局、同行进展" });
      };
      acts.appendChild(ind);
    }
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
    const sig = JSON.stringify([s.drawer, s.turnIdx, s.srcIdx, m ? m.sources : null, m && m.page ? [m.page.n, (m.page.png || "").length] : null,
                                [...s.collapsed]]);
    if (sig === s.drawerSig) return;
    s.drawerSig = sig;

    // 重建前记下抽屉的滚动位置、重建后放回去：点「查看原页」后不该跳回第一条
    const keepTop = body.scrollTop;
    requestAnimationFrame(() => { body.scrollTop = keepTop; });
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
