/** 空输入/无效平台令牌不能破坏已登录会话，不使用真实模型密钥。 */
const { chromium } = require("@playwright/test");
(async () => {
  const browser = await chromium.launch({
    executablePath:
      "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    headless: true,
  });
  try {
    const page = await browser.newPage();
    await page.goto("http://127.0.0.1:8092");
    await page.waitForFunction(
      () => sessionStorage.getItem("token") === "demo-buyer",
    );
    await page.getByRole("button", { name: "连接", exact: true }).click();
    await page
      .getByRole("alert")
      .filter({ hasText: "请输入平台访问令牌" })
      .waitFor();
    if (
      (await page.evaluate(() => sessionStorage.getItem("token"))) !==
      "demo-buyer"
    )
      throw Error("Empty input changed session");
    await page.getByLabel("自定义访问令牌").fill("invalid-test-platform-token");
    await page.getByRole("button", { name: "连接", exact: true }).click();
    await page
      .getByRole("alert")
      .filter({ hasText: "当前登录已保留" })
      .waitFor();
    await page.reload();
    await page.waitForFunction(
      () => sessionStorage.getItem("token") === "demo-buyer",
    );
    const status = await page.evaluate(async () => {
      const r = await fetch("/api/workspaces", {
        headers: { Authorization: "Bearer " + sessionStorage.getItem("token") },
      });
      return r.status;
    });
    if (status !== 200) throw Error("Original login unavailable");
    console.log("Empty/invalid token preserves platform login: passed");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
