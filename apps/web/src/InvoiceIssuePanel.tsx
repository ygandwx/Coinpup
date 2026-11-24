import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import type { Asset } from "./ledger-api";
import { getDocumentCurrent } from "./document-revision-api";
import { checkedDocumentCurrent } from "./document-revision-review-loader";
import { listBusinessConfirmations } from "./business-confirmation-api";
import type { BusinessConfirmedSummary } from "./business-confirmation-api";
import { listInvoiceHistory, listInvoiceProfiles, getInvoiceProfile } from "./invoice-api";
import type { InvoiceProfile } from "./invoice-issuance-receipt";
import { validReviewedInvoiceProfile } from "./invoice-web-intent";
import type { InvoiceReview } from "./invoice-web-intent";
import { BusinessDocumentCurrentFacts } from "./BusinessDocumentCurrentFacts";
import { InvoiceOriginals } from "./InvoiceOriginals";
import type { PendingInvoiceController } from "./pending-invoice";
import { uuid } from "./credit-intent";

type Props = {
    ledgerId: string;
    locale: Locale;
    session: Session;
    assets: Asset[];
    archived: boolean;
    available: boolean;
    controller: PendingInvoiceController;
    onUnauthorized: () => void;
    onEditing: (open: boolean) => void;
    onClose: () => void;
};
const invalid = () => new ApiError("server", 200, "invalid_response");
export function InvoiceIssuePanel({
    ledgerId,
    locale,
    session,
    assets,
    archived,
    available,
    controller,
    onUnauthorized,
    onEditing,
    onClose,
}: Props) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const [doc, setDoc] = useState(""),
        [config, setConfig] = useState(""),
        [dpage, setDpage] = useState(0),
        [ppage, setPpage] = useState(0),
        [refresh, setRefresh] = useState(0);
    const plan =
        state.plan?.ownerId === session.user.id && state.plan.ledgerId === ledgerId
            ? state.plan
            : null;
    const request = useMemo(
        () => ({ session, ledgerId, doc, config, dpage, ppage, assets, available, refresh, plan }),
        [session, ledgerId, doc, config, dpage, ppage, assets, available, refresh, plan],
    );
    const latest = useRef({ request, onUnauthorized, onEditing });
    latest.current = { request, onUnauthorized, onEditing };
    type Data = {
        request: typeof request;
        docs?: BusinessConfirmedSummary[];
        profiles?: InvoiceProfile[];
        review?: InvoiceReview;
        unavailable?: boolean;
        err?: boolean;
    };
    const [value, setValue] = useState<Data | null>(null),
        [checked, setChecked] = useState<Data | null>(null),
        [localError, setLocalError] = useState(false);
    useEffect(() => {
        latest.current.onEditing(true);
        return () => latest.current.onEditing(false);
    }, []);
    useEffect(() => {
        if (state.status === "auth-required") latest.current.onUnauthorized();
    }, [state.status]);
    useEffect(() => {
        if (plan || !available || archived) return;
        const a = new AbortController();
        const same = () => !a.signal.aborted && latest.current.request === request;
        async function read() {
            if (doc && config) {
                const [cur, profile, history] = await Promise.all([
                    getDocumentCurrent(ledgerId, doc, a.signal),
                    getInvoiceProfile(ledgerId, config, a.signal),
                    listInvoiceHistory(ledgerId, doc, null, a.signal, 1),
                ]);
                if (!same()) return;
                const c = checkedDocumentCurrent(cur, ledgerId, doc, assets);
                if (!validReviewedInvoiceProfile(profile, ledgerId) || profile.id !== config)
                    throw invalid();
                const asset = assets.find((v) => v.asset_id === c.document?.asset_id),
                    prev = history.items[0];
                if (
                    !asset ||
                    c.status !== "active" ||
                    c.document_kind !== "invoice" ||
                    profile.archived ||
                    (prev &&
                        (prev.current_document_version !== c.version ||
                            prev.document_status !== "superseded" ||
                            prev.document_version >= c.version))
                ) {
                    setValue({ request, unavailable: true });
                    return;
                }
                setValue({ request, review: { current: c, asset, profile, latest: history } });
                return;
            }
            const [docs, profiles] = await Promise.all([
                listBusinessConfirmations(ledgerId, dpage * 25, a.signal),
                listInvoiceProfiles(ledgerId, ppage * 50, a.signal),
            ]);
            if (!same()) return;
            if (
                !Array.isArray(docs) ||
                docs.length > 25 ||
                !docs.every(
                    (r) =>
                        r &&
                        r.ledger_id === ledgerId &&
                        uuid(r.id) &&
                        ["bill", "invoice"].includes(r.document_kind) &&
                        r.state === "confirmed" &&
                        typeof r.issue_date === "string",
                ) ||
                !Array.isArray(profiles) ||
                profiles.length > 50 ||
                !profiles.every((r) => validReviewedInvoiceProfile(r, ledgerId) && !r.archived)
            )
                throw invalid();
            if (
                new Set(docs.map((d) => d.id)).size !== docs.length ||
                new Set(profiles.map((d) => d.id)).size !== profiles.length
            )
                throw invalid();
            setValue({ request, docs, profiles });
        }
        void read().catch((e) => {
            if (!same()) return;
            setValue({ request, err: true });
            if (e instanceof ApiError && e.kind === "unauthorized") latest.current.onUnauthorized();
        });
        return () => a.abort();
    }, [request, plan, available, archived, doc, config, ledgerId, dpage, ppage, assets]);
    const v = value?.request === request ? value : null;
    const handle = (p: Promise<void>) => {
        setLocalError(false);
        void p.catch((e) => {
            if (latest.current.request.session !== session) return;
            if (e instanceof ApiError && e.kind === "unauthorized") latest.current.onUnauthorized();
            else setLocalError(true);
        });
    };
    if (state.status === "auth-required" || (state.plan && !plan)) return null;
    if (plan) {
        const obs = state.observed,
            receipt = state.receipt,
            body = JSON.parse(plan.bodyJson),
            code = state.error?.code;
        let logoHelp: string | undefined;
        if (state.status === "rejected" && code === "invoice_logo_size_limit")
            logoHelp = t(
                "请选择 Logo 不超过 8 MiB 或未设置 Logo 的配置，再重新审阅。",
                "Choose a profile with a logo of 8 MiB or less, or a profile without a logo, then review again.",
            );
        else if (state.status === "rejected" && code === "invoice_logo_invalid")
            logoHelp = t(
                "请改用完整的单帧 PNG、JPEG 或 WebP，每边最多 4096 像素、总计最多 4,000,000 像素。可重新导出为 RGB 或 RGBA 后再审阅。",
                "Choose an undamaged, single-frame PNG, JPEG or WebP with at most 4096 pixels per side and 4,000,000 pixels in total. Export in RGB or RGBA if needed, then review again.",
            );
        else if (state.status === "unknown") {
            if (code === "invoice_render_logo_limit" || code === "invoice_render_logo_invalid")
                logoHelp = t(
                    "请核对原意图中与预留号码绑定的 Logo，并保留原请求。",
                    "Inspect the original intent to review the logo bound to the reserved number. Keep the original request.",
                );
            else if (
                code === "invoice_render_logo_unavailable" ||
                code === "invoice_logo_integrity"
            )
                logoHelp = t(
                    "请核对预留时绑定的私有 Logo。原件缺失时，请先恢复原件，再重试原签发请求。",
                    "Check the private logo bound to the reservation. If the original is missing, restore it before retrying the original issuance.",
                );
        }
        return (
            <section
                className="business-panel master-panel invoice-panel"
                aria-label={t("签发结果", "Issuance result")}
            >
                <h3>{t("签发结果", "Issuance result")}</h3>
                <p role="status">
                    {state.status === "confirmed"
                        ? t(
                              "已核对本次签发的两份原件。",
                              "Both originals for this issuance are verified.",
                          )
                        : state.status === "submitting"
                          ? t("正在提交原签发请求…", "Submitting the original issuance…")
                          : state.status === "rejected"
                            ? t(
                                  "本次请求未获受理，请重新审阅单据或配置。",
                                  "This request was declined. Review the document and profile again.",
                              )
                            : state.status === "idempotency-conflict"
                              ? t(
                                    "原键或意图冲突，请核对记录。",
                                    "The original key or intent conflicts. Inspect the record.",
                                )
                              : t(
                                    "本次提交结果未知，只能重试原请求。",
                                    "The submission is uncertain. Retry only the original request.",
                                )}
                </p>
                {logoHelp && <p role="alert">{logoHelp}</p>}
                <p>
                    {t("单据", "Document")}: {body.document_id} · {t("版本", "Version")}{" "}
                    {body.expected_document_version}
                </p>
                {obs && !receipt && (
                    <p>
                        {t("读取到的记录", "Observed record")}: {obs.number} ·{" "}
                        {obs.state === "pending"
                            ? t("号码已预留", "Number reserved")
                            : t(
                                  "原件已封存，本次请求仍待确认",
                                  "Originals sealed; this request is still unconfirmed",
                              )}
                    </p>
                )}
                {receipt && (
                    <>
                        <p>
                            {t("正式编号", "Issued number")}: {receipt.number}
                        </p>
                        <InvoiceOriginals
                            key={receipt.id}
                            result={receipt}
                            ledgerId={ledgerId}
                            locale={locale}
                            onUnauthorized={onUnauthorized}
                        />
                    </>
                )}
                {state.status === "unknown" && (
                    <button
                        className="secondary-button"
                        type="button"
                        disabled={state.checking}
                        onClick={() => handle(controller.retry(session))}
                    >
                        {t("重试原签发请求", "Retry original issuance")}
                    </button>
                )}
                {["unknown", "idempotency-conflict"].includes(state.status) && (
                    <button
                        className="secondary-button"
                        type="button"
                        disabled={state.checking}
                        onClick={() => handle(controller.inspect(session))}
                    >
                        {t("核对原意图状态", "Inspect original intent")}
                    </button>
                )}
                {state.error && !state.checking && state.status === "unknown" && !logoHelp && (
                    <p>
                        {t(
                            "未获得可确认本次提交的完整回执。",
                            "A complete receipt for this submission is unavailable.",
                        )}
                    </p>
                )}
                {localError && (
                    <p role="alert">{t("无法执行此操作。", "This action is unavailable.")}</p>
                )}
                {["rejected", "confirmed"].includes(state.status) && (
                    <button
                        className="secondary-button"
                        onClick={() => {
                            if (controller.dismiss()) onClose();
                        }}
                    >
                        {t("关闭", "Close")}
                    </button>
                )}
            </section>
        );
    }
    const review = v?.review,
        previous = review?.latest.items[0];
    return (
        <section
            className="business-panel master-panel invoice-panel"
            aria-label={t("签发商业发票", "Issue commercial invoice")}
        >
            <h3>{t("签发商业发票", "Issue commercial invoice")}</h3>
            {archived ? (
                <p>
                    {t(
                        "已归档公司不能开始新签发。",
                        "An archived company cannot start a new issuance.",
                    )}
                </p>
            ) : !available || !v ? (
                <p role="status">{t("正在读取审阅资料…", "Loading review details…")}</p>
            ) : v.err ? (
                <p role="alert">
                    {t("无法核对签发资料。", "Issuance information could not be verified.")}
                </p>
            ) : v.unavailable ? (
                <p role="alert">
                    {t(
                        "当前单据或配置不可签发；已预留的版本请在历史中核对。",
                        "This version or profile is not available for issuance. Check reserved versions in history.",
                    )}
                </p>
            ) : null}
            {v?.docs && v.profiles && (
                <>
                    <label>
                        {t("已确认的 Invoice", "Confirmed invoice")}
                        <select value={doc} onChange={(e) => setDoc(e.target.value)}>
                            <option value="">{t("选择单据", "Choose a document")}</option>
                            {v.docs
                                .filter((d) => d.document_kind === "invoice" && !d.archived)
                                .map((d) => (
                                    <option key={d.id} value={d.id}>
                                        {d.issue_date} · {d.id}
                                    </option>
                                ))}
                        </select>
                    </label>
                    <button
                        className="secondary-button"
                        disabled={!dpage}
                        onClick={() => {
                            setDoc("");
                            setDpage((n) => n - 1);
                        }}
                    >
                        {t("上一页单据", "Previous documents")}
                    </button>
                    <button
                        className="secondary-button"
                        disabled={v.docs.length < 25 || dpage >= 4000}
                        onClick={() => {
                            setDoc("");
                            setDpage((n) => n + 1);
                        }}
                    >
                        {t("下一页单据", "Next documents")}
                    </button>
                    <label>
                        {t("签发配置", "Issuance profile")}
                        <select value={config} onChange={(e) => setConfig(e.target.value)}>
                            <option value="">{t("选择配置", "Choose a profile")}</option>
                            {v.profiles.map((p) => (
                                <option key={p.id} value={p.id}>
                                    {p.name}
                                </option>
                            ))}
                        </select>
                    </label>
                    <button
                        className="secondary-button"
                        disabled={!ppage}
                        onClick={() => {
                            setConfig("");
                            setPpage((n) => n - 1);
                        }}
                    >
                        {t("上一页配置", "Previous profiles")}
                    </button>
                    <button
                        className="secondary-button"
                        disabled={v.profiles.length < 50 || ppage >= 2000}
                        onClick={() => {
                            setConfig("");
                            setPpage((n) => n + 1);
                        }}
                    >
                        {t("下一页配置", "Next profiles")}
                    </button>
                </>
            )}
            {review && (
                <>
                    <BusinessDocumentCurrentFacts current={review.current} locale={locale} />
                    <h4>
                        {review.profile.name} · {t("配置版本", "Profile version")}{" "}
                        {review.profile.version}
                    </h4>
                    <dl>
                        <dt>{t("号码前缀 / 最少数字位数", "Number prefix / minimum digits")}</dt>
                        <dd>
                            {review.profile.numbering.prefix || "—"} /{" "}
                            {review.profile.numbering.minimum_digits}
                        </dd>
                        <dt>{t("发行人补充资料", "Issuer details")}</dt>
                        <dd>{review.profile.issuer_extra_text || "—"}</dd>
                        <dt>{t("收款说明", "Payment instructions")}</dt>
                        <dd>{review.profile.payment_instructions || "—"}</dd>
                        <dt>{t("页脚", "Footer")}</dt>
                        <dd>{review.profile.footer || "—"}</dd>
                        <dt>Logo</dt>
                        <dd>
                            {review.profile.logo_file_id ?? "—"}{" "}
                            {review.profile.logo_file_version ?? ""}
                        </dd>
                        <dt>{t("前次编号", "Previous number")}</dt>
                        <dd>{previous?.number ?? "—"}</dd>
                    </dl>
                    <label>
                        <input
                            type="checkbox"
                            checked={checked === v}
                            onChange={(e) => setChecked(e.target.checked ? v : null)}
                        />
                        {t(
                            "我已核对单据、配置及前次编号，签发中英文两份商业发票。",
                            "I reviewed the document, profile and previous number to issue both commercial originals.",
                        )}
                    </label>
                    <button
                        className="secondary-button"
                        type="button"
                        disabled={checked !== v || !available || archived}
                        onClick={() => handle(controller.start(session, ledgerId, review))}
                    >
                        {t("确认签发", "Confirm issuance")}
                    </button>
                </>
            )}
            {localError && (
                <p role="alert">
                    {t("无法提交已审阅的资料。", "The reviewed details could not be submitted.")}
                </p>
            )}
            {((doc && config) || v?.err) && (
                <button
                    className="secondary-button"
                    onClick={() => {
                        setDoc("");
                        setConfig("");
                        setChecked(null);
                        setRefresh((n) => n + 1);
                    }}
                >
                    {t("重新选择", "Choose again")}
                </button>
            )}
            <button className="secondary-button" type="button" onClick={onClose}>
                {t("关闭", "Close")}
            </button>
        </section>
    );
}
