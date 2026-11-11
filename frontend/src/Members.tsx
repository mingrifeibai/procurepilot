/** 管理员发放工作区访问凭据。新令牌只在发放成功后显示一次。 */
import { useEffect, useState } from "react";

type Role = "buyer" | "approver" | "viewer" | "admin";
type Member = {
  id: string;
  name: string;
  role: Role;
  revoked_at: string | null;
};
type Api = (path: string, body?: unknown, method?: string) => Promise<any>;

const roles: Record<Role, string> = {
  buyer: "采购员",
  approver: "审批人",
  viewer: "只读成员",
  admin: "组织管理员",
};

export function Members({
  endpoint,
  api,
  scope = "工作区",
  canManageAdmin = false,
}: {
  endpoint: string;
  api: Api;
  scope?: string;
  canManageAdmin?: boolean;
}) {
  const [members, setMembers] = useState<Member[]>([]);
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [issued, setIssued] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    api(endpoint)
      .then((rows) => {
        if (active) setMembers(rows);
      })
      .catch((failure) => {
        if (active) setError(String(failure));
      });
    return () => {
      active = false;
    };
  }, [endpoint, api]);

  async function change(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (failure) {
      setError(String(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <article>
      <h2>{scope}成员</h2>
      {error && <p role="alert">{error}</p>}
      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          void change(async () => {
            const { token, ...member } = await api(endpoint, {
              name: name.trim(),
              role,
            });
            setMembers((rows) => [...rows, member]);
            setIssued(token);
            setName("");
          });
        }}
      >
        <label>
          成员名称
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={80}
            required
          />
        </label>
        <label>
          {scope}角色
          <select
            value={role}
            onChange={(event) => setRole(event.target.value as Role)}
          >
            {Object.entries(roles)
              .filter(([id]) => id !== "admin" || canManageAdmin)
              .map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
          </select>
        </label>
        <button disabled={busy || !!issued || !name.trim()}>
          发放访问令牌
        </button>
      </form>
      {issued && (
        <div className="notice" role="status">
          <label>
            请复制访问令牌。关闭后无法再次查看。
            <input
              readOnly
              value={issued}
              onFocus={(event) => event.target.select()}
            />
          </label>
          <button className="secondary" onClick={() => setIssued("")}>
            关闭
          </button>
        </div>
      )}
      {members.map((member) => (
        <div className="list" key={member.id}>
          <b>
            {member.name} · {roles[member.role]}
          </b>
          {member.revoked_at ? (
            <p>已撤销</p>
          ) : member.role !== "admin" || canManageAdmin ? (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => {
                if (!window.confirm(`撤销 ${member.name} 的${scope}令牌？`))
                  return;
                void change(async () => {
                  const updated = await api(
                    `${endpoint}/${member.id}`,
                    undefined,
                    "DELETE",
                  );
                  setMembers((rows) =>
                    rows.map((row) => (row.id === member.id ? updated : row)),
                  );
                });
              }}
            >
              撤销令牌
            </button>
          ) : null}
        </div>
      ))}
    </article>
  );
}
