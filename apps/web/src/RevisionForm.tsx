import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type {
    Account,
    Asset,
    CancellationCreate,
    Category,
    CorrectionCreate,
    OperationState,
    PostingInput,
} from "./ledger-api";
import { PostingForm } from "./PostingForm";
import { postingInputFromState, replacementFromInput } from "./revision-input";
import "./posting-form.css";

const copy = {
    zh: {
        reason: "修订原因",
        reasonHelp: "填写本次修改的原因，最多 1000 字。原因将与历史记录一起保留。",
        reasonError: "请填写 1 至 1000 字的修订原因，不能仅为空白或包含空字符。",
        version: "基于版本",
        correction: "保存更正",
        cancellation: "确认取消",
        cancelling: "取消中…",
        back: "返回",
        cancelTitle: "取消记录表单",
        cancelHelp: "取消会冲销此记录的本金和全部手续费，历史仍会保留。取消后的记录不能重新激活。",
        inactive: "此记录已取消，不能再次更正或取消。",
    },
    en: {
        reason: "Revision reason",
        reasonHelp:
            "Explain this change in at most 1000 characters. The reason is retained with the history.",
        reasonError:
            "Enter a revision reason of 1 to 1000 characters, without null characters or only whitespace.",
        version: "Based on version",
        correction: "Save correction",
        cancellation: "Confirm cancellation",
        cancelling: "Cancelling…",
        back: "Back",
        cancelTitle: "Cancellation form",
        cancelHelp:
            "Cancellation reverses the principal and every fee while retaining the history. This entry cannot be reactivated after cancellation.",
        inactive: "This entry is already cancelled and cannot be corrected or cancelled again.",
    },
};

export type RevisionFormProps = {
    locale: Locale;
    operation: OperationState;
    action: "correct" | "cancel";
    accounts: Account[];
    assets: Asset[];
    categories: Category[];
    busy: boolean;
    submitting?: boolean;
    error?: string | null;
    onSubmit: (body: CorrectionCreate | CancellationCreate) => void;
    onCancel: () => void;
};

export function RevisionForm({
    locale,
    operation,
    action,
    accounts,
    assets,
    categories,
    busy,
    submitting = busy,
    error,
    onSubmit,
    onCancel,
}: RevisionFormProps) {
    const t = copy[locale];
    const reasonId = useId();
    // A new parent key explicitly reloads the entry. Background refreshes must not
    // silently advance expected_version or replace an in-progress correction.
    const [initial] = useState(() => ({
        version: operation.version,
        status: operation.status,
        input: postingInputFromState(operation),
    }));
    const [reason, setReason] = useState("");
    const [invalidReason, setInvalidReason] = useState(false);
    const inactive = initial.status !== "active";
    const locked = busy || inactive;

    function validatedReason(): string | null {
        const value = reason.trim();
        if (!value || value.length > 1000 || value.includes("\0")) {
            setInvalidReason(true);
            return null;
        }
        setInvalidReason(false);
        return value;
    }

    function correct(input: PostingInput) {
        if (locked) return;
        const value = validatedReason();
        if (value !== null)
            onSubmit({
                expected_version: initial.version,
                reason: value,
                replacement: replacementFromInput(input),
            });
    }

    function cancel(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (locked) return;
        const value = validatedReason();
        if (value !== null) onSubmit({ expected_version: initial.version, reason: value });
    }

    const header = (
        <section className="revision-form-header">
            <p className="help-text revision-version">
                {t.version} {initial.version}
            </p>
            {action === "cancel" && <p className="revision-cancel-help">{t.cancelHelp}</p>}
            <div className="field">
                <label htmlFor={reasonId}>{t.reason}</label>
                <textarea
                    id={reasonId}
                    value={reason}
                    maxLength={1000}
                    required
                    disabled={locked}
                    aria-invalid={invalidReason}
                    aria-describedby={`${reasonId}-help${invalidReason ? ` ${reasonId}-error` : ""}`}
                    onChange={(event) => {
                        setReason(event.target.value);
                        setInvalidReason(false);
                    }}
                />
                <p className="help-text" id={`${reasonId}-help`}>
                    {t.reasonHelp}
                </p>
            </div>
            {invalidReason && (
                <p className="inline-error" role="alert" id={`${reasonId}-error`}>
                    {t.reasonError}
                </p>
            )}
            {inactive && (
                <p className="inline-error" role="alert">
                    {t.inactive}
                </p>
            )}
        </section>
    );

    if (action === "correct")
        return (
            <PostingForm
                locale={locale}
                accounts={accounts}
                assets={assets}
                categories={categories}
                initialInput={initial.input}
                lockKind
                lockOpeningTarget
                submitLabel={t.correction}
                extraHeader={header}
                busy={locked}
                submitting={submitting}
                error={error}
                onSubmit={correct}
                onCancel={onCancel}
            />
        );

    return (
        <form
            className="revision-form"
            aria-label={t.cancelTitle}
            aria-busy={submitting}
            onSubmit={cancel}
        >
            {header}
            {error && (
                <p className="inline-error" role="alert">
                    {error}
                </p>
            )}
            <div className="form-actions">
                <button
                    type="button"
                    className="secondary-button"
                    disabled={busy}
                    onClick={onCancel}
                >
                    {t.back}
                </button>
                <button type="submit" className="primary-button" disabled={locked}>
                    {submitting ? t.cancelling : t.cancellation}
                </button>
            </div>
        </form>
    );
}
