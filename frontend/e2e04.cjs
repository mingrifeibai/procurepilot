/** 浏览器验收混合团队模板和偏好版本生命周期；不发起模型调用。 */
const {chromium}=require('@playwright/test');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',headless:true});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8092');
 await page.getByLabel('新工作区名称').fill('0.4 浏览器验收 · 混合团队与偏好');
 await page.getByRole('button',{name:'＋ 创建工作区'}).click();
 await page.getByRole('button',{name:'填写采购需求'}).click();
 await page.getByLabel('研发与设计混合团队').check();
 await page.getByRole('button',{name:'保存需求版本'}).click();
 await page.getByText('已保存新版本，旧方案将无法执行',{exact:true}).waitFor();
 await page.getByRole('button',{name:'✦ 运行采购规划'}).click();
 await page.getByText('规划已完成，请查看方案及证据',{exact:true}).waitFor();
 const wid=await page.locator('.workspace-bar select').inputValue();
 const snapshot=await page.request.get(`http://127.0.0.1:8092/api/w/${wid}/snapshot`,{headers:{Authorization:'Bearer demo-buyer'}});
 const data=await snapshot.json();
 if(data.requirement.at(-1).needs.length!==4||data.plan.at(-1).total_cents!==18000000)throw Error('Mixed team template incorrect');
 await page.getByRole('button',{name:'澄清与偏好'}).click();
 await page.getByLabel('偏好名称').fill('交付偏好');
 await page.getByLabel('偏好内容').fill('优先统一到货');
 await page.getByLabel('我确认保存此偏好').check();
 await page.getByRole('button',{name:'保存偏好新版本'}).click();
 await page.getByText('交付偏好 · v1',{exact:true}).waitFor();
 await page.getByLabel('偏好内容').fill('允许分批到货，但不得改变明确交期');
 await page.getByLabel('我确认保存此偏好').check();
 await page.getByRole('button',{name:'保存偏好新版本'}).click();
 await page.getByText('交付偏好 · v2',{exact:true}).waitFor();
 await page.getByRole('button',{name:'停用偏好',exact:true}).click();
 await page.getByText(/已停用/).waitFor();
 await page.screenshot({path:'../../../outputs/procurepilot-04-dialogue.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Mixed team plan and preference save/update/deactivation passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
