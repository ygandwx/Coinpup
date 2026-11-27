import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import "./account-editor.css";

export function AccountDrawer({
    title,
    busy,
    onCancel,
    languageControl,
    children,
}: {
    title: string;
    busy: boolean;
    onCancel: () => void;
    languageControl?: ReactNode;
    children: ReactNode;
}) {
    const dialog = useRef<HTMLDialogElement>(null);
    const heading = useRef<HTMLHeadingElement>(null);
    const titleId = useId();

    useEffect(() => {
        const element = dialog.current;
        const previous = document.documentElement.style.overflow;
        element?.showModal();
        heading.current?.focus();
        document.documentElement.style.overflow = "hidden";
        return () => {
            element?.close();
            document.documentElement.style.overflow = previous;
        };
    }, []);

    return (
        <dialog
            ref={dialog}
            className="account-drawer"
            aria-labelledby={titleId}
            onCancel={(event) => {
                event.preventDefault();
                if (!busy) onCancel();
            }}
        >
            <header className="account-drawer-header">
                <h2 id={titleId} ref={heading} tabIndex={-1}>
                    {title}
                </h2>
                {languageControl}
            </header>
            {children}
        </dialog>
    );
}
