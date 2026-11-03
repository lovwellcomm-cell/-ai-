/**
 * 评测页组件前端：按 data（docs/eval/snapshot.json，由 tools/build_eval_snapshot.py 生成）画整页。
 *
 * 和 landing.js 同样的 Streamlit 约定（streamlit==1.66.0）：
 *  1. default export 每次 data 变化都会重跑 —— 整页重画、事件一律用 el.onxxx = 赋值，保持幂等；
 *  2. parentElement 是 ShadowRoot；
 *  3. setTriggerValue 只在点击里调用：顶栏「首页」「开始聊天」发 go（home / chat），由 app.py 改网址。
 *
 * 数据是仓库里的快照（不是访客输入），照样一律用 textContent 写字；链接只拼两种：
 * 仓库地址（固定前缀）和提交哈希（先过 HASH_RE）。
 */

const HASH_RE = /^[0-9a-f]{7,40}$/;
const REPO_RE = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+$/;

function mk(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = String(text);
  return n;
}

function arr(v) { return Array.isArray(v) ? v : []; }

function commitLink(repo, hash, cls) {
  if (!HASH_RE.test(String(hash || ""))) return mk("span", cls, hash || "");
  const a = mk("a", cls, String(hash).slice(0, 7));
  if (REPO_RE.test(repo || "")) {
    a.href = repo + "/commit/" + hash;
    a.target = "_blank";
    a.rel = "noopener";
    a.title = "在 GitHub 上看这次提交";
  }
  return a;
}

function section(title, sub) {
  const s = mk("section", "ev-sec");
  s.appendChild(mk("h2", null, title));
  if (sub) s.appendChild(mk("p", "ev-sub", sub));
  return s;
}

function hero(d) {
  const h = mk("section", "ev-hero");
  h.appendChild(mk("h1", null, "它答得对不对，我们这样测"));
  h.appendChild(mk("p", "ev-lede",
    "每次上线前跑一遍固定的评测：从提问到回答的整条流程、规划器理解问题、复核器拦错误、三张报表的程序抽取。"
    + "下面的数字都来自最近一次上线评测，题目、对照组和局限也一并写明。"));
  const meta = mk("div", "ev-meta");
  const r = d.release || {};
  if (r.date) meta.appendChild(mk("span", null, "评测时间 " + r.date));
  if (r.model) { const m = mk("span", null, "模型 "); m.appendChild(mk("span", "mono", r.model)); meta.appendChild(m); }
  if (r.commit) { const c = mk("span", null, "代码版本 "); c.appendChild(commitLink(d.repo, r.commit, "mono")); meta.appendChild(c); }
  h.appendChild(meta);
  const stats = mk("div", "ev-stats");
  arr(d.headline).forEach((x) => {
    const t = mk("div", "ev-stat");
    t.appendChild(mk("div", "ev-stat-v", x.value));
    t.appendChild(mk("div", "ev-stat-k", x.label));
    t.appendChild(mk("div", "ev-stat-s", x.sub));
    stats.appendChild(t);
  });
  h.appendChild(stats);
  return h;
}

function suites(d) {
  const s = section("测了什么", "六套评测，前三套调用真实模型，后三套是程序本身的检查。每张卡下面是复现命令。");
  const g = mk("div", "ev-grid");
  arr(d.suites).forEach((x) => {
    const c = mk("div", "ev-card ev-suite");
    const top = mk("div", "ev-suite-top");
    top.appendChild(mk("span", "ev-suite-n", x.name));
    top.appendChild(mk("span", "ev-tag", x.model ? "调用模型" : "不调模型"));
    c.appendChild(top);
    c.appendChild(mk("div", "ev-suite-r", x.result));
    c.appendChild(mk("p", "ev-suite-w", x.what));
    if (x.detail) c.appendChild(mk("p", "ev-suite-d", x.detail));
    if (x.cmd) c.appendChild(mk("div", "ev-cmd", x.cmd));
    g.appendChild(c);
  });
  s.appendChild(g);
  return s;
}

const ACTION = { retrieve: "补检索后重答", regenerate: "带着复核意见重写", replan: "重新理解问题" };

