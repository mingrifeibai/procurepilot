/** 澄清和偏好是显式用户输入；回答不会隐式更改采购硬约束。 */
import { useState, useEffect } from "react";
type Api = (path: string, body?: unknown, method?: string) => Promise<any>;
export function Dialogue({
  wid,
  api,
  perform,
}: {
  wid: string;
  api: Api;
  perform: (action: () => Promise<unknown>, notice?: string) => Promise<void>;
}) {
  const [questions, setQuestions] = useState<any[]>([]),
    [preferences, setPreferences] = useState<any[]>([]);
  const [key, setKey] = useState("交付偏好"),
    [value, setValue] = useState(""),
    [confirmed, setConfirmed] = useState(false),
    [days, setDays] = useState(30);
  async function load() {
    const [q, p] = await Promise.all([
      api(`/w/${wid}/clarifications`),
      api(`/w/${wid}/preferences`),
    ]);
    setQuestions(q);
    setPreferences(p);
  }
  useEffect(() => {
    void perform(load);
  }, [wid]);
  return (
    <div className="grid">
      <article>
        <h2>待澄清问题</h2>
        <p>
          回答作为用户说明保存。若预算、数量或硬性参数变化，请在“采购需求”中保存新版本，旧方案需要重新规划。
        </p>
        {!questions.length && (
          <p>暂无问题。分析发现关键证据缺口后，问题会自动保存在这里。</p>
        )}
        {questions.map((q) => (
          <div className="list" key={q.id}>
            <b>{q.question}</b>
            <p>
              {q.stale
                ? "已属于旧需求版本"
                : q.state === "answered"
                  ? "已回答"
                  : "待回答"}{" "}
              · {q.asked_by}
            </p>
            {q.state === "answered" ? (
              <p>{q.answer}</p>
            ) : (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  const answer = new FormData(e.currentTarget).get("answer");
                  void perform(async () => {
                    await api(`/w/${wid}/clarifications/${q.id}/answer`, {
                      answer,
                    });
                    await load();
                  }, "回答已保存，可基于当前需求重新分析");
                }}
              >
                <label>
                  你的说明
                  <textarea
                    name="answer"
                    required
                    maxLength={2000}
                    disabled={q.stale}
                  />
                </label>
                <button disabled={q.stale}>提交回答</button>
              </form>
            )}
          </div>
        ))}
      </article>
      <article>
        <h2>确认偏好</h2>
        <p>
          偏好只在当前工作区使用，不能覆盖当前需求的明确预算、数量和硬约束。停用后不再用于新分析，审计历史保留。
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void perform(async () => {
              await api(`/w/${wid}/preferences`, {
                key,
                value,
                confirmed,
                expires_in_days: days,
              });
              setValue("");
              setConfirmed(false);
              await load();
            }, "已保存偏好新版本");
          }}
        >
          <label>
            偏好名称
            <input
              value={key}
              required
              maxLength={100}
              onChange={(e) => setKey(e.target.value)}
            />
          </label>
          <label>
            偏好内容
            <input
              value={value}
              required
              maxLength={1000}
              onChange={(e) => setValue(e.target.value)}
            />
          </label>
          <label>
            有效天数
            <input
              type="number"
              min={1}
              max={3650}
              value={days}
              onChange={(e) => setDays(+e.target.value)}
            />
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            我确认保存此偏好
          </label>
          <button disabled={!confirmed}>保存偏好新版本</button>
        </form>
        {preferences.map((p) => (
          <div className="list" key={p.id}>
            <b>
              {p.key} · v{p.version || 1}
            </b>
            <p>
              {p.value} ·{" "}
              {
                (
                  {
                    active: "生效中",
                    inactive: "已停用",
                    expired: "已过期",
                  } as Record<string, string>
                )[p.state]
              }
            </p>
            {p.state === "active" && (
              <button
                className="secondary"
                onClick={() =>
                  perform(async () => {
                    await api(
                      `/w/${wid}/preferences/${p.id}`,
                      undefined,
                      "DELETE",
                    );
                    await load();
                  }, "偏好已停用")
                }
              >
                停用偏好
              </button>
            )}
          </div>
        ))}
      </article>
    </div>
  );
}
