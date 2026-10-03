import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import { businessError } from "./business-errors";
import type { Locale } from "./i18n";
import { listOperations } from "./ledger-api";
import type { Account, Asset, Category, Entity, FinancialResponse, OperationState } from "./ledger-api";
import { PostingForm } from "./PostingForm";
import type { PendingCommandController, PostingInput } from "./pending-command";

export function operationName(kind: FinancialResponse["kind"], locale: Locale): string {
  const names = { opening: ["期初", "Opening balance"], income: ["收入", "Income"], expense: ["支出", "Expense"], transfer: ["转账", "Transfer"], exchange: ["换汇", "Exchange"] };
  return names[kind][locale === "zh" ? 0 : 1];
}

function OperationCard({ operation, accounts, categories, locale }: {
  operation: OperationState; accounts: Account[]; categories: Category[]; locale: Locale;
}) {
  const t = (zh: string, en: string) => locale === "zh" ? zh : en;
  const posting = operation.latest_posting;
  const account = (id: string) => accounts.find(item => item.id === id)?.name ?? id;
  const category = (id: string) => {
    const item = categories.find(value => value.id === id);
    return item ? (locale === "en" ? item.name_en || item.name : item.name) : id;
  };
  return <article className={`operation-card ${operation.status}`} data-testid={`operation-${operation.id}`}>
    <div className="record-heading"><h3>{operationName(posting.kind, locale)}</h3><span className="record-badge">{operation.status === "cancelled" ? t("已取消", "Cancelled") : t("有效", "Active")} · v{operation.version}</span></div>
    <p className="help-text"><time dateTime={posting.transaction_date}>{posting.transaction_date}</time>{posting.recognition_date !== posting.transaction_date && <> · {t("业务归属日", "Recognition date")}: {posting.recognition_date}</>}</p>
    {posting.description && <p className="operation-description">{posting.description}</p>}
    <dl className="operation-quantities">
      {posting.kind === "exchange" ? <><div><dt>{t("转出", "From")} · {account(posting.source_account_id)} · {posting.source_asset_id}</dt><dd tabIndex={0}>{posting.source_amount}</dd></div><div><dt>{t("转入", "To")} · {account(posting.destination_account_id)} · {posting.destination_asset_id}</dt><dd tabIndex={0}>{posting.destination_amount}</dd></div></> : posting.kind === "transfer" ? <div><dt>{account(posting.source_account_id)} → {account(posting.destination_account_id)} · {posting.asset_id}</dt><dd tabIndex={0}>{posting.amount}</dd></div> : <div><dt>{account(posting.account_id)} · {posting.asset_id}</dt><dd tabIndex={0}>{posting.amount}</dd></div>}
    </dl>
    {"splits" in posting && posting.splits.length > 0 && <ul className="operation-breakdown" aria-label={t("分类拆分", "Category splits")}>{posting.splits.map(split => <li key={split.category_id}><span>{category(split.category_id)}</span><span className="money-quantity" tabIndex={0}>{split.amount}</span></li>)}</ul>}
    {!!posting.fees?.length && <details className="operation-fees"><summary>{t("手续费", "Fees")} ({posting.fees.length})</summary><ul className="operation-breakdown">{posting.fees.map((fee, index) => <li key={index}><span>{account(fee.account_id)} · {category(fee.category_id)} · {fee.asset_id}</span><span className="money-quantity" tabIndex={0}>{fee.amount}</span></li>)}</ul></details>}
    {operation.status === "cancelled" && <p className="help-text">{t("取消原因", "Cancellation reason")}: {operation.cancellation.reason}</p>}
  </article>;
}

