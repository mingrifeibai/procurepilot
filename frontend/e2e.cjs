/** 浏览器验收：真实点击需求、规划、角色审批与下单。 */
const { chromium } = require('@playwright/test');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8091');
 await page.getByRole('button',{name:'＋ 创建工作区'}).click();
 await page.getByRole('button',{name:'填写采购需求'}).click();
 await page.getByRole('button',{name:'保存需求版本'}).click();
 await page.getByRole('status').waitFor();
 await page.getByRole('button',{name:'✦ 运行采购规划'}).click();
 await page.getByText('待审批',{exact:false}).waitFor();
 await page.locator('header select').selectOption('demo-approver');
 await page.getByRole('button',{name:'审批此版本'}).click();
 await page.getByText('审批已记录',{exact:true}).waitFor();
 await page.locator('header select').selectOption('demo-buyer');
 await page.getByRole('button',{name:'执行采购'}).click();
 await page.getByText('采购单已创建',{exact:true}).waitFor();
 await page.getByRole('button',{name:'采购与验收'}).click();
 await page.getByText('已下单',{exact:true}).waitFor();
 await page.screenshot({path:'../../../outputs/procurepilot-workbench.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Browser procurement flow passed');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
