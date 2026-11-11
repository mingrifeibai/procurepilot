/** 采购工作台。数据以服务端快照为准，按钮状态仅改善交互，不承担授权。 */
import React, { useState, useEffect, useRef, useCallback } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { Evidence } from "./Evidence";
import { Dialogue } from "./Dialogue";
import { Members } from "./Members";
import { RunTrace } from "./RunTrace";
import { readAnalysisProgress } from "./AnalysisProgress";

type Line = {
  quote_id: string;
  name: string;
  supplier: string;
  quantity: number;
  price_cents: number;
  source: string;
  delivery_days: number;
};
type Item = { id: string; [key: string]: any };
type Snapshot = Record<string, Item[]>;
const money = (n: number) =>
  new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(
    (n || 0) / 100,
  );
const names: Record<string, string> = {
  computer: "电脑",
  monitor: "显示器",
  dock: "扩展坞",
  placed: "已下单",
  partial: "分批到货",
  completed: "已完成",
  exception: "验收异常",
};

function App() {
  const [credential, setCredential] = useState("");
  const [modelCheckResult, setModelCheckResult] = useState<Record<
    string,
    any
  > | null>(null);
  const [token, setToken] = useState(
    sessionStorage.getItem("token") || "demo-buyer",
  );
  const [spaces, setSpaces] = useState<Item[]>([]),
    [wid, setWid] = useState(""),
    [data, setData] = useState<Snapshot>({});
  const currentToken = useRef(token);
  const currentWorkspace = useRef("");
  const [tab, setTab] = useState("overview"),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const [catalog, setCatalog] = useState<Item[]>([]);
  const [sessionRole, setSessionRole] = useState("");
  const [platformAdmin, setPlatformAdmin] = useState(false);
  const [organizations, setOrganizations] = useState<Item[]>([]);
  const [organization, setOrganization] = useState("");
  const [organizationName, setOrganizationName] = useState("");
  const [mixedTeam, setMixedTeam] = useState(false);
  const [designCount, setDesignCount] = useState(5);
  const [analysisTokens, setAnalysisTokens] = useState(16000);
  const [coordinationMode, setCoordinationMode] = useState("dynamic");
  const [catalogMode, setCatalogMode] = useState("preloaded");
  const analysisSubmission = useRef<{
    wid: string;
    key: string;
    tokens: number;
    mode: string;
    catalogMode: string;
  } | null>(null);
  const [title, setTitle] = useState("研发团队设备采购"),
    [budget, setBudget] = useState("180000"),
    [days, setDays] = useState(14),
    [quantity, setQuantity] = useState(20),
    [split, setSplit] = useState(true);
  const [newName, setNewName] = useState("研发团队工作区");
  const [quote, setQuote] = useState({
    product_id: "dev-32",
    supplier: "演示供应商 B",
    unit_price: "5500",
    stock: 20,
    delivery_days: 10,
    valid_hours: 72,
    source: "用户录入的模拟报价",
  });
  const api = useCallback(
    async (path: string, body?: unknown, method?: string) => {
      const response = await fetch("/api" + path, {
        method: method || (body === undefined ? "GET" : "POST"),
        headers: {
          Authorization: "Bearer " + token,
          "Content-Type": "application/json",
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!response.ok) {
        const err = await response.json();
        throw Error(
          typeof err.detail === "string"
            ? err.detail
            : JSON.stringify(err.detail),
        );
      }
      return response.json();
    },
    [token],
  );
  async function refresh(id = wid) {
    if (
      !id ||
      id !== currentWorkspace.current ||
      token !== currentToken.current
    )
      return;
    const snapshot = await api(`/w/${id}/snapshot`);
    if (id === currentWorkspace.current && token === currentToken.current)
      setData(snapshot);
  }
  function selectWorkspace(id: string) {
    currentWorkspace.current = id;
    setData({});
    setWid(id);
  }
  function switchToken(next: string) {
    if (next === token) return;
    currentToken.current = next;
    selectWorkspace("");
    setSpaces([]);
    setSessionRole("");
    setPlatformAdmin(false);
    setOrganizations([]);
    setOrganization("");
    setOrganizationName("");
    setModelCheckResult(null);
    setError("");
    setNotice("");
    setBusy(false);
    setTab("overview");
    setToken(next);
  }
  /** 先验证候选平台令牌，失败时保留当前会话；模型密钥由服务端管理。 */
  async function connectPlatformToken() {
    const candidate = credential.trim();
    setCredential("");
    if (!candidate) {
      setError(
        "请输入平台访问令牌。演示环境直接选择采购员即可，无需填写模型 API Key。",
      );
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch("/api/workspaces", {
        headers: { Authorization: "Bearer " + candidate },
      });
      if (!response.ok)
        throw Error(
          "平台令牌验证失败，当前登录已保留。这里不填写智谱 API Key。",
        );
      await response.json();
      if (candidate !== token) switchToken(candidate);
      setNotice("平台令牌已验证并连接");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function perform(
    action: () => Promise<unknown>,
    message = "操作已完成",
  ) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      await refresh();
      if (token === currentToken.current) setNotice(message);
    } catch (e) {
      if (token === currentToken.current) setError(String(e));
    } finally {
      if (token === currentToken.current) setBusy(false);
    }
  }
  useEffect(() => {
    let active = true;
    sessionStorage.setItem("token", token);
    setSessionRole("");
    api("/session")
      .then((session) => {
        if (active) {
          setSessionRole(session.role);
          setPlatformAdmin(session.platform_admin);
          setOrganization(session.organization_id || "");
        }
      })
      .catch(() => {});
    api("/workspaces")
      .then((rows) => {
        if (active) setSpaces(rows);
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    api("/organizations")
      .then((rows) => {
        if (active) setOrganizations(rows);
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    api("/catalog")
      .then(setCatalog)
      .catch((e) => setError(String(e)));
    return () => {
      active = false;
    };
  }, [token]);
  useEffect(() => {
    if (!wid) return;
    // SSE 通知何时回读快照，断线时继续顺序轮询。切换工作区会中止两者。
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>,
      retry: ReturnType<typeof setTimeout>;
    let refreshing = false,
      changed = false,
      connected = false,
      cursor = "";
    setData({});
    async function poll() {
      if (controller.signal.aborted) return;
      if (refreshing) {
        changed = true;
        return;
      }
      clearTimeout(timer);
      refreshing = true;
      let delay = 10000;
      try {
        const response = await fetch(`/api/w/${wid}/snapshot`, {
          headers: { Authorization: "Bearer " + token },
          signal: controller.signal,
        });
        if (!response.ok) throw Error("进度查询失败，请检查平台连接");
        const snapshot: Snapshot = await response.json();
        if (
          controller.signal.aborted ||
          token !== currentToken.current ||
          wid !== currentWorkspace.current
        )
          return;
        setData(snapshot);
        if (
          !connected &&
          snapshot.analysis?.some((r) =>
            ["queued", "running"].includes(r.state),
          )
        )
          delay = 2000;
      } catch (e) {
        if (!controller.signal.aborted) setError(String(e));
      } finally {
        refreshing = false;
        if (!controller.signal.aborted) {
          timer = setTimeout(poll, changed ? 0 : delay);
          changed = false;
        }
      }
    }
    async function subscribe() {
      try {
        await readAnalysisProgress(
          wid,
          token,
          cursor,
          controller.signal,
          () => {
            connected = true;
          },
          (next) => {
            cursor = next;
            void poll();
          },
        );
      } catch {
        // 连接或代理不支持 SSE 时，由顺序轮询继续获取分析状态。
      } finally {
        connected = false;
        if (!controller.signal.aborted) retry = setTimeout(subscribe, 2000);
      }
    }
    void poll();
    void subscribe();
    return () => {
      controller.abort();
      clearTimeout(timer);
      clearTimeout(retry);
    };
  }, [wid, token]);

  async function enqueueAnalysis() {
    // 响应丢失后再次点击复用操作键和参数，防止无意创建第二个付费任务。
    if (!analysisSubmission.current || analysisSubmission.current.wid !== wid) {
      analysisSubmission.current = {
        wid,
        key: crypto.randomUUID(),
        tokens: analysisTokens,
        mode: coordinationMode,
        catalogMode: coordinationMode === "dynamic" ? catalogMode : "preloaded",
      };
    }
    const pending = analysisSubmission.current;
    await api(`/w/${wid}/analysis-jobs`, {
      max_tokens: pending.tokens,
      idempotency_key: pending.key,
      coordination_mode: pending.mode,
      catalog_mode: pending.catalogMode,
    });
    analysisSubmission.current = null;
  }
  const latest = data.requirement?.at(-1),
    plans = data.plan || [],
    orders = data.order || [];
  async function create() {
    const w = await api("/workspaces", {
      name: newName,
      ...(organization && { organization_id: organization }),
    });
    if (token !== currentToken.current) return;
    setSpaces((items) => [...items, w]);
    selectWorkspace(w.id);
  }
  async function run() {
    const r = await api(`/w/${wid}/runs`, { max_steps: 8 });
    await api(`/w/${wid}/runs/${r.id}/resume`, {});
    setTab("plans");
  }
  async function download(id: string) {
    const r = await fetch(`/api/w/${wid}/orders/${id}/export`, {
      headers: { Authorization: "Bearer " + token },
    });
    if (!r.ok) throw Error("导出失败");
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `order-${id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }
  async function downloadOriginal(id: string, filename: string) {
    const response = await fetch(`/api/w/${wid}/attachments/${id}/original`, {
      headers: { Authorization: "Bearer " + token },
    });
    if (!response.ok) throw Error("原始附件下载失败，请检查权限和原件状态");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  }
  return (
    <div className="layout">
      <aside>
        <div className="brand">
          <b>◈</b> ProcurePilot
        </div>
        <small>企业智能采购与供应商协同</small>
        <div className="nav">
          {[
            ["overview", "工作区概览"],
            ["requirements", "采购需求"],
            ["quotes", "产品与报价"],
            ["plans", "方案与审批"],
            ["orders", "采购与验收"],
            ["evidence", "附件与证据"],
            ["dialogue", "澄清与偏好"],
            ["runs", "执行与审计"],
            ...(sessionRole === "admin" ? [["members", "工作区成员"]] : []),
          ].map(([id, label], i) => (
            <button
              className={tab === id ? "active" : ""}
              onClick={() => setTab(id)}
              key={id}
            >
              <span>0{i + 1}</span>
              {label}
            </button>
          ))}
        </div>
        <div className="aside-bottom">
          <span className="dot" /> 本地演示环境
          <p>
            供应商与产品数据均为模拟。
            <br />
            采购单在本平台持久化保存。
          </p>
        </div>
      </aside>
      <main>
        <header>
          <div>WORKSPACE / 采购工作台</div>
          <select
            value={token.startsWith("demo-") ? token : "connected"}
            onChange={(e) => switchToken(e.target.value)}
          >
            <option value="demo-buyer">采购员 · 演示</option>
            <option value="demo-approver">审批人 · 演示</option>
            <option value="demo-viewer">只读成员 · 演示</option>
            {!token.startsWith("demo-") && (
              <option value="connected" disabled>
                平台令牌 · 已连接
              </option>
            )}
          </select>
          <form
            className="row"
            onSubmit={(e) => {
              e.preventDefault();
              void connectPlatformToken();
            }}
          >
            <input
              aria-label="自定义访问令牌"
              type="password"
              placeholder="平台令牌（非模型 API Key）"
              title="演示环境请选择左侧采购员角色。此处仅用于平台登录，不填写智谱 API Key。"
              value={credential}
              onChange={(e) => setCredential(e.target.value)}
              autoComplete="off"
            />
            <button className="secondary" disabled={busy}>
              连接
            </button>
          </form>
        </header>
        <section className="heading">
          <div>
            <p className="eyebrow">PROCUREMENT, WITH EVIDENCE</p>
            <h1>
              {
                (
                  {
                    overview: "让每一项采购都有据可循",
                    requirements: "明确目标，保留每次变更",
                    quotes: "统一报价口径与产品证据",
                    plans: "先验证约束，再审批执行",
                    orders: "从采购单到到货验收",
                    runs: "可追踪，也可以恢复",
                    evidence: "把每个结论连接到原始证据",
                    dialogue: "明确缺口，记住已确认的偏好",
                    members: "管理成员与工作区访问",
                  } as Record<string, string>
                )[tab]
              }
            </h1>
            <p>需求版本 · 约束求解 · 人工审批 · 可靠执行</p>
          </div>
          <button
            disabled={busy || !wid}
            onClick={() => perform(run, "规划已完成，请查看方案及证据")}
          >
            ✦ 运行采购规划
          </button>
        </section>
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
        {notice && (
          <div className="notice" role="status">
            {notice}
          </div>
        )}
        {sessionRole === "admin" && (platformAdmin || organization) && (
          <details>
            <summary>组织与成员</summary>
            {platformAdmin && (
              <form
                className="row"
                onSubmit={(event) => {
                  event.preventDefault();
                  void perform(async () => {
                    const row = await api("/organizations", {
                      name: organizationName.trim(),
                    });
                    if (token !== currentToken.current) return;
                    setOrganizations((items) => [...items, row]);
                    setOrganization(row.id);
                    setOrganizationName("");
                  }, "组织已创建");
                }}
              >
                <label>
                  新组织名称
                  <input
                    value={organizationName}
                    maxLength={100}
                    onChange={(event) =>
                      setOrganizationName(event.target.value)
                    }
                  />
                </label>
                <button disabled={busy || !organizationName.trim()}>
                  创建组织
                </button>
              </form>
            )}
            {organization && (
              <Members
                key={organization}
                endpoint={`/organizations/${organization}/members`}
                api={api}
                scope="组织"
                canManageAdmin={platformAdmin}
              />
            )}
          </details>
        )}
        <div className="workspace-bar">
          {platformAdmin && organizations.length > 0 && (
            <select
              aria-label="新工作区归属"
              value={organization}
              onChange={(event) => setOrganization(event.target.value)}
            >
              <option value="">历史工作区范围</option>
              {organizations.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          )}
          <select
            aria-label="选择工作区"
            value={wid}
            onChange={(e) => selectWorkspace(e.target.value)}
          >
            <option value="">选择工作区</option>
            {spaces.map((w) => {
              const owner = organizations.find(
                (item) => item.id === w.organization_id,
              );
              return (
                <option value={w.id} key={w.id}>
                  {owner ? owner.name + " / " : ""}
                  {w.name}
                </option>
              );
            })}
          </select>
          <input
            aria-label="新工作区名称"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <button
            className="secondary"
            disabled={busy}
            onClick={() => perform(create)}
          >
            ＋ 创建工作区
          </button>
          <button
            className="secondary"
            disabled={busy || !wid}
            onClick={() => perform(() => refresh())}
          >
            刷新
          </button>
        </div>
        {!wid ? (
          <div className="empty">
            <h2>开启一项设备采购</h2>
            <p>创建工作区后，录入需求并运行规划。切换审批角色后可审批方案。</p>
          </div>
        ) : (
          <>
            {tab === "overview" && (
              <>
                <div className="stats">
                  {[
                    ["需求预算", money(latest?.budget_cents)],
                    ["方案版本", plans.length],
                    ["采购订单", orders.length],
                    [
                      "待处理异常",
                      data.exception?.filter((x) => x.state === "open")
                        .length || 0,
                    ],
                  ].map(([a, b]) => (
                    <article key={a}>
                      <small>{a}</small>
                      <strong>{b}</strong>
                    </article>
                  ))}
                </div>
                <div className="grid">
                  <article>
                    <p className="eyebrow">YOUR NEXT MOVE</p>
                    <h2>
                      {latest ? "需求已就绪，开始比选" : "先描述团队的采购需求"}
                    </h2>
                    <p>
                      系统会验证数量、预算、库存、交期与接口要求。未知参数不会被默认为兼容。
                    </p>
                    <button
                      onClick={() => setTab(latest ? "plans" : "requirements")}
                    >
                      {latest ? "查看方案" : "填写采购需求"} →
                    </button>
                  </article>
                  <article>
                    <h2>一项采购，完整闭环</h2>
                    {[
                      "需求与报价分别版本化",
                      "基于实际库存生成可行方案",
                      "审批绑定具体方案版本",
                      "幂等下单，分批到货验收",
                    ].map((x, i) => (
                      <p key={x} className="step">
                        <em>{i + 1}</em>
                        {x}
                      </p>
                    ))}
                  </article>
                </div>
              </>
            )}
            {tab === "requirements" && (
              <div className="grid">
                <article>
                  <h2>新增需求版本</h2>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      perform(
                        () =>
                          api(`/w/${wid}/requirements`, {
                            title,
                            budget,
                            delivery_days: days,
                            allow_split: split,
                            needs: [
                              {
                                category: "computer",
                                quantity: mixedTeam
                                  ? quantity - designCount
                                  : quantity,
                                min_memory_gb: 32,
                                interface: "usb-c",
                                purpose: "研发岗位",
                              },
                              ...(mixedTeam && designCount > 0
                                ? [
                                    {
                                      category: "computer",
                                      quantity: designCount,
                                      min_memory_gb: 64,
                                      interface: "usb-c",
                                      purpose: "设计岗位",
                                    },
                                  ]
                                : []),
                              {
                                category: "monitor",
                                quantity,
                                min_memory_gb: 0,
                                interface: "usb-c",
                              },
                              {
                                category: "dock",
                                quantity,
                                min_memory_gb: 0,
                                interface: "usb-c",
                              },
                            ],
                          }),
                        "已保存新版本，旧方案将无法执行",
                      );
                    }}
                  >
                    <label>
                      采购主题
                      <input
                        value={title}
                        onChange={(e) => setTitle(e.target.value)}
                        required
                      />
                    </label>
                    <div className="row">
                      <label>
                        总预算（元）
                        <input
                          type="number"
                          min="1"
                          step="0.01"
                          value={budget}
                          onChange={(e) => setBudget(e.target.value)}
                        />
                      </label>
                      <label>
                        团队人数
                        <input
                          type="number"
                          min="1"
                          max="10000"
                          value={quantity}
                          onChange={(e) => {
                            const n = +e.target.value;
                            setQuantity(n);
                            setDesignCount((v) =>
                              Math.min(v, Math.max(1, n - 1)),
                            );
                            if (n < 2) setMixedTeam(false);
                          }}
                        />
                      </label>
                      <label>
                        交期（天）
                        <input
                          type="number"
                          min="1"
                          max="365"
                          value={days}
                          onChange={(e) => setDays(+e.target.value)}
                        />
                      </label>
                    </div>
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={mixedTeam}
                        disabled={quantity < 2}
                        onChange={(e) => setMixedTeam(e.target.checked)}
                      />
                      研发与设计混合团队
                    </label>
                    {mixedTeam && (
                      <label>
                        设计岗位人数（64G电脑）
                        <input
                          type="number"
                          min={1}
                          max={Math.max(1, quantity - 1)}
                          value={designCount}
                          onChange={(e) => setDesignCount(+e.target.value)}
                        />
                      </label>
                    )}
                    <p>
                      工作台快捷模板：每人 1 台 32G 电脑、1 台显示器和 1 个
                      USB-C 扩展坞。自定义类别和参数可通过 API 提交。
                    </p>
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={split}
                        onChange={(e) => setSplit(e.target.checked)}
                      />
                      允许多供应商拆单
                    </label>
                    <button disabled={busy}>保存需求版本</button>
                  </form>
                </article>
                <article>
                  <h2>版本历史</h2>
                  {[...(data.requirement || [])].reverse().map((r) => (
                    <div className="list" key={r.id}>
                      <b>
                        v{r.version} · {r.title}
                      </b>
                      <p>
                        {money(r.budget_cents)} · {r.delivery_days} 天 ·{" "}
                        {r.allow_split ? "允许拆单" : "单供应商"}
                      </p>
                      <small>{r.id}</small>
                    </div>
                  ))}
                </article>
              </div>
            )}
            {tab === "quotes" && (
              <>
                <article>
                  <h2>新增报价</h2>
                  <form
                    className="row"
                    onSubmit={(e) => {
                      e.preventDefault();
                      perform(() => api(`/w/${wid}/quotes`, quote));
                    }}
                  >
                    <label>
                      产品
                      <select
                        value={quote.product_id}
                        onChange={(e) =>
                          setQuote({ ...quote, product_id: e.target.value })
                        }
                      >
                        {catalog.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    {[
                      ["supplier", "供应商"],
                      ["unit_price", "含税含运单价"],
                      ["stock", "库存"],
                      ["delivery_days", "交期（天）"],
                      ["valid_hours", "有效小时"],
                      ["source", "来源"],
                    ].map(([k, label]) => (
                      <label key={k}>
                        {label}
                        <input
                          required
                          value={(quote as any)[k]}
                          type={
                            [
                              "stock",
                              "delivery_days",
                              "valid_hours",
                              "unit_price",
                            ].includes(k)
                              ? "number"
                              : "text"
                          }
                          min="0"
                          onChange={(e) =>
                            setQuote({
                              ...quote,
                              [k]: [
                                "stock",
                                "delivery_days",
                                "valid_hours",
                              ].includes(k)
                                ? +e.target.value
                                : e.target.value,
                            })
                          }
                        />
                      </label>
                    ))}
                    <button disabled={busy}>保存报价</button>
                  </form>
                </article>
                <article className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>产品 / 供应商</th>
                        <th>单价</th>
                        <th>库存</th>
                        <th>交期</th>
                        <th>有效期 / 来源</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.quote?.map((q) => (
                        <tr key={q.id}>
                          <td>
                            <b>
                              {catalog.find((p) => p.id === q.product_id)?.name}
                            </b>
                            <small>{q.supplier}</small>
                          </td>
                          <td>{money(q.price_cents)}</td>
                          <td>{q.stock}</td>
                          <td>{q.delivery_days} 天</td>
                          <td>
                            {new Date(q.expires_at).toLocaleString()}
                            <small>{q.source}</small>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </article>
              </>
            )}
            {tab === "plans" && (
              <>
                {!plans.length && (
                  <div className="empty">保存需求后，点击“运行采购规划”。</div>
                )}
                {[...plans].reverse().map((p) => (
                  <article key={p.id}>
                    <div className="card-head">
                      <div>
                        <span className="badge">
                          方案 v{p.version} ·{" "}
                          {p.status === "feasible"
                            ? "约束可行"
                            : "未找到可行方案"}
                        </span>
                        <h2>
                          {money(p.total_cents)}{" "}
                          <small>较上一版 {money(p.delta_cents)}</small>
                        </h2>
                      </div>
                      <div className="row">
                        <button
                          className="secondary"
                          disabled={busy}
                          onClick={() =>
                            perform(
                              () =>
                                api(`/w/${wid}/replan`, {
                                  previous_plan_id: p.id,
                                }),
                              "已生成增量方案，请重新审批",
                            )
                          }
                        >
                          基于此版重规划
                        </button>
                        <button
                          className="secondary"
                          disabled={busy || p.status !== "feasible"}
                          onClick={() =>
                            perform(
                              () => api(`/w/${wid}/plans/${p.id}/approve`, {}),
                              "审批已记录",
                            )
                          }
                        >
                          审批此版本
                        </button>
                        <button
                          disabled={busy || p.status !== "feasible"}
                          onClick={() =>
                            perform(
                              () =>
                                api(`/w/${wid}/plans/${p.id}/execute`, {
                                  idempotency_key: `execute-${p.id}`,
                                }),
                              "采购单已创建",
                            )
                          }
                        >
                          执行采购
                        </button>
                      </div>
                    </div>
                    {p.replanning && (
                      <p>
                        增量重规划 · 保留需求组{" "}
                        {p.replanning.retained_need_indices
                          .map((i: number) => i + 1)
                          .join("、") || "无"}{" "}
                        · 变更 {p.replanning.changes.length} 项分配 ·{" "}
                        {p.replanning.full_fallback
                          ? "全局约束冲突，已回退全量求解"
                          : "已复用未受影响组"}{" "}
                        · 需要重新审批
                      </p>
                    )}
                    <p>
                      {p.reason ||
                        "已校验预算、数量、库存、交期和声明接口；不代表真实硬件组合认证。"}
                    </p>
                    <small>
                      需求 {p.requirement_id} ·{" "}
                      {data.approval?.some((a) => a.plan_id === p.id)
                        ? "已审批"
                        : "待审批"}
                    </small>
                    <table>
                      <thead>
                        <tr>
                          <th>产品</th>
                          <th>供应商</th>
                          <th>数量</th>
                          <th>单价</th>
                          <th>证据</th>
                        </tr>
                      </thead>
                      <tbody>
                        {p.lines.map((l: Line, i: number) => (
                          <tr key={i}>
                            <td>{l.name}</td>
                            <td>{l.supplier}</td>
                            <td>{l.quantity}</td>
                            <td>{money(l.price_cents)}</td>
                            <td>
                              <code>{l.source}</code>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </article>
                ))}
              </>
            )}
            {tab === "orders" && (
              <>
                {!orders.length && (
                  <div className="empty">
                    审批并执行方案后，采购单会出现在这里。
                  </div>
                )}
                {orders.map((o) => (
                  <article key={o.id}>
                    <div className="card-head">
                      <h2>
                        采购单 <small>{o.id.slice(0, 8)}</small>
                      </h2>
                      <span className="badge">{names[o.state] || o.state}</span>
                      <button
                        className="secondary"
                        onClick={() => perform(() => download(o.id), "已导出")}
                      >
                        导出 CSV
                      </button>
                    </div>
                    <p>{money(o.total_cents)} · 模拟供应商，不涉及外部付款</p>
                    {o.state === "placed" && (
                      <button
                        className="secondary"
                        disabled={busy}
                        onClick={() =>
                          perform(
                            () => api(`/w/${wid}/orders/${o.id}/cancel`, {}),
                            "采购单已取消，库存已回补",
                          )
                        }
                      >
                        审批取消订单
                      </button>
                    )}
                    {o.lines.map((l: Line, i: number) => {
                      const receipts = (data.receipt || []).filter(
                        (r) => r.order_id === o.id && r.quote_id === l.quote_id,
                      );
                      const arrived = receipts.reduce(
                        (a, r) => a + r.quantity,
                        0,
                      );
                      return (
                        <div className="list" key={i}>
                          <b>
                            {l.name} × {l.quantity}
                          </b>
                          <p>
                            累计到货 {arrived} · 验收合格{" "}
                            {receipts.reduce((a, r) => a + r.accepted, 0)}
                          </p>
                          <Receipt
                            disabled={
                              busy ||
                              o.state === "cancelled" ||
                              arrived >= l.quantity
                            }
                            remaining={l.quantity - arrived}
                            onSubmit={(n, a, note) =>
                              perform(() =>
                                api(`/w/${wid}/orders/${o.id}/receipts`, {
                                  quote_id: l.quote_id,
                                  quantity: n,
                                  accepted: a,
                                  note,
                                  idempotency_key: crypto.randomUUID(),
                                }),
                              )
                            }
                          />
                        </div>
                      );
                    })}
                  </article>
                ))}
              </>
            )}
            {tab === "orders" &&
              (data.exception || [])
                .filter((e) => e.state === "open")
                .map((issue) => (
                  <article key={issue.id}>
                    <h2>待处理验收异常</h2>
                    <p>
                      {issue.note || "验收数量不一致"} · 采购单{" "}
                      {issue.order_id.slice(0, 8)}
                    </p>
                    <form
                      className="row"
                      onSubmit={(e) => {
                        e.preventDefault();
                        const note = new FormData(e.currentTarget).get("note");
                        const receipt = data.receipt.find(
                          (r) => r.id === issue.receipt_id,
                        );
                        perform(
                          () =>
                            api(`/w/${wid}/exceptions/${issue.id}/resolve`, {
                              accepted_after_reinspection: receipt?.quantity,
                              note,
                            }),
                          "复检已记录，异常已关闭",
                        );
                      }}
                    >
                      <label>
                        复检合格说明
                        <input
                          required
                          name="note"
                          placeholder="请先完成实际复检，再填写结果"
                        />
                      </label>
                      <button disabled={busy}>审批复检合格并关闭</button>
                    </form>
                  </article>
                ))}
            {tab === "evidence" && (
              <Evidence
                key={wid}
                wid={wid}
                api={api}
                perform={perform}
                downloadOriginal={downloadOriginal}
              />
            )}
            {tab === "dialogue" && (
              <Dialogue wid={wid} api={api} perform={perform} />
            )}
            {tab === "members" && sessionRole === "admin" && (
              <Members key={wid} endpoint={`/w/${wid}/members`} api={api} />
            )}
            {tab === "runs" && (
              <>
                <article>
                  <div className="card-head">
                    <h2>运行与恢复</h2>
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() =>
                        perform(async () => {
                          setModelCheckResult(await api("/model-check", {}));
                        }, "模型自检已结束")
                      }
                    >
                      模型自检（1 次请求）
                    </button>
                    <button
                      disabled={busy}
                      onClick={() =>
                        perform(
                          enqueueAnalysis,
                          "分析已提交后台，可切换页面或刷新查看进度",
                        )
                      }
                    >
                      提交后台模型分析
                    </button>
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() =>
                        perform(
                          () => api(`/w/${wid}/dispatch`, {}),
                          "Outbox 已投递",
                        )
                      }
                    >
                      投递待处理事件
                    </button>
                  </div>
                  <label>
                    分析总Token预算
                    <select
                      value={analysisTokens}
                      onChange={(e) => setAnalysisTokens(+e.target.value)}
                    >
                      <option value={16000}>16000（默认）</option>
                      <option value={24000}>24000（复杂五角色场景）</option>
                      <option value={32000}>32000（更长证据）</option>
                    </select>
                  </label>
                  <label>
                    多智能体编排
                    <select
                      aria-label="多智能体编排"
                      value={coordinationMode}
                      onChange={(e) => setCoordinationMode(e.target.value)}
                    >
                      <option value="dynamic">协调员按需求委派专业任务</option>
                      <option value="fixed">固定角色流程（对照）</option>
                    </select>
                  </label>
                  {coordinationMode === "dynamic" && (
                    <label>
                      目录证据
                      <select
                        value={catalogMode}
                        onChange={(e) => setCatalogMode(e.target.value)}
                      >
                        <option value="preloaded">预加载快照</option>
                        <option value="on_demand">按需查询 MCP 目录</option>
                      </select>
                    </label>
                  )}
                  <p>
                    离线规则节点使用 LangGraph
                    编排，不产生模型费用。模型分析在后台执行，所有角色共享用量预算；进度自动更新，刷新页面不会重新发起调用。
                  </p>
                  {modelCheckResult && (
                    <div className="notice" role="status">
                      模型自检：
                      {modelCheckResult.status === "ok"
                        ? "通过"
                        : modelCheckResult.hint || modelCheckResult.status}{" "}
                      · {modelCheckResult.elapsed_ms} ms{" "}
                      {modelCheckResult.usage
                        ? ` · ${modelCheckResult.usage.prompt_tokens + modelCheckResult.usage.completion_tokens} Token`
                        : ""}
                    </div>
                  )}
                  {data.analysis?.map((r) => (
                    <div className="list" key={r.id}>
                      <b>
                        模型分析 ·{" "}
                        {r.state === "queued"
                          ? "排队中"
                          : r.state === "running"
                            ? "执行中"
                            : r.state}
                      </b>
                      <p>
                        已完成 {r.steps} / {r.roles?.length || 3} 个角色 · 任务{" "}
                        {r.id}
                      </p>
                      {r.catalog_mode === "on_demand" && <p>目录按需取证</p>}
                      {r.evidence_gap && <p role="status">{r.evidence_gap}</p>}
                      {r.delegation_state && (
                        <p>
                          任务拆分：
                          {r.delegation_state === "model_selected"
                            ? "协调员已决定"
                            : r.delegation_state === "policy_completed"
                              ? `拆单规则补派 ${r.policy_added_roles.join("、")}`
                              : "模型未提供拆分，使用固定流程"}
                        </p>
                      )}
                      {r.delegated_tasks?.map((task: any) => (
                        <p key={task.role}>
                          {task.role}：{task.objective}
                        </p>
                      ))}
                      {r.parallel_batches?.map((batch: any) => (
                        <p key={batch.id}>
                          并行执行 · {batch.roles.join("、")}
                        </p>
                      ))}
                      {r.tool_results?.length > 0 && (
                        <details>
                          <summary>
                            MCP 只读工具 · {r.tool_results.length} 次
                          </summary>
                          {r.tool_results.map((result: any, index: number) => (
                            <div key={index}>
                              <p>
                                {result.tool} ·{" "}
                                {result.state === "completed"
                                  ? "已返回"
                                  : "不可用"}
                              </p>
                              <pre>
                                {JSON.stringify(
                                  {
                                    参数: result.arguments,
                                    证据: result.output,
                                  },
                                  null,
                                  2,
                                )}
                              </pre>
                            </div>
                          ))}
                        </details>
                      )}
                      {["queued", "running"].includes(r.state) && (
                        <progress value={r.steps} max={r.roles?.length || 3} />
                      )}
                      {r.error && (
                        <p role="status">
                          {r.error.hint}{" "}
                          {r.error.http_status
                            ? `（HTTP ${r.error.http_status}${r.error.provider_code ? `，错误码 ${r.error.provider_code}` : ""}）`
                            : ""}
                        </p>
                      )}
                      {(r.state === "queued" ||
                        r.state === "running" ||
                        r.state === "waiting_provider" ||
                        r.state === "waiting_response") && (
                        <div className="row">
                          <button
                            disabled={
                              busy ||
                              ["queued", "leased"].includes(r.dispatch_state)
                            }
                            onClick={() =>
                              perform(
                                () =>
                                  api(
                                    `/w/${wid}/analysis-jobs/${r.id}/resume`,
                                    {},
                                  ),
                                "任务已重新排队，原预算保持不变",
                              )
                            }
                          >
                            恢复分析
                          </button>
                          <button
                            className="secondary"
                            disabled={busy}
                            onClick={() =>
                              perform(
                                () =>
                                  api(`/w/${wid}/analyses/${r.id}/cancel`, {}),
                                "已请求停止分析",
                              )
                            }
                          >
                            停止后续角色
                          </button>
                        </div>
                      )}
                      {r.state === "waiting_response" && (
                        <button
                          className="secondary"
                          disabled={busy}
                          onClick={() =>
                            perform(
                              () =>
                                api(
                                  `/w/${wid}/analysis-jobs/${r.id}/continue`,
                                  {
                                    seconds: 300,
                                  },
                                ),
                              "续时请求已处理，原Token和费用预算保持不变",
                            )
                          }
                        >
                          追加最多5分钟并继续未完成角色
                        </button>
                      )}
                      {r.state === "waiting_provider" && (
                        <p>
                          模型服务繁忙，已保留进度。
                          {new Date(r.retry_at).toLocaleTimeString()}{" "}
                          后点击“恢复分析”。系统不会自动重发。
                        </p>
                      )}
                      {r.uncertain_usage && (
                        <p>
                          远端调用结果或用量不明，已停止自动重发。请先核对模型供应商记录。
                        </p>
                      )}
                      <p>
                        {r.steps} 个角色 · {r.tokens} Token ·{" "}
                        {r.elapsed_ms ?? 0} ms · 费用{" "}
                        {r.cost === null ? "未配置单价" : r.cost}
                      </p>
                      {r.assessments.map((a: any, i: number) => (
                        <div key={i}>
                          <b>{a.role}</b>
                          <p>{a.summary}</p>
                          {a.quantity_checks?.length > 0 && (
                            <p>
                              数量核验：
                              {a.quantity_checks
                                .map(
                                  (row: {
                                    need_index: number;
                                    allocated_quantity: number;
                                    required_quantity: number;
                                  }) =>
                                    `需求 ${row.need_index + 1} 已分配 ${row.allocated_quantity}/${row.required_quantity} 台`,
                                )
                                .join(" · ")}
                            </p>
                          )}
                          <p>
                            待确认：{a.missing_information.join("；") || "无"} ·
                            风险：{a.risks.join("；") || "无"}
                          </p>
                          <small>来源：{a.evidence_ids.join(", ")}</small>
                        </div>
                      ))}
                    </div>
                  ))}
                  {data.run?.map((r) => (
                    <div className="list" key={r.id}>
                      <b>
                        {r.state} · {r.completed.join(" → ")}
                      </b>
                      <p>
                        步骤 {r.steps}/{r.max_steps} · 工具 {r.tool_calls} 次 ·{" "}
                        {r.mode} · 缓存命中 {r.cache_hits || 0} 次
                      </p>
                      <button
                        className="secondary"
                        disabled={busy || r.state === "completed"}
                        onClick={() =>
                          perform(() =>
                            api(`/w/${wid}/runs/${r.id}/resume`, {}),
                          )
                        }
                      >
                        从检查点恢复
                      </button>
                      <RunTrace
                        url={`/api/w/${wid}/runs/${r.id}/events`}
                        token={token}
                        steps={r.steps}
                      />
                    </div>
                  ))}
                </article>
                <article>
                  <h2>操作审计</h2>
                  {[...(data.audit || [])]
                    .reverse()
                    .slice(0, 50)
                    .map((a) => (
                      <div className="audit" key={a.id}>
                        <span>
                          {new Date(a.created_at).toLocaleTimeString()}
                        </span>
                        <b>{a.action}</b>
                        <small>
                          {a.actor} · {a.resource.slice(0, 8)}
                        </small>
                      </div>
                    ))}
                </article>
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}
function Receipt({
  disabled,
  remaining,
  onSubmit,
}: {
  disabled: boolean;
  remaining: number;
  onSubmit: (n: number, a: number, note: string) => void;
}) {
  const [n, setN] = useState(1),
    [a, setA] = useState(1),
    [note, setNote] = useState("");
  return (
    <form
      className="row"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(n, a, note);
      }}
    >
      <label>
        本次到货
        <input
          type="number"
          min="1"
          max={Math.max(1, remaining)}
          value={n}
          onChange={(e) => setN(+e.target.value)}
        />
      </label>
      <label>
        合格数量
        <input
          type="number"
          min="0"
          max={n}
          value={a}
          onChange={(e) => setA(+e.target.value)}
        />
      </label>
      <label>
        验收备注
        <input value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <button disabled={disabled}>登记验收</button>
    </form>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
