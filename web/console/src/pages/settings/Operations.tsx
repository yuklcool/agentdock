import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useAuth } from "../../auth/useAuth";
import { Button } from "../../ui/Button";
import { Input } from "../../ui/inputs";
import { Field } from "../../ui/Field";
import { Avatar } from "../../ui/Avatar";
import { Pill } from "../../ui/Pill";
import { Dropdown } from "../../ui/Dropdown";
import { EmptyRow } from "../../ui/EmptyState";
import { useToast } from "../../components/Toast";
import "./Operations.css";

type Policy = Record<string, number>;
const groups = [
  { title: "实例限制", description: "限制每位用户持有的私有实例数量。", fields: [
    ["max_private_containers_per_user", "每人最多私有实例数"],
  ] },
  { title: "工作空间每日限额", description: "整个工作空间共用的每日预算。", fields: [
    ["daily_task_limit", "工作空间每日任务数"], ["daily_token_budget", "工作空间每日 Token 预算"],
  ] },
  { title: "单用户每日限额", description: "每位用户在此工作空间的每日预算。", fields: [
    ["user_daily_task_limit", "每人每日任务数"], ["user_daily_token_budget", "每人每日 Token 预算"],
  ] },
];
export type AuditEvent = {
  id: number; ts: string; action: string; action_label: string;
  actor_type: string; actor_id: string | null;
  actor: { id: string | null; name: string; account: string | null; email: string | null };
  target: { id: string | null; type: string | null; type_label: string; name: string };
  container: { id: string; name: string } | null; tenant_id: string;
  status: "success" | null; status_label: string;
};

function timeLabel(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间不可用";
  const time = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  return date.toDateString() === new Date().toDateString() ? `今天 ${time}` :
    `${date.toLocaleDateString("zh-CN")} ${time}`;
}

function AuditDetails({ event, onClose }: { event: AuditEvent; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => previous?.focus();
  }, []);
  return createPortal(<div className="operations-overlay" onMouseDown={(e) => {
    if (e.target === e.currentTarget) onClose();
  }}>
    <div className="card operations-dialog" role="dialog" aria-modal="true" aria-labelledby="audit-detail-title"
      ref={panel} tabIndex={-1} onKeyDown={(e) => {
        if (e.key === "Escape") { e.stopPropagation(); onClose(); }
        if (e.key === "Tab") {
          // The close button is the only interactive element in this read-only dialog.
          e.preventDefault(); panel.current?.querySelector<HTMLButtonElement>("button")?.focus();
        }
      }}>
      <div className="operations-header"><h2 id="audit-detail-title">操作详情</h2>
        <Button size="sm" onClick={onClose}>关闭</Button></div>
      <dl className="operations-details">
        <dt>操作</dt><dd>{event.action_label}</dd>
        <dt>操作者</dt><dd>{event.actor.name}<small>{event.actor.account ?? event.actor.email ?? "—"}</small></dd>
        <dt>对象</dt><dd>{event.target.name}</dd>
        <dt>时间</dt><dd>{new Date(event.ts).toLocaleString("zh-CN")}</dd>
        <dt>状态</dt><dd>{event.status_label}</dd>
      </dl>
      <h3>技术信息</h3>
      <dl className="operations-details operations-technical">
        <dt>记录 ID</dt><dd>{event.id}</dd>
        <dt>Actor ID</dt><dd>{event.actor_id ?? "—"}</dd>
        <dt>Actor Type</dt><dd>{event.actor_type}</dd>
        <dt>Target ID</dt><dd>{event.target.id ?? "—"}</dd>
        <dt>Target Type</dt><dd>{event.target.type ?? "—"}</dd>
        <dt>Container ID</dt><dd>{event.container?.id ?? "—"}</dd>
        <dt>Tenant ID</dt><dd>{event.tenant_id}</dd>
        <dt>原始 Action</dt><dd>{event.action}</dd>
      </dl>
    </div>
  </div>, document.body);
}

export default function Operations() {
  const { user } = useAuth();
  const tenantId = user?.active_tenant_id ?? user?.tenant_id ?? "current";
  // A workspace switch must discard drafts, filters and an open audit detail.
  return <OperationsPage key={tenantId} tenantId={tenantId} />;
}

