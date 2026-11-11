/** 验证增量方案在工作台可见，旧审批不能授权新方案。 */
const { chromium } = require("@playwright/test");
(async () => {
  const browser = await chromium.launch({
    executablePath:
      "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    headless: true,
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const base = "http://127.0.0.1:8091";
    const buyer = { Authorization: "Bearer demo-buyer" };
    async function post(path, body = {}, headers = buyer) {
      const r = await page.request.post(base + "/api" + path, {
        headers,
        data: body,
      });
      if (!r.ok()) throw Error(await r.text());
      return r.json();
    }
    const w = await post("/workspaces", { name: "0.3 浏览器重规划验收" });
    const needs = [
      {
        category: "computer",
        quantity: 20,
        min_memory_gb: 32,
        interface: "usb-c",
      },
      { category: "monitor", quantity: 20, interface: "usb-c" },
      { category: "dock", quantity: 20, interface: "usb-c" },
    ];
    await post(`/w/${w.id}/requirements`, {
      title: "研发设备",
      budget: "180000",
      delivery_days: 14,
      needs,
    });
    const p = await post(`/w/${w.id}/plans`);
    await post(
      `/w/${w.id}/plans/${p.id}/approve`,
      {},
      { Authorization: "Bearer demo-approver" },
    );
    needs[0].quantity = 21;
    await post(`/w/${w.id}/requirements`, {
      title: "研发设备新增一台电脑",
      budget: "180000",
      delivery_days: 14,
      needs,
    });
    await page.goto(base);
    await page.locator(".workspace-bar select").selectOption(w.id);
    await page.getByRole("button", { name: "方案与审批" }).click();
    await page.getByRole("button", { name: "基于此版重规划" }).click();
    await page
      .getByText("已生成增量方案，请重新审批", { exact: true })
      .waitFor();
    await page.getByText(/增量重规划 · 保留需求组 2、3/).waitFor();
    await page
      .getByRole("button", { name: "执行采购", exact: true })
      .first()
      .click();
    await page
      .getByRole("alert")
      .filter({ hasText: "需要审批当前方案版本" })
      .waitFor();
    await page.screenshot({
      path: "../../../outputs/procurepilot-03-replanning.png",
      fullPage: true,
    });
    console.log("0.3 browser replanning and approval boundary passed");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