function caught(d) {
  const s = section("拦下的错误",
    "回答交给用户之前，复核器先查一遍：要点答到没有、说法有没有依据、片段里明明有却说「未找到」。不过关就回退一次。");
  s.appendChild(mk("h3", "ev-h3", "真实发生的：复核发现问题，回退后答对"));
  const list = mk("div", "ev-caught");
  const real = arr(d.caught);
  if (!real.length) list.appendChild(mk("p", "ev-empty", "这次上线评测里没有题目触发回退。"));
  real.forEach((x) => {
    const c = mk("div", "ev-card");
    c.appendChild(mk("p", "ev-q", "「" + x.q + "」"));
    c.appendChild(mk("p", "ev-q-meta", "复核后：" + (ACTION[x.action] || x.action) + (x.report ? "（去 " + x.report + " 补检索）" : "")));
    const note = mk("p", "ev-note");
    note.appendChild(mk("b", null, "复核说"));
    note.appendChild(document.createTextNode(x.note || "（没有给理由）"));
    c.appendChild(note);
    const pair = mk("div", "ev-pair");
    [["first", "第一版回答（没交给用户）"], ["final", "最终回答"]].forEach(([k, label]) => {
      const a = mk("div", "ev-ans " + k);
      const head = mk("div", "ev-ans-k");
      head.appendChild(mk("i"));
      head.appendChild(document.createTextNode(label));
      a.appendChild(head);
      a.appendChild(mk("p", "ev-ans-t", x[k]));
      pair.appendChild(a);
    });
    c.appendChild(pair);
    list.appendChild(c);
  });
  s.appendChild(list);

  s.appendChild(mk("h3", "ev-h3", "人为注入的：往正常回答里改出错误，看复核器拦不拦"));
  const inj = mk("div", "ev-card ev-inj");
  arr(d.injected).forEach((x) => {
    const row = mk("div", "ev-inj-row");
    row.appendChild(mk("div", "ev-inj-k", x.kind));
    const b = mk("div", "ev-inj-b");
    const q = mk("p", "ev-diff before");
    q.appendChild(mk("span", null, "题目"));
    q.appendChild(document.createTextNode("「" + x.q + "」"));
    b.appendChild(q);
    const before = mk("p", "ev-diff before");
    before.appendChild(mk("span", null, "原句"));
    before.appendChild(document.createTextNode(x.before));
    b.appendChild(before);
    const after = mk("p", "ev-diff after");
    after.appendChild(mk("span", null, "改成"));
    after.appendChild(mk("mark", null, x.after));
    b.appendChild(after);
    const why = mk("p", "ev-why");
    why.appendChild(mk("b", null, "✓ 拦下"));
    why.appendChild(document.createTextNode(x.note || ""));
    b.appendChild(why);
    row.appendChild(b);
    inj.appendChild(row);
  });
  s.appendChild(inj);

  // 这次判错的：原样列出（正常回答被拦、注入的错误没拦下或复核调用出错），有人工核对的附上说明
  const misses = arr(d.misses);
  if (misses.length) {
    s.appendChild(mk("h3", "ev-h3", "这次判错的（" + misses.length + " 例）"));
    const box = mk("div", "ev-card ev-inj");
    misses.forEach((x) => {
      const row = mk("div", "ev-inj-row");
      row.appendChild(mk("div", "ev-inj-k", x.kind));
      const b = mk("div", "ev-inj-b");
      const q = mk("p", "ev-diff before");
      q.appendChild(mk("span", null, "题目"));
      q.appendChild(document.createTextNode("「" + x.q + "」"));
      b.appendChild(q);
      const v = mk("p", "ev-diff");
      v.appendChild(mk("span", null, x.kind === "好回答" ? "被拦下" : "没拦下"));
      v.appendChild(document.createTextNode(x.verdict === "error" ? "复核调用出错" : (x.note || x.verdict)));
      b.appendChild(v);
      if (x.comment) b.appendChild(mk("p", "ev-why", x.comment));
      row.appendChild(b);
      box.appendChild(row);
    });
    s.appendChild(box);
  }
  return s;
}