function OperationsPage({ tenantId }: { tenantId: string }) {
  const policy = useQuery({ queryKey: ["operations", tenantId, "policy"], queryFn: () => api.get<Policy>("/v1/operations/policy") });
  const audit = useQuery({ queryKey: ["operations", tenantId, "audit"], queryFn: () => api.get<{ events: AuditEvent[] }>("/v1/operations/audit") });
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<AuditEvent | null>(null);
  const toast = useToast();
  const qc = useQueryClient();
  const events = audit.data?.events ?? [];
  const actorKey = (event: AuditEvent) => event.actor_id ?? `type:${event.actor_type}`;
  const actions = Array.from(new Map(events.map(e => [e.action, e.action_label])));
  const actors = Array.from(new Map(events.map(e => [actorKey(e), e.actor])));
  const query = search.trim().toLocaleLowerCase();
  const visible = events.filter(e => (!action || e.action === action) && (!actor || actorKey(e) === actor) &&
    (!query || [e.target.name, e.target.type_label, e.container?.name].some(v => v?.toLocaleLowerCase().includes(query))));
  const dirty = Object.keys(draft).some(key => Number(draft[key]) !== policy.data?.[key]);
  async function save() {
    if (!policy.data || saving) return;
    setSaving(true);
    const current = { ...policy.data, ...Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, Number(v)])) };
    try {
      await api.put("/v1/operations/policy", current);
      await qc.invalidateQueries({ queryKey: ["operations", tenantId] });
      setDraft({});
      toast.success("配额已保存");
    } catch {
      toast.error("保存失败，请检查输入或稍后重试");
    } finally { setSaving(false); }
  }
  return <div className="page operations-page">
    <div className="page-title operations-header">
      <div><h1>配额与审计</h1><p>管理工作空间和用户的资源使用限制，并查看关键操作记录。</p></div>
      <Button variant="primary" size="sm" form="operations-policy" type="submit" disabled={saving || !dirty || !policy.data}>
        {saving ? "保存中…" : "保存更改"}
      </Button>
    </div>
    {policy.isError && <div className="card" role="alert">无法加载配额。<Button size="sm" onClick={() => void policy.refetch()}>重试配额</Button></div>}
    {policy.isLoading && <div className="card" role="status">正在加载配额…</div>}
    {policy.data && <form id="operations-policy" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <div className="operations-quota-grid">
        {groups.map(group => <section key={group.title} className="card form-card">
          <h2>{group.title}</h2><p className="form-card-sub">{group.description}</p>
          <div className="operations-fields">{group.fields.map(([key, label]) => {
            const instance = key === "max_private_containers_per_user";
            const value = draft[key] ?? (policy.data![key] === 0 ? "" : String(policy.data![key]));
            return <Field key={key} label={label} htmlFor={key}
              hint={instance ? "1–1000 个；暂停和归档的实例也计入。" : Number(value) === 0 ? "不限额（保存值为 0）" : "填 0 或留空表示不限额。"}>
              <Input id={key} type="number" required={instance} min={instance ? 1 : 0} disabled={saving}
                max={instance ? 1000 : key.endsWith("task_limit") ? 1e9 : 1e12}
                placeholder={instance ? undefined : "不限额"} step={1} value={value}
                onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} />
            </Field>;
          })}</div>
        </section>)}
      </div>
      <details className="operations-hint"><summary>每日限额如何计算？</summary>
        <p>每日用量按 UTC 自然日重置。执行中的任务会预留 Token 上限；限额用于控制新任务准入，实际用量以模型返回为准。</p>
      </details>
    </form>}
    <section className="card flush">
      <div className="card-head"><h2>最近操作</h2><span className="spacer operations-muted">最近 100 条内筛选 · {visible.length} / {events.length} 条</span>
        <Button size="sm" disabled={audit.isFetching} onClick={() => void audit.refetch()}>刷新</Button></div>
      <div className="operations-filters">
        <Dropdown aria-label="筛选操作" value={action} onChange={setAction} options={[{ value: "", label: "全部操作" }, ...actions.map(([value, label]) => ({ value, label }))]} portal width={180} />
        <Dropdown aria-label="筛选操作者" value={actor} onChange={setActor} options={[{ value: "", label: "全部用户" }, ...actors.map(([value, person]) => ({ value, label: [person.name, person.account ?? person.email].filter(Boolean).join(" · ") }))]} portal width={220} />
        <Input aria-label="搜索资源" placeholder="搜索资源名称…" value={search} onChange={e => setSearch(e.target.value)} />
        {(action || actor || search) && <Button size="sm" variant="ghost" onClick={() => { setAction(""); setActor(""); setSearch(""); }}>清除筛选</Button>}
      </div>
      <div className="tbl-wrap"><table className="tbl operations-table">
        <thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>对象</th><th>状态</th><th><span className="sr-only">详情</span></th></tr></thead>
        <tbody>
          {audit.isError ? <EmptyRow colSpan={6} title="无法加载审计记录" description="请稍后重试。" actions={<Button size="sm" onClick={() => void audit.refetch()}>重试审计</Button>} /> :
          audit.isLoading ? <EmptyRow colSpan={6} title="正在加载操作记录…" /> :
          visible.length === 0 ? <EmptyRow colSpan={6} icon="History" title={events.length ? "没有匹配的操作" : "暂无操作记录"} description={events.length ? "试试其他筛选条件。" : "工作空间中的关键操作会显示在这里。"} /> :
          visible.map(event => <tr key={event.id} onClick={() => setSelected(event)} className="operations-audit-row">
            <td><time dateTime={event.ts} title={new Date(event.ts).toLocaleString("zh-CN")}>{timeLabel(event.ts)}</time></td>
            <td><div className="operations-person"><Avatar name={event.actor.name} size={32} /><div><strong>{event.actor.name}</strong><small>{event.actor.account ?? event.actor.email ?? (event.actor_type === "system" ? "自动操作" : "—")}</small></div></div></td>
            <td>{event.action_label}</td>
            <td><strong>{event.target.name}</strong><small>{event.target.type_label}</small></td>
            <td><Pill tone={event.status === "success" ? "success" : "dormant"}>{event.status_label}</Pill></td>
            <td><Button size="sm" variant="ghost" aria-label={`查看${event.action_label}详情`} onClick={(e) => { e.stopPropagation(); setSelected(event); }}>详情</Button></td>
          </tr>)}
        </tbody>
      </table></div>
    </section>
    {selected && <AuditDetails event={selected} onClose={() => setSelected(null)} />}
  </div>;
}