export function TransactionsPanel({ session, locale, entity, accounts, assets, categories, controller, dataLoading, onChanged, onUnauthorized, onEditingChange }: {
  session: Session; locale: Locale; entity: Entity; accounts: Account[]; assets: Asset[]; categories: Category[];
  controller: PendingCommandController; dataLoading: boolean; onChanged: () => void; onUnauthorized: () => void; onEditingChange: (value: boolean) => void;
}) {
  const t = (zh: string, en: string) => locale === "zh" ? zh : en;
  const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const [editing, setEditing] = useState(false);
  const [operations, setOperations] = useState<OperationState[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState<"all" | "active" | "cancelled">("all");
  const [refresh, setRefresh] = useState(0);
  const [saved, setSaved] = useState(false);
  const callbacks = useRef({ onChanged, onUnauthorized, onEditingChange });
  callbacks.current = { onChanged, onUnauthorized, onEditingChange };
  const locked = !["idle", "rejected", "confirmed"].includes(pending.status);
  const commandHere = pending.command?.ledgerId === entity.ledger.id;
  const handled = useRef<string | null>(null);

  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setLoadError(null);
    void listOperations(entity.ledger.id, { status, offset, limit: 25 }, abort.signal).then(items => {
      if (abort.signal.aborted) return;
      setOperations(items); setLoading(false);
    }).catch(error => {
      if (abort.signal.aborted) return;
      setLoading(false); setOperations([]);
      if (error instanceof ApiError && error.kind === "unauthorized") callbacks.current.onUnauthorized();
      else setLoadError(error);
    });
    return () => abort.abort();
  }, [entity.ledger.id, status, offset, refresh]);

  useEffect(() => { callbacks.current.onEditingChange(editing); return () => callbacks.current.onEditingChange(false); }, [editing]);
  useEffect(() => {
    if (editing) document.getElementById("posting-panel-title")?.focus();
  }, [editing]);
  useEffect(() => {
    if (pending.status === "auth-required") { callbacks.current.onUnauthorized(); return; }
    if (pending.status !== "confirmed" || !commandHere || !pending.command || handled.current === pending.command.key) return;
    handled.current = pending.command.key;
    setEditing(false); setSaved(true); setStatus("all"); setOffset(0); setRefresh(value => value + 1);
    callbacks.current.onChanged();
    controller.dismiss();
    requestAnimationFrame(() => document.getElementById("new-entry-button")?.focus());
  }, [pending, commandHere, controller]);

  function closeForm() {
    if (locked) return;
    controller.dismiss(); setEditing(false);
    requestAnimationFrame(() => document.getElementById("new-entry-button")?.focus());
  }
  async function submit(input: PostingInput) {
    if (locked || entity.archived || dataLoading) return;
    setSaved(false);
    if (pending.status === "rejected") controller.dismiss();
    await controller.submit(session, entity.ledger.id, input);
  }
  const unknown = pending.status === "unknown" || pending.status === "idempotency-conflict";
  return <section className="transactions-panel">
    {saved && <p className="save-notice" role="status">{t("记录已保存。", "Entry saved.")}</p>}
    {commandHere && (unknown || pending.status === "submitting") && <div className="business-panel pending-command" role="status">
      <h2>{pending.status === "submitting" ? t("正在保存…", "Saving…") : t("保存结果待确认", "Save not confirmed")}</h2>
      <p>{pending.status === "idempotency-conflict" ? t("服务器收到同一标识的不同内容。请先核对原记录，当前提交已锁定。", "The server received different content for this submission. Check the original entry before continuing.") : t("已保留原始内容。重试会使用同一个提交标识，不会重复记账。", "The original content is retained. Retrying uses the same submission identifier and will not post it twice.")}</p>
      <p className="help-text">{t("刷新或关闭网页会丢失待确认内容；如已离开，请先核对流水再新增。", "Reloading or closing this page loses the pending content. If you leave, check transactions before creating another entry.")}</p>
      <div className="form-actions"><button className="secondary-button" disabled={pending.status === "submitting" || pending.checking} onClick={() => void controller.reconcile(session)}>{pending.checking ? t("核对中…", "Checking…") : t("核对记录", "Check entry status")}</button><button className="primary-button" disabled={pending.status === "submitting" || pending.status === "idempotency-conflict" || pending.checking} onClick={() => void controller.retry(session)}>{t("重试原提交", "Retry original submission")}</button></div>
    </div>}
    {editing ? <section className="business-panel form-panel"><h2 id="posting-panel-title" tabIndex={-1}>{t("新增记录", "New entry")}</h2><PostingForm locale={locale} accounts={accounts} assets={assets} categories={categories} busy={locked || dataLoading} error={pending.status === "rejected" ? businessError(pending.error, locale) : undefined} onSubmit={input => void submit(input)} onCancel={closeForm} /></section> : <>
      <section className="business-panel">
        <div className="section-heading"><div><h2>{t("资金流水", "Transactions")}</h2><p className="help-text">{t("原币本金和手续费分别展示。期初与转账本金不计入收支。", "Principal and fees retain their original assets. Opening balances and transfer principal are separate from income and expenses.")}</p></div><button id="new-entry-button" className="primary-button" disabled={entity.archived || dataLoading || locked} onClick={() => { setEditing(true); setSaved(false); controller.dismiss(); }}>{t("新增记录", "New entry")}</button></div>
        <div className="transaction-toolbar"><div className="field"><label htmlFor="transaction-status">{t("记录状态", "Entry status")}</label><select id="transaction-status" value={status} disabled={loading} onChange={event => { setStatus(event.target.value as typeof status); setOffset(0); }}><option value="all">{t("全部", "All")}</option><option value="active">{t("有效", "Active")}</option><option value="cancelled">{t("已取消", "Cancelled")}</option></select></div><button className="secondary-button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>{t("刷新流水", "Refresh transactions")}</button></div>
        {loadError !== null && <p role="alert" className="inline-error">{businessError(loadError, locale)}</p>}
        {loading ? <p role="status" className="help-text">{t("正在读取流水…", "Loading transactions…")}</p> : operations.length ? <div className="operations-list">{operations.map(operation => <OperationCard key={operation.id} operation={operation} accounts={accounts} categories={categories} locale={locale} />)}</div> : <p className="empty-copy">{t("此页没有记录。添加期初、收入或支出，开始记录资金变化。", "No entries on this page. Add an opening balance, income or expense to start.")}</p>}
        <nav className="transaction-pagination" aria-label={t("流水分页", "Transaction pages")}><button className="secondary-button" disabled={loading || offset === 0} onClick={() => setOffset(value => Math.max(0, value - 25))}>{t("上一页", "Previous page")}</button><span>{t("第", "Page")} {offset / 25 + 1} {locale === "zh" ? "页" : ""}</span><button className="secondary-button" disabled={loading || operations.length < 25 || offset >= 100000} onClick={() => setOffset(value => value + 25)}>{t("下一页", "Next page")}</button></nav>
      </section>
    </>}
  </section>;
}
