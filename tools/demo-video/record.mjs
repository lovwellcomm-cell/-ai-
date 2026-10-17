/**
 * 演示视频录制器：Playwright 驱动 headed Chrome，按 scenario.json 执行动作。
 *
 * 视频由外部 FFmpeg gdigrab 抓屏（见 capture.ps1），本脚本只负责"操作"，
 * 并输出一份 timeline.json 记录每个动作的真实时刻，供后期对齐字幕与加速段。
 *
 * 用法：
 *   node record.mjs --scenario tools/demo-video/scenario.json --timeline /tmp/timeline.json
 *
 * 为什么不用 page.video()：Playwright 自带录屏需要 ms-playwright/ffmpeg-1011，
 * 且不绘制鼠标指针——演示视频里看不见鼠标是致命的。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

// Playwright 缓存在 npx 目录里，允许通过环境变量覆盖
const PW =
  process.env.PLAYWRIGHT_MODULE ||
  'C:/Users/lovwe/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright';
const { chromium } = require(PW);

const argv = process.argv.slice(2);
const arg = (name, def) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : def;
};

const scenarioPath = arg('scenario', 'tools/demo-video/scenario.json');
const timelinePath = arg('timeline', 'tools/demo-video/timeline.json');
const scenario = JSON.parse(readFileSync(scenarioPath, 'utf8'));
const { width, height } = scenario.viewport;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const timeline = [];
const counts = new Map();
const t0 = Date.now();
const now = () => (Date.now() - t0) / 1000;

function mark(step, phase, extra = {}) {
  const rec = { at: +now().toFixed(2), step: step.at, action: step.action, phase, ...extra };
  timeline.push(rec);
  console.log(`[${rec.at.toFixed(2)}s] ${step.action} ${phase}${step.note ? ` — ${step.note}` : ''}`);
}

// 鼠标移动带缓动，避免瞬间跳变（录屏里看起来像瞬移）
async function glide(page, x, y, ms = 450) {
  const steps = 18;
  await page.evaluate(
    async ({ x, y, steps, ms }) => {
      // 通过 CDP 移动真实指针：Playwright 的 mouse.move 本身不发 intermediate 事件
    },
    { x, y, steps, ms }
  );
  await page.mouse.move(x, y, { steps });
  await sleep(ms);
}

const W = scenario.viewport.width;
const H = scenario.viewport.height;
const WIN_W = W;
const WIN_H = H + 100;   // 普通窗口：标签栏 + 地址栏约 100px

async function main() {
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: false,
    args: [
      '--disable-gpu',            // gdigrab 是 GDI 抓屏，GPU 合成层会抓到黑帧
      '--window-position=0,0',
      `--window-size=${WIN_W},${WIN_H}`,
      '--force-device-scale-factor=1',
      '--hide-scrollbars',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-infobars',
      '--disable-features=Translate,TranslateUI,ChromeWhatsNewUI',
      '--disable-translate',
      '--lang=zh-CN',
      '--disable-extensions',
      '--disable-component-extensions-with-background-pages',
    ],
  });

  const ctx = await browser.newContext({
    viewport: { width: W, height: H },
    deviceScaleFactor: 1,
    locale: 'zh-CN',
    colorScheme: 'light',
  });
  const page = await ctx.newPage();

  // 关掉翻译条：它是浏览器级 UI，只能通过偏好设置压制
  await page.addInitScript(() => {
    try {
      localStorage.setItem('translate', JSON.stringify({ enabled: false }));
    } catch {}
  });

  // 关掉 Chrome 的翻译提示（中文页面被识别为外文时会弹条，挡住右上角）
  await ctx.addInitScript(() => {
    try {
      localStorage.setItem('translate', '{"enabled":false}');
    } catch {}
  });

  // 录屏从这一刻开始计时（capture.ps1 已提前启动 ffmpeg，留 1.5s 余量）
  const startedFile = timelinePath.replace(/\.json$/, '.started');
  writeFileSync(startedFile, String(Date.now()));

  await page.goto(scenario.url, { waitUntil: 'domcontentloaded', timeout: 60000 });

  // 关键：等应用真正渲染完成再开始计时。
  // 否则 gdigrab 会录进 VS Code 窗口、Chrome 白屏、加载中的空页面。
  await page.waitForSelector('.arb', { timeout: 120000 });
  await page.waitForSelector('#thread', { timeout: 30000 });
  // 首屏的 Hero 区（示例问题卡）出现才算真正就绪
  await page.waitForSelector('button:has-text("财务摘要")', { timeout: 60000 });
  await sleep(1200);   // 留一点稳定时间，避免动画首帧抖动

  // 记录"应用真正可见"的时刻：gdigrab 从 capture.ps1 启动时就在录，
  // 这一段空窗由 post.py 用这个差值裁掉。
  const readyFile = timelinePath.replace(/\.json$/, '.ready');
  writeFileSync(readyFile, JSON.stringify({ readyAt: Date.now() }));

  for (const step of scenario.steps) {
    // 按 scenario 的相对时刻等待；等待期间是"静止画面"，同时也是观众读画面的时间
    const target = step.at;
    const wait = target - now();
    if (wait > 0) await sleep(wait * 1000);

    mark(step, 'start');

    try {
      switch (step.action) {
        case 'waitFor': {
          await page.waitForSelector(step.selector, {
            timeout: step.timeout || 30000,
            state: 'visible',
          });
          break;
        }
        case 'hover': {
          const el = await page.waitForSelector(step.selector, { timeout: 15000 });
          const box = await el.boundingBox();
          if (box) await glide(page, box.x + box.width / 2, box.y + box.height / 2, step.settle || 500);
          break;
        }
        case 'click': {
          const el = await page.waitForSelector(step.selector, { timeout: 20000 });
          const box = await el.boundingBox();
          if (box) await glide(page, box.x + box.width / 2, box.y + box.height / 2, 420);
          await el.click();
          await sleep(step.settle ?? 700);
          break;
        }
        case 'waitNew': {
          // 等待"新增"一条匹配元素，避免匹配到已有的旧消息。
          // 用录制脚本维护的基线计数（count 动作写入），而不是开始等待时的计数。
          const before = counts.get(step.selector) ?? 0;
          const deadline = Date.now() + (step.timeout || 90000);
          let n = before;
          while (Date.now() < deadline) {
            n = await page.locator(step.selector).count();
            if (n > before) break;
            await sleep(250);
          }
          if (n <= before) throw new Error(`等待新元素超时：${step.selector}（基线 ${before}）`);
          counts.set(step.selector, n);
          await sleep(step.settle ?? 600);
          break;
        }
        case 'count': {
          const n = await page.locator(step.selector).count();
          counts.set(step.selector, n);
          mark(step, 'counted', { count: n });
          break;
        }
        case 'type': {
          const el = await page.waitForSelector(step.selector, { timeout: 15000 });
          const box = await el.boundingBox();
          if (box) await glide(page, box.x + box.width / 2, box.y + box.height / 2, 380);
          await el.click();
          await el.fill('');
          // 逐字输入，录屏里能看到打字过程
          await el.type(step.text, { delay: 90 });
          if (step.press) {
            await el.press(step.press);
          }
          await sleep(step.settle ?? 600);
          break;
        }
        case 'press': {
          await page.waitForSelector(step.selector, { timeout: 15000 });
          await page.press(step.selector, step.key || 'Enter');
          await sleep(step.settle ?? 500);
          break;
        }
        case 'scroll': {
          const el = await page.waitForSelector(step.selector, { timeout: 15000 });
          const box = await el.boundingBox();
          if (box) await glide(page, box.x + box.width / 2, box.y + box.height / 2, 300);
          // 分多次滚动，画面更平滑
          const dy = step.dy ?? 300;
          const chunks = 8;
          for (let i = 0; i < chunks; i++) {
            await page.mouse.wheel(0, dy / chunks);
            await sleep(80);
          }
          await sleep(step.settle ?? 800);
          break;
        }
        case 'end':
          mark(step, 'done');
          break;
        default:
          throw new Error(`未知动作: ${step.action}`);
      }
      mark(step, 'done');
    } catch (err) {
      mark(step, 'error', { message: String(err.message || err).slice(0, 200) });
      console.error(`  ✗ 步骤失败（继续后续步骤）: ${err.message?.split('\n')[0]}`);
    }
  }

  // 收尾留 1.2s，避免最后一步刚结束就断帧
  await sleep(1200);
  timeline.push({ at: +now().toFixed(2), action: 'finish' });

  writeFileSync(timelinePath, JSON.stringify({ duration: +now().toFixed(2), timeline }, null, 2));
  console.log(`\n完成，真实时长 ${now().toFixed(1)}s → ${timelinePath}`);

  await ctx.close();
  await browser.close();
}

main().catch((e) => {
  console.error('录制失败:', e);
  process.exit(1);
});
