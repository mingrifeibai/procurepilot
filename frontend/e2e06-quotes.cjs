/** 实际工作台报价导入。使用独立合成工作区，不发起模型调用。 */
const { chromium, request } = require("@playwright/test");
const assert = require("node:assert/strict");

async function main() {
  const baseURL = "http://127.0.0.1:8092";
  const client = await request.newContext({ baseURL, extraHTTPHeaders: { Authorization: "Bearer demo-buyer" } });
  const browser = await chromium.launch({
    executablePath: "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    headless: true,
  });
  try {
    const health = await (await client.get("/api/health")).json();
    assert.equal(health.mode, "demo");
    const workspace = await (await client.post("/api/workspaces", { data: { name: "0.6 浏览器验收 · CSV 报价" } })).json();
    const page = await browser.newPage();
    await page.goto(baseURL);
    await page.locator(".workspace-bar select").selectOption(workspace.id);
    await page.getByRole("button", { name: "附件与证据" }).click();
    const csv = "product_id,supplier,unit_price,stock,delivery_days\ndev-32,合成浏览器供应商,5300,4,6\nunknown,无效报价,200,4,6\n";
    await page.getByLabel("上传附件").setInputFiles({ name: "demo-quotes.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
    await page.getByRole("button", { name: "预览报价字段与缺项" }).click();
    assert.equal(await page.getByRole("checkbox", { name: /第 3 行/ }).isDisabled(), true);
    await page.getByRole("checkbox", { name: /第 2 行/ }).check();
    await page.getByRole("button", { name: "确认导入 1 行" }).click();
    await page.getByText("所选报价已导入，可在产品与报价查看来源", { exact: true }).waitFor();
    const snapshot = await (await client.get(`/api/w/${workspace.id}/snapshot`)).json();
    const imported = snapshot.quote.find((quote) => quote.supplier === "合成浏览器供应商");
    assert.equal(imported.price_cents, 530000);
    assert.match(imported.source, /^artifact:.*:row:2$/);
    assert.equal(snapshot.order.length, 0);
    console.log(JSON.stringify({ passed: true, source_line: 2, price_cents: imported.price_cents, orders: 0 }));
  } finally {
    await browser.close();
    await client.dispose();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
