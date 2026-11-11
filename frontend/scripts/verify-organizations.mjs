// 在隔离容器验证组织管理员与只读成员使用工作台的路径。
import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { chromium, expect } from "@playwright/test";

const [base, output] = process.argv.slice(2);
const fixtureToken = process.env.ORGANIZATION_FIXTURE_TOKEN;
assert(
  base && output && fixtureToken,
  "需要隔离演示 URL、报告路径和夹具平台令牌",
);
const origin = new URL(base);
assert.equal(origin.hostname, "127.0.0.1");
const health = await fetch(new URL("/api/health", origin)).then((r) =>
  r.json(),
);
assert.equal(health.mode, "demo");
assert.equal(health.supplier, "simulated");

const browser = await chromium.launch({ channel: "msedge" });
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto(origin.href);
  async function connect(token) {
    await page.getByLabel("自定义访问令牌").fill(token);
    const verified = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/workspaces") &&
        response.request().headers().authorization === "Bearer " + token,
    );
    await page.getByRole("button", { name: "连接", exact: true }).click();
    assert.equal((await verified).status(), 200);
    await expect(page.getByText("平台令牌已验证并连接")).toBeVisible();
  }
  async function createOrganization(name) {
    await page.getByLabel("新组织名称").fill(name);
    await page.getByRole("button", { name: "创建组织" }).click();
    await expect(
      page.getByRole("status").filter({ hasText: "组织已创建" }),
    ).toBeVisible();
    await expect(
      page.getByLabel("新工作区归属").locator("option:checked"),
    ).toHaveText(name);
  }
  async function createWorkspace(name) {
    await page.getByLabel("新工作区名称").fill(name);
    await page.getByRole("button", { name: "创建工作区" }).click();
    await expect(
      page.getByLabel("选择工作区").locator("option:checked"),
    ).toContainText(name);
  }
  async function issue(name, role) {
    await page.getByLabel("成员名称").fill(name);
    await page.getByLabel("组织角色").selectOption(role);
    await page.getByRole("button", { name: "发放访问令牌" }).click();
    const issued = page.getByLabel("请复制访问令牌。关闭后无法再次查看。");
    await expect(issued).toHaveValue(/^pporg_/);
    const token = await issued.inputValue();
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    return token;
  }
  await connect(fixtureToken);
  await page.getByText("组织与成员", { exact: true }).click();
  await createOrganization("浏览器验收甲组织");
  await createWorkspace("甲采购部");
  const firstWorkspace = await page.getByLabel("选择工作区").inputValue();
  const admin = await issue("甲组织管理员", "admin");
  await createOrganization("浏览器验收乙组织");
  await createWorkspace("乙采购部");
  await connect(admin);
  const selector = page.getByLabel("选择工作区");
  await expect(
    selector.locator("option").filter({ hasText: "甲采购部" }),
  ).toHaveCount(1);
  await expect(
    selector.locator("option").filter({ hasText: "乙采购部" }),
  ).toHaveCount(0);
  const management = page.getByText("组织与成员", { exact: true });
  await expect(management).toBeVisible();
  if (!(await page.getByLabel("成员名称").isVisible()))
    await management.click();
  await expect(
    page.getByLabel("组织角色").locator('option[value="admin"]'),
  ).toHaveCount(0);
  const viewer = await issue("甲只读成员", "viewer");
  const fits = await page.evaluate(
    () => document.documentElement.scrollWidth <= innerWidth,
  );
  assert(fits, "组织成员表单超过 390px 视口");
  await connect(viewer);
  await expect(
    selector.locator("option").filter({ hasText: "甲采购部" }),
  ).toHaveCount(1);
  await expect(
    selector.locator("option").filter({ hasText: "乙采购部" }),
  ).toHaveCount(0);
  await expect(page.getByText("组织与成员", { exact: true })).toHaveCount(0);
  const firstPrefix = `/api/w/${firstWorkspace}`;
  const requirement = await page.request.post(
    new URL(firstPrefix + "/requirements", origin).href,
    {
      headers: { Authorization: "Bearer " + admin },
      data: {
        title: "轨迹验收电脑采购",
        budget: "8000",
        delivery_days: 14,
        needs: [
          {
            category: "computer",
            quantity: 1,
            min_memory_gb: 32,
            interface: "usb-c",
          },
        ],
      },
    },
  );
  assert.equal(requirement.status(), 201);
  await connect(admin);
  await selector.selectOption(firstWorkspace);
  await page.getByRole("button", { name: "运行采购规划" }).click();
  await expect(page.getByText("规划已完成，请查看方案及证据")).toBeVisible();
  await connect(viewer);
  await selector.selectOption(firstWorkspace);
  await page.getByRole("button", { name: "执行与审计" }).click();
  await page.getByText("查看运行轨迹", { exact: true }).click();
  const stages = page
    .getByRole("list", { name: "已提交的规划节点" })
    .getByRole("listitem");
  await expect(stages).toHaveText([
    /澄清需求/,
    /读取报价/,
    /生成方案/,
    /核验方案/,
  ]);
  await writeFile(
    output,
    JSON.stringify(
      {
        passed: true,
        fixture: true,
        viewport: 390,
        organizations: 2,
        workspaces: 2,
        checks: [
          "admin-workspace-scope",
          "viewer-workspace-scope",
          "member-role-limits",
          "member-form-fits",
          "scoped-run-trace-replay",
        ],
      },
      null,
      2,
    ),
  );
  console.log("组织管理和跨组织工作区隔离浏览器验收通过");
} finally {
  await browser.close();
}
