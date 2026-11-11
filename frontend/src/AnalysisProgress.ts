/** 带平台令牌订阅有限 SSE 流。浏览器保留最近的状态游标以便重连。 */
export async function readAnalysisProgress(
  wid: string,
  token: string,
  cursor: string,
  signal: AbortSignal,
  onConnected: () => void,
  onChange: (cursor: string) => void,
) {
  const response = await fetch(`/api/w/${wid}/analysis-events`, {
    headers: {
      Authorization: "Bearer " + token,
      Accept: "text/event-stream",
      "Last-Event-ID": cursor,
    },
    signal,
  });
  if (
    !response.ok ||
    !response.body ||
    !response.headers.get("Content-Type")?.startsWith("text/event-stream")
  ) {
    throw new Error("分析订阅不可用");
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let pending = "";
  onConnected();
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) return;
      pending += value;
      const frames = pending.split("\n\n");
      pending = frames.pop() || "";
      for (const frame of frames) {
        const match = /^id: ([0-9a-f]{64})\nevent: analysis_changed\n/m.exec(
          frame,
        );
        if (match && !signal.aborted) onChange(match[1]);
      }
      if (pending.length > 4096) throw new Error("分析订阅事件格式无效");
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
