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
    s = { draft: "", scrollTop: 0, atBottom: true, drawer: false, turnIdx: -1, srcIdx: 0, activeCite: null, collapsed: new Set(), srcOpen: new Set(), headSig: null, msgSigs: [], drawerSig: null, themeOverride: null };
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
  const msgSig = (m, i) => JSON.stringify([Object.assign({}, m, { page: undefined }),
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
    // 原本贴在底部就继续贴底。当场设一次（不留一帧空档），图片和字体撑高后下一帧再补一次
    const stick = () => {
      if (s.atBottom) thread.scrollTop = thread.scrollHeight;
      else thread.scrollTop = Math.min(s.scrollTop, thread.scrollHeight);
    };
    stick();
    requestAnimationFrame(stick);
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
    body.appendChild(meta);
    if (m.notice) body.appendChild(mk("div", "notice", m.notice));
    wrap.appendChild(body);

    // 待定状态：检索和生成阶段还没有正文，只显示 spinner。
    // 但「扩展分析」阶段首轮回答已经产出，先把正文画出来再挂指示 ——
    // 用户不必等第二次模型调用跑完才看到东西。
    if (m.status === "resolving" || m.status === "retrieving" || m.status === "generating" || m.status === "analyzing" || m.status === "searching") {
      WEB_LIST = m.webSources || [];
      if (m.text) appendProse(body, m.text, (m.sources || []).length);
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

  function chartNode(c) {
    const box = mk("div", "chart");
    const title = mk("div", "chart-t");
    const t1 = mk("span", null, c.title);
    t1.appendChild(mk("span", "mono", "　" + (c.range || "") + " · " + (c.unit || "")));
    title.appendChild(t1);
    box.appendChild(title);

    const W = 260, top = 18, base = 92, plot = base - top;
    const x0 = 16, x1 = 250, n = c.values.length, band = (x1 - x0) / n, bw = 26;
    const vals = c.values.map(Number);
    // 以零为基线：有负值时（例如某年经营现金流为负）零线上移，负值向下画并用 --neg。
    // 以前负值会被画成 3px 的正柱子，看起来像「很小的正数」。
    const hi = Math.max(0, ...vals), lo = Math.min(0, ...vals);
    const span = (hi - lo) || 1;
    const zero = top + hi / span * plot;
    const barH = (v) => Math.max(3, Math.abs(v) / span * plot);
    const barTop = (v) => (v >= 0 ? zero - barH(v) : zero);
    let svg = '<svg viewBox="0 0 ' + W + ' 118" role="img" aria-label="' + esc(c.title + " " + (c.range || "")) + '">';
    svg += '<line x1="' + x0 + '" y1="' + top + '" x2="' + x1 + '" y2="' + top + '" stroke="var(--line)" stroke-width="1" stroke-dasharray="2 3"/>';
    svg += '<line x1="' + x0 + '" y1="' + zero + '" x2="' + x1 + '" y2="' + zero + '" stroke="var(--line)" stroke-width="1"/>';
    vals.forEach((v, i) => {
      const bx = x0 + i * band + (band - bw) / 2;
      const h = barH(v), by = barTop(v);
      const path = v >= 0 ? barPath(bx, by, bw, h, 4) : barPathDown(bx, by, bw, h, 4);
      svg += '<path d="' + path + '" fill="' + (v >= 0 ? "var(--mark)" : "var(--neg)") + '" opacity="' + (i === n - 1 ? 1 : .55) + '"/>';
      svg += '<text x="' + (bx + bw / 2) + '" y="' + (base + 15) + '" text-anchor="middle" font-family="var(--font-mono)" font-size="9.5" fill="var(--ink-3)">' + esc(c.labels[i]) + '</text>';
      if (i === n - 1) {
        // 负值的数字也标在零线上方，避免和底部年份重叠
        const ly = v >= 0 ? by - 7 : zero - 7;
        svg += '<text x="' + (bx + bw / 2) + '" y="' + ly + '" text-anchor="middle" font-family="var(--font-mono)" font-size="10.5" font-weight="500" fill="var(--ink)">' + v.toFixed(2) + '</text>';
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
        t.appendChild(mk("b", null, vals[i].toFixed(2) + " " + (c.unit || "")));
        t.appendChild(document.createElement("br"));
        t.appendChild(document.createTextNode("同比 " + ((c.yoy && c.yoy[i]) || "—")));
        clear(tip);
        tip.appendChild(t);
        const cx = x0 + i * band + band / 2;
        tip.style.left = (cx / W * 100) + "%";
        tip.style.top = ((Math.min(barTop(vals[i]), zero) - 10) / 118 * 100) + "%";
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
