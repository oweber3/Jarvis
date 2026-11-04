// Measures the web chat page served by fixture.py: first load (also at 4x CPU slowdown), opening the long chat, and
// the cost of one new message arriving in it. Usage: LONG=<fixture's --long> node measure.mjs <port> <controlPort>
// <label> [runs]; FIRST_ONLY=1 measures the first load alone. Needs Playwright with Chromium. See README.md.
import { chromium } from "playwright"

const [port, controlPort, label, runsArg] = process.argv.slice(2)
const runs = Number(runsArg ?? 5)
const base = `http://127.0.0.1:${port}/`
const control = `http://127.0.0.1:${controlPort}`

const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b)
  return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2
}

async function metrics(cdp) {
  const { metrics } = await cdp.send("Performance.getMetrics")
  return Object.fromEntries(metrics.map((m) => [m.name, m.value]))
}

async function post(path, body = {}) {
  const r = await fetch(control + path, { method: "POST", body: JSON.stringify(body) })
  return r.json()
}

const ids = await (await fetch(control + "/")).json()
const browser = await chromium.launch()
const out = { label, firstLoad: [], firstLoadSlow: [], openLong: [], update: [] }

async function fresh(throttle = 1) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } })
  const page = await context.newPage()
  const cdp = await context.newCDPSession(page)
  await cdp.send("Performance.enable")
  await cdp.send("Network.enable")
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true })
  if (throttle > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate: throttle })
  return { context, page, cdp }
}

async function openShortChat() {
  // Every run starts with the short chat open on the server.
  await fetch(`${base}api/chats/${ids.short}/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  })
}

for (let i = 0; i < runs; i++) {
  for (const throttle of [1, 4]) {
    await openShortChat()
    const { context, page, cdp } = await fresh(throttle)
    await page.goto(base, { waitUntil: "load" })
    await page.waitForSelector("text=Deleted the folder.", { timeout: 60000 })
    const ready = await page.evaluate(() => performance.now())
    const timing = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0]
      const fcp = performance.getEntriesByName("first-contentful-paint")[0]
      return { dcl: nav.domContentLoadedEventEnd, load: nav.loadEventEnd, fcp: fcp ? fcp.startTime : null }
    })
    const m = await metrics(cdp)
    const row = { ...timing, ready, heapMB: m.JSHeapUsedSize / 1048576, nodes: m.Nodes, script: m.ScriptDuration }
    ;(throttle === 1 ? out.firstLoad : out.firstLoadSlow).push(row)
    await context.close()
  }
}

for (let i = 0; i < (process.env.FIRST_ONLY ? 0 : runs); i++) {
  await openShortChat()
  const { context, page, cdp } = await fresh(1)
  await page.goto(base, { waitUntil: "load" })
  await page.waitForSelector("text=Deleted the folder.", { timeout: 60000 })
  await page.waitForTimeout(500)
  const before = await metrics(cdp)
  const t0 = await page.evaluate(() => performance.now())
  await page.getByText("Quarterly planning notes").first().click()
  const lastIndex = Math.floor(Number(process.env.LONG ?? 3000) / 2) - 1
  await page.waitForSelector(`text=Question number ${lastIndex}:`, { timeout: 120000 })
  await page.waitForTimeout(300)
  const t1 = await page.evaluate(() => performance.now())
  const after = await metrics(cdp)
  out.openLong.push({
    ms: t1 - t0 - 300,
    heapMB: after.JSHeapUsedSize / 1048576,
    nodes: after.Nodes,
    script: (after.ScriptDuration - before.ScriptDuration) * 1000,
    layout: (after.LayoutDuration - before.LayoutDuration) * 1000,
  })
  // One new voice exchange arriving while the long chat is open.
  for (let k = 0; k < 3; k++) {
    await page.waitForTimeout(800)
    const m0 = await metrics(cdp)
    const s0 = await page.evaluate(() => performance.now())
    const marker = `Marker reply ${i}-${k}`
    await post("/voice", { user: `marker question ${i}-${k}`, reply: marker })
    await page.waitForSelector(`text=${marker}`, { timeout: 30000 })
    const s1 = await page.evaluate(() => performance.now())
    await page.waitForTimeout(400)
    const m1 = await metrics(cdp)
    out.update.push({
      ms: s1 - s0,
      script: (m1.ScriptDuration - m0.ScriptDuration) * 1000,
      layout: (m1.LayoutDuration - m0.LayoutDuration) * 1000,
      style: (m1.RecalcStyleDuration - m0.RecalcStyleDuration) * 1000,
    })
  }
  await context.close()
}
await browser.close()

const summary = (rows, keys) => Object.fromEntries(keys.map((k) => [k, +median(rows.map((r) => r[k] ?? 0)).toFixed(1)]))
console.log(`📊 ${label}: medians of ${runs} runs`)
console.log(
  JSON.stringify(
    {
      label,
      firstLoad: summary(out.firstLoad, ["fcp", "dcl", "load", "ready", "heapMB", "nodes"]),
      firstLoadCpu4x: summary(out.firstLoadSlow, ["fcp", "dcl", "load", "ready", "heapMB"]),
      openLongChat: summary(out.openLong, ["ms", "heapMB", "nodes", "script", "layout"]),
      newMessageInLongChat: summary(out.update, ["ms", "script", "layout", "style"]),
    },
    null,
    2,
  ),
)