function table(head, rows, wrapCols) {
  const wrap = mk("div", "ev-tbl-wrap");
  const t = mk("table", "ev-tbl");
  const thead = mk("thead");
  const hr = mk("tr");
  head.forEach((h) => hr.appendChild(mk("th", null, h)));
  thead.appendChild(hr);
  t.appendChild(thead);
  const tb = mk("tbody");
  rows.forEach((r) => {
    const tr = mk("tr");
    r.forEach((v, i) => {
      if (v instanceof Node) { const td = mk("td"); td.appendChild(v); tr.appendChild(td); return; }
      tr.appendChild(mk("td", (wrapCols || []).includes(i) ? "wrap" : null, v));
    });
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  wrap.appendChild(t);
  return wrap;
}

function experiments(d) {
  const s = section("对照实验", "每加一个机制，都和「没有它」的版本跑同一批题。数字直接来自那次评测的结果表。");
  const box = mk("div", "ev-exp");
  arr(d.experiments).forEach((x) => {
    const c = mk("div", "ev-card");
    c.appendChild(mk("p", "ev-exp-t", x.title));
    c.appendChild(mk("p", "ev-exp-w", x.what));
    c.appendChild(table([""].concat(arr(x.cols)), arr(x.rows)));
    if (x.note) c.appendChild(mk("p", "ev-exp-n", x.note));
    box.appendChild(c);
  });
  s.appendChild(box);
  return s;
}

function milestones(d) {
  const s = section("一路怎么改进的",
    "数字抄自当时的提交说明，点哈希可以在 GitHub 上核对。题库一直在扩（端到端 15 → 20 → 30 → 37 题），不同阶段的数不能横着比。");
  const ol = mk("ol", "ev-tl");
  arr(d.milestones).forEach((x) => {
    const li = mk("li");
    const top = mk("div", "ev-tl-top");
    top.appendChild(mk("span", "ev-tl-d", x.date));
    top.appendChild(mk("span", "ev-tl-t", x.title));
    if (x.result) top.appendChild(mk("span", "ev-tl-r", x.result));
    top.appendChild(commitLink(d.repo, x.commit, "ev-tl-c"));
    li.appendChild(top);
    if (x.note) li.appendChild(mk("p", "ev-tl-n", x.note));
    ol.appendChild(li);
  });
  s.appendChild(ol);
  return s;
}

function history(d) {
  const s = section("上线记录", "每次改了回答流程（rag/、ui/）的上线，合并前都跑一遍上面这套评测，结果记一行。");
  const rows = arr(d.history).map((h) => [
    h.date, commitLink(d.repo, h.commit, null), h.e2e, String(h.orphan), h.intent, h.review, h.statements,
    String(h.tests), h.note || "",
  ]);
  const c = mk("div", "ev-card");
  c.appendChild(table(["时间", "版本", "端到端", "无出处数字", "问题理解", "复核 放行 · 拦下", "三表出表", "单元测试", "说明"],
    rows, [8]));
  s.appendChild(c);
  return s;
}

function limits(d) {
  const s = section("局限", "这些评测能说明什么、不能说明什么。");
  const ul = mk("ul", "ev-limits");
  arr(d.limits).forEach((x) => ul.appendChild(mk("li", null, x)));
  s.appendChild(ul);
  return s;
}

export default function (component) {
  const { parentElement, data, setTriggerValue } = component;
  const d = data && typeof data === "object" ? data : {};
  const root = parentElement.querySelector("#ev");
  if (!root) return;
  root.setAttribute("data-theme", d.theme === "light" ? "light" : "dark");

  const repo = root.querySelector("#evRepo");
  if (repo && REPO_RE.test(d.repo || "")) { repo.href = d.repo; } else if (repo) { repo.remove(); }

  root.querySelectorAll("[data-go]").forEach((b) => {
    b.onclick = () => {
      b.disabled = true;
      if (setTriggerValue) setTriggerValue("go", b.getAttribute("data-go"));
    };
  });

  const main = root.querySelector("#evMain");
  while (main.firstChild) main.removeChild(main.firstChild);
  if (!d.headline) {
    main.appendChild(mk("p", "ev-empty", "评测数据还没有生成（docs/eval/snapshot.json）。"));
    return;
  }
  [hero, suites, caught, experiments, milestones, history, limits].forEach((f) => main.appendChild(f(d)));
  const foot = mk("footer", "ev-foot");
  foot.appendChild(document.createTextNode("评测脚本在仓库的 tools/ 目录，页面数据由 tools/build_eval_snapshot.py 生成于 " + (d.generated || "") + "。"));
  main.appendChild(foot);
}
