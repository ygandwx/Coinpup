import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import { businessError } from "./business-errors";
import type { Locale } from "./i18n";
import type { PendingPeriodController } from "./pending-period";
import { periodReason } from "./pending-period";
import { getPeriod, periodHistory } from "./periods-api";
import type { PeriodChange, PeriodReceipt, PeriodState } from "./periods-api";

export function PeriodPanel({
    ledgerId,
    locale,
    session,
    controller,
    onUnauthorized,
}: {
    ledgerId: string;
    locale: Locale;
    session: Session;
    controller: PendingPeriodController;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const matching = pending.plan?.ledgerId === ledgerId;
    const frozen = matching ? (JSON.parse(pending.plan!.bodyJson) as PeriodChange) : null;
    const [action, setAction] = useState<PeriodChange["action"]>(frozen?.action ?? "close");
    const [cutoff, setCutoff] = useState(frozen?.closed_through ?? "");
    const [reason, setReason] = useState(frozen?.reason ?? "");
    const [offset, setOffset] = useState(0);
    const [refresh, setRefresh] = useState(0);
    const [localError, setLocalError] = useState<unknown>(null);
    const locked = !["idle", "rejected", "confirmed"].includes(pending.status);
    const conflict = matching && pending.error?.code === "version_conflict";
    const key = JSON.stringify([ledgerId, offset, refresh, matching ? pending.receipt?.id : null]);
    const [result, setResult] = useState<{
        key: string;
        state: PeriodState;
        history: PeriodReceipt[];
    } | null>(null);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const current = result?.key === key ? result : null;
    const readError = failure?.key === key ? failure.error : null;
    const unauthorized = useRef(onUnauthorized);
    unauthorized.current = onUnauthorized;
    useEffect(() => {
        const abort = new AbortController();
        Promise.all([
            getPeriod(ledgerId, abort.signal),
            periodHistory(ledgerId, offset, abort.signal),
        ])
            .then(([state, history]) => {
                if (!abort.signal.aborted) setResult({ key, state, history });
            })
            .catch((error) => {
                if (abort.signal.aborted) return;
                if (error instanceof ApiError && error.status === 401) unauthorized.current();
                else setFailure({ key, error });
            });
        return () => abort.abort();
    }, [ledgerId, offset, key]);
    useEffect(() => {
        if (pending.status === "auth-required") unauthorized.current();
    }, [pending.status]);
    async function submit(event: FormEvent) {
        event.preventDefault();
        if (!current || locked || conflict) return;
        setLocalError(null);
        try {
            await controller.start(session, ledgerId, {
                action,
                closed_through: cutoff || null,
                reason,
                expected_version: current.state.version,
            });
        } catch (error) {
            setLocalError(error);
        }
    }
    async function retry() {
        setLocalError(null);
        try {
            await controller.retry(session);
        } catch (error) {
            setLocalError(error);
        }
    }
    const open = t("未结账", "Open");
    return (
        <section
            className="business-panel period-panel"
            aria-label={t("结账与重开", "Period closing")}
        >
            <div className="section-heading">
                <h2>{t("结账与重开", "Period closing")}</h2>
                <button
                    type="button"
                    disabled={locked}
                    onClick={() => {
                        controller.dismiss();
                        setLocalError(null);
                        setRefresh((v) => v + 1);
                    }}
                >
                    {t("重新读取状态", "Reload period state")}
                </button>
            </div>
            <p className="help-text">
                {t(
                    "每个账本可选择不结账。截止日包含当天；交易日或归属日落入关闭期间的新增、更正和取消会被拒绝。需要调整时先填写理由重开。",
                    "Closing is optional for each ledger. The cutoff is inclusive: new entries, corrections and cancellations touching a closed transaction or recognition date are rejected. Reopen with a reason before making changes.",
                )}
            </p>
            {readError != null && <p role="alert">{businessError(readError, locale)}</p>}
            {!current && !readError && <p role="status">{t("正在读取期间…", "Loading period…")}</p>}
            {current && (
                <div className="summary-grid">
                    <article className="summary-card">
                        <h3>{t("已结账至", "Closed through")}</h3>
                        <strong data-testid="period-cutoff">
                            {current.state.closed_through ?? open}
                        </strong>
                        <span>
                            {t("版本", "Version")}{" "}
                            <span data-testid="period-version">{current.state.version}</span>
                        </span>
                    </article>
                    {current.state.last_reopened && (
                        <article className="summary-card">
                            <h3>{t("最近重开", "Last reopened")}</h3>
                            <p>{current.state.last_reopened.created_at}</p>
                            <p>{current.state.last_reopened.reason}</p>
                            <span>
                                {t("再次结账仍保留重开历史。", "Reclosing preserves this history.")}
                            </span>
                        </article>
                    )}
                </div>
            )}
            {matching && pending.status !== "idle" && (
                <div aria-live="polite">
                    <p role="status">
                        {pending.status === "confirmed"
                            ? t("期间变更已完成。", "Period change completed.")
                            : pending.status === "submitting"
                              ? t("正在提交，请勿关闭页面。", "Submitting; keep this page open.")
                              : pending.status === "unknown"
                                ? t(
                                      "结果未知。请重试原请求，勿重新提交。",
                                      "Outcome unknown. Retry the original request; do not submit a new change.",
                                  )
                                : pending.status === "idempotency-conflict"
                                  ? t(
                                        "原意图冲突，请保留页面并核对记录。",
                                        "Original intent conflict. Keep this page open and check records.",
                                    )
                                  : pending.status === "auth-required"
                                    ? t(
                                          "请重新登录后恢复原请求。",
                                          "Sign in again to resume the original request.",
                                      )
                                    : t(
                                          "请求已被拒绝，输入已保留。",
                                          "Request rejected; your input is preserved.",
                                      )}
                    </p>
                    {pending.status === "unknown" && (
                        <button type="button" onClick={() => void retry()}>
                            {t("重试原期间请求", "Retry original period request")}
                        </button>
                    )}
                    {conflict && (
                        <p role="alert">
                            {t(
                                "期间已被其他操作修改。请重新读取状态，输入会保留；核对后再提交。",
                                "The period changed elsewhere. Reload period state; your input will be preserved for review before resubmission.",
                            )}
                        </p>
                    )}
                    {pending.status === "rejected" && pending.error && !conflict && (
                        <p role="alert">{businessError(pending.error, locale)}</p>
                    )}
                </div>
            )}
            {localError != null && <p role="alert">{businessError(localError, locale)}</p>}
            <form onSubmit={(event) => void submit(event)}>
                <fieldset disabled={locked}>
                    <legend>{t("调整期间", "Change period")}</legend>
                    <label className="field">
                        {t("操作", "Action")}
                        <select
                            aria-label={t("操作", "Action")}
                            value={action}
                            onChange={(event) =>
                                setAction(event.target.value as PeriodChange["action"])
                            }
                        >
                            <option value="close">{t("结账", "Close")}</option>
                            <option value="reopen">{t("重开", "Reopen")}</option>
                        </select>
                    </label>
                    <label className="field">
                        {t("新的截止日", "New cutoff date")}
                        <input
                            type="date"
                            required={action === "close"}
                            value={cutoff}
                            onChange={(event) => setCutoff(event.target.value)}
                        />
                    </label>
                    {action === "reopen" && (
                        <p className="help-text">
                            {t(
                                "输入更早的截止日；留空表示解除全部关闭。",
                                "Enter an earlier cutoff; leave blank to reopen all dates.",
                            )}
                        </p>
                    )}
                    <label className="field">
                        {t("理由", "Reason")}
                        <textarea
                            aria-label={t("理由", "Reason")}
                            required
                            maxLength={1000}
                            value={reason}
                            onChange={(event) => setReason(event.target.value)}
                        />
                    </label>
                    <button
                        className="primary-button"
                        type="submit"
                        disabled={
                            !current ||
                            conflict ||
                            !periodReason(reason) ||
                            (action === "close" && !cutoff)
                        }
                    >
                        {t("提交期间变更", "Submit period change")}
                    </button>
                </fieldset>
            </form>
            <h3>{t("变更历史", "Change history")}</h3>
            {current?.history.length === 0 && (
                <p>{t("尚无期间变更。", "No period changes yet.")}</p>
            )}
            {current?.history.map((item) => (
                <article className="period-audit" data-testid="period-audit" key={item.id}>
                    <h4>
                        {item.action === "close" ? t("结账", "Close") : t("重开", "Reopen")} ·{" "}
                        {t("版本", "Version")} {item.version}
                    </h4>
                    <p>
                        {item.previous_closed_through ?? open} → {item.closed_through ?? open}
                    </p>
                    <p>{item.reason}</p>
                    <time>{item.created_at}</time>
                    <details>
                        <summary>{t("执行人与记录编号", "Actor and record ID")}</summary>
                        <p>{item.actor_id}</p>
                        <p>{item.id}</p>
                    </details>
                </article>
            ))}
            <div className="operation-actions">
                <button
                    type="button"
                    disabled={locked || offset === 0}
                    onClick={() => setOffset((v) => Math.max(0, v - 25))}
                >
                    {t("上一页", "Previous page")}
                </button>
                <button
                    type="button"
                    disabled={locked || !current || current.history.length < 25 || offset >= 100000}
                    onClick={() => setOffset((v) => v + 25)}
                >
                    {t("下一页", "Next page")}
                </button>
            </div>
        </section>
    );
}
