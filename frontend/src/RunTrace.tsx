/** 按需回放一次采购规划已提交的 LangGraph 节点。 */
import { useEffect, useState } from "react";

type Trace = { id: string; stage: string; created_at: string };
const stageNames: Record<string, string> = {
  clarify: "澄清需求",
  source: "读取报价",
  plan: "生成方案",
  verify: "核验方案",
};

export function RunTrace({
  url,
  token,
  steps,
}: {
  url: string;
  token: string;
  steps: number;
}) {
  const [open, setOpen] = useState(false);
  const [traces, setTraces] = useState<Trace[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setTraces(null);
    setError(false);
    async function replay() {
      const response = await fetch(url, {
        headers: {
          Authorization: "Bearer " + token,
          Accept: "text/event-stream",
        },
        signal: controller.signal,
        cache: "no-store",
      });
      if (!response.ok) throw Error("无法加载轨迹");
      // 服务端返回有限 SSE，结束后一次性展示；节点步数变化时重新回放。
      const body = await response.text();
      const events = body
        .trimEnd()
        .split(/\r?\n\r?\n/)
        .flatMap((frame) => {
          const lines = frame.split(/\r?\n/);
          if (!lines.includes("event: trace")) return [];
          const json = lines
            .filter((line) => line.startsWith("data: "))
            .map((line) => line.slice(6))
            .join("\n");
          return [JSON.parse(json) as Trace];
        });
      if (!controller.signal.aborted) setTraces(events);
    }
    void replay().catch(() => {
      if (!controller.signal.aborted) setError(true);
    });
    return () => controller.abort();
  }, [open, steps, url, token]);

  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>查看运行轨迹</summary>
      {open && error && (
        <p role="alert">轨迹加载失败，请确认访问权限后重新展开。</p>
      )}
      {open && !error && traces === null && <p role="status">正在加载轨迹</p>}
      {open && traces?.length === 0 && <p>还没有已提交的节点。</p>}
      {open && traces && traces.length > 0 && (
        <ol aria-label="已提交的规划节点">
          {traces.map((event) => (
            <li key={event.id}>
              {stageNames[event.stage] || event.stage} ·{" "}
              <time dateTime={event.created_at}>
                {new Date(event.created_at).toLocaleTimeString()}
              </time>
            </li>
          ))}
        </ol>
      )}
    </details>
  );
}
