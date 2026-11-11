/** 浏览器真实 HTTP / 后台 Worker 验收；仅允许连接独立模型桩服务8095。 */
const { chromium, request } = require('@playwright/test');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:8095';
(async () => {
  const client = await request.newContext({baseURL: base, extraHTTPHeaders: {Authorization: 'Bearer demo-buyer'}});
  const status = await (await client.get('/api/model-status')).json();
  assert.equal(status.model_name, 'browser-test-fake-model');
  const workspace = await (await client.post('/api/workspaces', {data: {name: '0.5 浏览器验收 · 异步与检索'}})).json();
  await client.post(`/api/w/${workspace.id}/requirements`, {data: {
    title: '固定合成电脑需求', budget: '8000', delivery_days: 14,
    needs: [{category: 'computer', quantity: 1, min_memory_gb: 32, interface: 'usb-c'}],
  }});
  const browser = await chromium.launch({executablePath:'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe', headless:true});
  try {
    const page = await browser.newPage({viewport: {width:1440, height:1000}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(base);
    await page.locator('.workspace-bar select').selectOption(workspace.id);
    await page.getByRole('button', {name:'执行与审计'}).click();
    const [response] = await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/analysis-jobs') && r.request().method() === 'POST'),
      page.getByRole('button', {name:'提交后台模型分析', exact:true}).click(),
    ]);
    assert.equal(response.status(), 202);
    const job = await response.json();
    assert.equal(job.state, 'queued');
    await page.reload();
    await page.locator('.workspace-bar select').selectOption(workspace.id);
    await page.getByRole('button', {name:'执行与审计'}).click();
    await page.getByText('模型分析 · completed', {exact:true}).waitFor({timeout:25000});
    const snapshot = await (await client.get(`/api/w/${workspace.id}/snapshot`)).json();
    assert.equal(snapshot.analysis.length, 1);
    assert.equal(snapshot.model_call.length, 3);
    assert.equal(snapshot.analysis[0].tokens, 450);
    assert.equal(snapshot.order.length, 0);
    await page.screenshot({path:'../../../outputs/procurepilot-05-background.png', fullPage:true});
    await page.getByRole('button', {name:'提交后台模型分析', exact:true}).click();
    await page.getByRole('button', {name:'停止后续角色', exact:true}).click();
    await page.getByText('模型分析 · cancelled', {exact:true}).waitFor();
    await page.getByRole('button', {name:'附件与证据'}).click();
    const text = '背景说明。'.repeat(600) + '\n扩展坞 USB-C 参数，报价450元，质保三年，交期七天。';
    await page.getByLabel('上传附件').setInputFiles({name:'合成扩展坞参数.txt', mimeType:'text/plain', buffer:Buffer.from(text)});
    await page.getByText('附件已保存', {exact:true}).waitFor();
    await page.getByPlaceholder('检索参数、条款或供应商').fill('USB-C 扩展坞 交期');
    await page.getByRole('button', {name:'搜索证据', exact:true}).click();
    await page.getByRole('button', {name:'查看对应原文', exact:true}).first().click();
    await page.getByText('已读取命中位置原文', {exact:true}).waitFor();
    assert.match(await page.locator('pre').innerText(), /质保三年/);
    await page.screenshot({path:'../../../outputs/procurepilot-05-retrieval.png', fullPage:true});
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({passed:true, workspace:workspace.id, analysis:job.id, checks:['async_202','reload_without_duplicate','three_roles_usage_450','cancel','late_text_retrieval','source_readback','zero_orders','no_browser_errors']}));
  } finally { await browser.close(); await client.dispose(); }
})().catch(error => {console.error(error); process.exitCode=1;});
