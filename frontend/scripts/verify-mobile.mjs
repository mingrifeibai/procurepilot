// 在独立演示容器中检查移动端工作台的导航和视口溢出。
import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { chromium } from "@playwright/test";

const [base, output] = process.argv.slice(2);
assert(base && output, "usage: verify-mobile.mjs <isolated demo URL> <report>");
const origin = new URL(base);
assert.equal(origin.hostname, "127.0.0.1");
const authorization = { Authorization: "Bearer demo-buyer" };
const health = await fetch(new URL("/api/health", origin)).then((r) =>
  r.json(),
);
assert.equal(health.mode, "demo");
assert.equal(health.supplier, "simulated");
const workspace = await fetch(new URL("/api/workspaces", origin), {
  method: "POST",
  headers: { ...authorization, "Content-Type": "application/json" },
  body: JSON.stringify({ name: "移动端合成验收" }),
});
assert.equal(workspace.status, 201);
const { id } = await workspace.json();
const browser = await chromium.launch({ channel: "msedge" });
const results = [];
try {
  for (const width of [320, 390, 768]) {
    const page = await browser.newPage({ viewport: { width, height: 844 } });
    await page.goto(origin.href);
    await page
      .locator(".workspace-bar select")
      .locator('option[value="' + id + '"]')
      .waitFor({ state: "attached" });
    const snapshot = page.waitForResponse((response) =>
      response.url().endsWith("/api/w/" + id + "/snapshot"),
    );
    await page.locator(".workspace-bar select").selectOption(id);
    await snapshot;
    for (const label of [
      "工作区概览",
      "采购需求",
      "产品与报价",
      "方案与审批",
      "采购与验收",
      "附件与证据",
      "澄清与偏好",
      "执行与审计",
    ]) {
      const button = page.locator(".nav").getByRole("button", { name: label });
      await button.click();
      assert((await button.getAttribute("class"))?.includes("active"));
      const measurement = await page.evaluate(() => ({
        viewport: document.documentElement.clientWidth,
        pageWidth: document.documentElement.scrollWidth,
        headerRight: Math.ceil(
          document.querySelector("header form").getBoundingClientRect().right,
        ),
      }));
      results.push({ width, tab: label, ...measurement });
    }
    await page.close();
  }
} finally {
  await browser.close();
}
const failures = results.filter(
  (row) => row.pageWidth > row.viewport || row.headerRight > row.viewport,
);
await writeFile(
  output,
  JSON.stringify({ passed: !failures.length, results, failures }, null, 2),
  "utf8",
);
assert.equal(
  failures.length,
  0,
  "page or login controls overflow the mobile viewport",
);
console.log(results.length + " mobile viewport/tab combinations passed");
