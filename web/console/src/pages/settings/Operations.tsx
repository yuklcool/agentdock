import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Button } from "../../ui/Button";
import { Input } from "../../ui/inputs";
import { Field } from "../../ui/Field";
import { useToast } from "../../components/Toast";

type Policy = Record<string, number>;
const fields: Record<string, string> = {
  max_private_containers_per_user: "每人最多私有实例数",
  daily_token_budget: "工作空间每日 Token 预算",
  daily_task_limit: "工作空间每日任务数",
  user_daily_token_budget: "每人每日 Token 预算",
  user_daily_task_limit: "每人每日任务数",
};
type AuditEvent = { id: number; ts: string; action: string; actor_id: string | null; target_id: string | null };

export default function Operations() {
  const policy = useQuery({ queryKey: ["operations", "policy"], queryFn: () => api.get<Policy>("/v1/operations/policy") });
  const audit = useQuery({ queryKey: ["operations", "audit"], queryFn: () => api.get<{ events: AuditEvent[] }>("/v1/operations/audit") });
  const [draft, setDraft] = useState<Policy | null>(null);
  const [saving, setSaving] = useState(false);
  const toast = useToast();
  const qc = useQueryClient();
  const current = draft ?? policy.data;
  async function save() {
    if (!current) return;
    setSaving(true);
    try {
      await api.put("/v1/operations/policy", current);
      await qc.invalidateQueries({ queryKey: ["operations"] });
      setDraft(null);
      toast.success("配额已保存");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally { setSaving(false); }
  }
  return <div className="page">
    <h1>配额与审计</h1>
    <p>按 UTC 自然日计算。每日预算填 0 表示不限额；正在执行的任务会预留其 Token 上限。预算控制新任务准入，实际用量以模型返回为准。</p>
    {policy.error && <p role="alert">无法加载配额，请重试。</p>}
    {current && <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      {Object.entries(fields).map(([key, label]) => <Field key={key} label={label}>
        <Input type="number" required min={key === "max_private_containers_per_user" ? 1 : 0}
          max={key === "max_private_containers_per_user" ? 1000 : key.endsWith("task_limit") ? 1e9 : 1e12}
          step={1} value={current[key]} onChange={(event) => setDraft({ ...current, [key]: Number(event.target.value) })} />
      </Field>)}
      <Button type="submit" disabled={saving}>{saving ? "保存中…" : "保存配额"}</Button>
    </form>}
    <h2>最近审计记录</h2>
    {audit.error && <p role="alert">无法加载审计记录。</p>}
    <table><thead><tr><th>时间</th><th>操作</th><th>操作者</th><th>对象</th></tr></thead>
      <tbody>{audit.data?.events.map((event) => <tr key={event.id}>
        <td>{new Date(event.ts).toLocaleString()}</td><td>{event.action}</td>
        <td>{event.actor_id ?? "系统"}</td><td>{event.target_id ?? "—"}</td>
      </tr>)}</tbody>
    </table>
  </div>;
}
