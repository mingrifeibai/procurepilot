/** 附件只作为数据上传，读取原文使用分页接口，避免将全文塞进模型上下文。 */
import { useEffect, useState } from "react";
type Api = (path: string, body?: unknown) => Promise<any>;
type QuoteRow = {
  row: number;
  fields: Record<string, string>;
  missing: string[];
  errors: string[];
  normalized: Record<string, unknown> | null;
};

/** 页面只展示命中附近；回读接口仍使用服务端的原始字符偏移。 */
function excerpt(hit: any) {
  const terms: string[] = [...(hit.matched_terms || [])].sort(
    (a, b) => b.length - a.length,
  );
  const term = terms.find(
    (t) => t.length > 1 && hit.snippet.toLowerCase().includes(t.toLowerCase()),
  );
  const position = term
    ? hit.snippet.toLowerCase().indexOf(term.toLowerCase())
    : 0;
  const start = Math.max(0, position - 80);
  const end = Math.min(hit.snippet.length, position + 260);
  return (
    (start ? "…" : "") +
    hit.snippet.slice(start, end) +
    (end < hit.snippet.length ? "…" : "")
  );
}
export function Evidence({
  wid,
  api,
  perform,
  downloadOriginal,
}: {
  wid: string;
  api: Api;
  perform: (a: () => Promise<unknown>, message?: string) => Promise<void>;
  downloadOriginal: (id: string, filename: string) => Promise<void>;
}) {
  const [files, setFiles] = useState<any[]>([]),
    [query, setQuery] = useState(""),
    [hits, setHits] = useState<any[]>([]),
    [preview, setPreview] = useState("");
  const [indexJobs, setIndexJobs] = useState<any[]>([]);
  const [searchStatus, setSearchStatus] = useState<any>(null);
  const [indexError, setIndexError] = useState("");
  const [useOcr, setUseOcr] = useState(false);
  const [quotePreview, setQuotePreview] = useState<{
    id: string;
    name: string;
    rows: QuoteRow[];
  } | null>(null);
  const [selectedRows, setSelectedRows] = useState<number[]>([]);
  async function load() {
    setFiles(await api(`/w/${wid}/attachments`));
  }
  useEffect(() => {
    let active = true;
    setFiles([]);
    setHits([]);
    setPreview("");
    setIndexJobs([]);
    setQuotePreview(null);
    setSelectedRows([]);
    async function refreshIndex() {
      try {
        const [jobs, status] = await Promise.all([
          api(`/w/${wid}/evidence/index`),
          api("/search-status"),
        ]);
        if (active) {
          setIndexJobs(jobs);
          setSearchStatus(status);
          setIndexError("");
        }
      } catch (e) {
        if (active) setIndexError(String(e));
      }
    }
    perform(async () => {
      const files = await api(`/w/${wid}/attachments`);
      if (active) setFiles(files);
      await refreshIndex();
    });
    const timer = setInterval(refreshIndex, 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [wid, api]);
  async function upload(file: File) {
    if (file.size > 2000000) throw Error("附件最大 2 MB");
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (const b of bytes) binary += String.fromCharCode(b);
    await api(`/w/${wid}/attachments`, {
      filename: file.name,
      content_base64: btoa(binary),
      ocr: useOcr,
    });
    await load();
  }
  return (
    <>
      <article>
        <h2>工作证据库</h2>
        <p>
          检索方式：
          {searchStatus?.backend === "opensearch"
            ? "全文与本地中文向量混合召回"
            : "本地词法检索"}
        </p>
        <p>
          支持
          TXT、Markdown、CSV、PDF、XLSX；扫描PDF和图片可勾选本机OCR，最多5页。识别结果请核对后再录入报价。
        </p>
        <label>
          <input
            type="checkbox"
            checked={useOcr}
            onChange={(e) => setUseOcr(e.target.checked)}
          />
          扫描件使用本机OCR
        </label>
        <input
          aria-label="上传附件"
          type="file"
          accept=".txt,.md,.csv,.pdf,.xlsx,.png,.jpg,.jpeg,.webp"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) perform(() => upload(f), "附件已保存");
            e.target.value = "";
          }}
        />
        {searchStatus?.backend === "opensearch" && (
          <div className="list">
            <button
              className="secondary"
              onClick={() =>
                perform(async () => {
                  await api(`/w/${wid}/evidence/index`, {});
                  setIndexJobs(await api(`/w/${wid}/evidence/index`));
                }, "索引构建已提交后台")
              }
            >
              构建或更新检索索引
            </button>
            <p>向量计算在本机CPU进行。上传或修改附件后需要更新索引。</p>
            {indexJobs.slice(-3).map((job) => (
              <p key={job.id}>
                索引 {job.stale ? "附件已变化" : job.state} ·{" "}
                {job.completed_chunks} / {job.total_chunks ?? "待计算"} 段
                {job.error_type ? ` · ${job.error_type}` : ""}
              </p>
            ))}
          </div>
        )}
        {indexError && <p role="alert">{indexError}</p>}
        <div className="row" style={{ marginTop: 20 }}>
          <input
            placeholder="检索参数、条款或供应商"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button
            onClick={() =>
              perform(async () =>
                setHits(
                  await api(
                    `/w/${wid}/evidence/search?query=${encodeURIComponent(query)}`,
                  ),
                ),
              )
            }
          >
            搜索证据
          </button>
        </div>
        {hits.map((h, i) => (
          <div className="list" key={i}>
            <b>
              {h.filename} · 原文字符 {h.offset}–{h.end_offset}
            </b>
            <p style={{ overflowWrap: "anywhere" }}>
              <code>{h.source}</code>
            </p>
            <p>{excerpt(h)}</p>
            <button
              className="secondary"
              onClick={() =>
                perform(async () => {
                  const result = await api(
                    `/w/${wid}/attachments/${h.artifact_id}?offset=${h.offset}&limit=1000`,
                  );
                  setPreview(result.text);
                }, "已读取命中位置原文")
              }
            >
              查看对应原文
            </button>
          </div>
        ))}
      </article>
      <article>
        <h2>已保存附件</h2>
        {files.map((f) => (
          <div className="list" key={f.id}>
            <b>{f.filename}</b>
            {f.ocr && <p>OCR识别 · {f.ocr.pages}页 · 文字与金额需核对</p>}
            <p>
              {f.characters} 字符 · {f.id}
            </p>
            {f.object && (
              <button
                className="secondary"
                onClick={() =>
                  perform(
                    () => downloadOriginal(f.id, f.filename),
                    "原始附件已下载",
                  )
                }
              >
                下载原始附件
              </button>
            )}
            <button
              className="secondary"
              onClick={() =>
                perform(async () =>
                  setPreview(
                    (await api(`/w/${wid}/attachments/${f.id}?limit=4000`))
                      .text,
                  ),
                )
              }
            >
              读取前 4000 字符
            </button>
            {f.filename.toLowerCase().endsWith(".csv") && (
              <button
                className="secondary"
                onClick={() =>
                  perform(async () => {
                    const rows = await api(
                      `/w/${wid}/attachments/${f.id}/quotes-preview`,
                    );
                    setQuotePreview({ id: f.id, name: f.filename, rows });
                    setSelectedRows([]);
                  })
                }
              >
                预览报价字段与缺项
              </button>
            )}
          </div>
        ))}
        {quotePreview && (
          <div className="list">
            <h3>报价导入预览 · {quotePreview.name}</h3>
            <p>核对报价、库存及交期后勾选要录入的行。每批最多 100 行。</p>
            <div style={{ maxHeight: 360, overflow: "auto" }}>
              {quotePreview.rows.map((row) => (
                <div key={row.row} className="list">
                  <label>
                    <input
                      type="checkbox"
                      disabled={
                        !row.normalized ||
                        (selectedRows.length >= 100 &&
                          !selectedRows.includes(row.row))
                      }
                      checked={selectedRows.includes(row.row)}
                      onChange={(event) => {
                        const checked = event.target.checked;
                        setSelectedRows((values) =>
                          checked
                            ? [...values, row.row]
                            : values.filter((id) => id !== row.row),
                        );
                      }}
                    />
                    第 {row.row} 行 · {row.fields.product_id || "未知产品"} ·{" "}
                    {row.fields.supplier || "缺供应商"}
                  </label>
                  <p>
                    单价 {row.fields.unit_price || "—"} 元 · 库存{" "}
                    {row.fields.stock || "—"} · 交期{" "}
                    {row.fields.delivery_days || "—"} 天
                  </p>
                  {row.missing.length > 0 && (
                    <small>缺少字段 {row.missing.join("、")}</small>
                  )}
                  {row.errors.length > 0 && (
                    <small>{row.errors.join("、")}</small>
                  )}
                </div>
              ))}
            </div>
            <button
              disabled={!selectedRows.length}
              onClick={() =>
                perform(async () => {
                  await api(
                    `/w/${wid}/attachments/${quotePreview.id}/quotes-import`,
                    { rows: selectedRows },
                  );
                  setSelectedRows([]);
                }, "所选报价已导入，可在产品与报价查看来源")
              }
            >
              确认导入 {selectedRows.length} 行
            </button>
          </div>
        )}
        <pre
          style={{
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            fontSize: 12,
          }}
        >
          {preview}
        </pre>
      </article>
    </>
  );
}
