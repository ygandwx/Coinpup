import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import type { Entity } from "./ledger-api";
import {
  downloadFile, getFile, getFileConfiguration, linkFile, listFiles, listOperationFiles,
  updateFile, updateLink,
} from "./document-api";
import type { DocumentFile, FileConfiguration, OperationFileLink } from "./document-api";
import type { PendingUploadController } from "./pending-upload";
import "./documents.css";

type TitleDraft = { file: DocumentFile; title: string; error: unknown };
type LinkedFile = { link: OperationFileLink; file: DocumentFile };
const PAGE_SIZE = 25;

function documentError(error: unknown, locale: Locale): string {
  const t = (zh: string, en: string) => locale === "zh" ? zh : en;
  if (!(error instanceof ApiError)) return t("暂时无法完成操作，请重试。", "This action could not be completed. Please retry.");
  if (error.code === "version_conflict") return t("此文件已被修改。当前输入已保留；重新载入会替换草稿。", "This file changed elsewhere. Your draft is preserved; reloading replaces it.");
  if (error.code === "file_archived") return t("请先恢复文件，再创建或恢复关联。", "Restore the file before creating or restoring an association.");
  if (error.code === "entity_archived") return t("账本已归档，目前仅可查看和下载。", "This ledger is archived. Viewing and downloading remain available.");
  if (["file_too_large", "upload_too_large", "empty_file"].includes(error.code ?? "")) return t("请选择大小在允许范围内的非空文件。", "Choose a nonempty file within the upload limit.");
  if (error.code === "unsupported_file_format") return t("文件内容不符合支持的 PDF、JPEG、PNG 或 WebP 格式。", "The file content is not a supported PDF, JPEG, PNG or WebP document.");
  if (["upload_manifest_conflict", "upload_content_conflict"].includes(error.code ?? "")) return t("此上传标识对应的内容不同。请核对服务器保存的文件，不能将本次上传当作已确认。", "This upload identifier has different content. Inspect the stored file; this upload is not confirmed.");
  if (error.code === "file_integrity_error") return t("原件完整性校验未通过，请检查服务器文件存储或备份。", "The original file failed its integrity check. Check server storage or the backup.");
  if (error.code === "download_integrity_error") return t("下载内容与文件记录不符，未生成下载文件。请重试或检查文件存储。", "The download did not match the file record. No download was created. Retry or check file storage.");
  if (error.status === 415) return t("请选择受支持的文件格式。", "Choose a supported file format.");
  if (error.status === 422) return t("请检查文件名、文件大小和标题；标题不能为空或含控制字符。", "Check the filename, size and title. Titles cannot be blank or contain control characters.");
  if (error.status === 409) return t("文件或关联状态已变化，请刷新核对后再操作。", "The file or association changed. Refresh and check before another action.");
  if (error.status === 404) return t("找不到此文件或关联，请刷新列表。", "This file or association was not found. Refresh the list.");
  if (error.kind === "forbidden") return t("当前操作未获允许，请重新登录后重试。", "This action is not permitted. Sign in again and retry.");
  if (error.kind === "network" || error.status === 408) return t("连接中断或超时。请先核对当前状态，避免把未确认的保存当作成功。", "The connection was interrupted or timed out. Check the current state before treating a save as complete.");
  return t("文件服务暂时不可用，请稍后重试。", "The file service is unavailable. Please retry later.");
}

function fileSize(bytes: number, locale: Locale): string {
  return `${bytes.toLocaleString(locale === "zh" ? "zh-CN" : "en-GB")} ${locale === "zh" ? "字节" : "bytes"}`;
}

function FileCard({ file, link, locale, busy, readOnly, onDownload, onEdit, onArchive, onLinkArchive }: {
  file: DocumentFile; link?: OperationFileLink; locale: Locale; busy: boolean; readOnly: boolean;
  onDownload: () => void; onEdit: () => void; onArchive: () => void; onLinkArchive?: () => void;
}) {
  const t = (zh: string, en: string) => locale === "zh" ? zh : en;
  return <article className="document-card" data-testid={link ? `file-link-${link.id}` : `file-${file.id}`}>
    <div className="record-heading"><h3>{file.title}</h3><span className="record-badge">{file.archived ? t("文件已归档", "File archived") : t("有效文件", "Active file")} · v{file.version}</span></div>
    <dl className="document-details">
      <div><dt>{t("原始文件名", "Original filename")}</dt><dd>{file.original_filename}</dd></div>
      <div><dt>{t("文件格式与大小", "Format and size")}</dt><dd>{file.detected_media_type} · {fileSize(file.byte_size, locale)}</dd></div>
      {link && <div><dt>{t("此记录的关联", "Association with this entry")}</dt><dd>{link.archived ? t("关联已归档", "Link archived") : t("有效关联", "Active link")} · v{link.version}</dd></div>}
    </dl>
    <div className="row-actions document-actions">
      <button disabled={busy} onClick={onDownload}>{t("下载", "Download")}</button>
      <button disabled={busy || readOnly} onClick={onEdit}>{t("编辑标题", "Edit title")}</button>
      <button disabled={busy || readOnly} onClick={onArchive}>{file.archived ? t("恢复文件", "Restore file") : t("归档文件", "Archive file")}</button>
      {link && <button disabled={busy || readOnly || (link.archived && file.archived)} onClick={onLinkArchive}>{link.archived ? t("恢复关联", "Restore link") : t("归档关联", "Archive link")}</button>}
    </div>
  </article>;
}

export type DocumentsPanelProps = {
  session: Session; locale: Locale; entity: Entity; operationId?: string | null;
  controller: PendingUploadController; onUnauthorized: () => void;
  onEditingChange: (value: boolean) => void; onClose?: () => void;
};

export function DocumentsPanel({ session, locale, entity, operationId = null, controller, onUnauthorized, onEditingChange, onClose }: DocumentsPanelProps) {
  const t = (zh: string, en: string) => locale === "zh" ? zh : en;
  const ledgerId = entity.ledger.id;
  const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const commandHere = pending.command?.ledgerId === ledgerId && pending.command.operationId === operationId;
  const transferring = pending.status === "reserving" || pending.status === "uploading";
  const uploadLocked = !["idle", "rejected", "confirmed"].includes(pending.status);
  const [configuration, setConfiguration] = useState<FileConfiguration | null>(null);
  const [configurationError, setConfigurationError] = useState<unknown>(null);
  const [files, setFiles] = useState<DocumentFile[]>([]);
  const [linkedFiles, setLinkedFiles] = useState<LinkedFile[]>([]);
  const [fileOffset, setFileOffset] = useState(0);
  const [linkOffset, setLinkOffset] = useState(0);
  const [showArchivedFiles, setShowArchivedFiles] = useState(false);
  const [showArchivedLinks, setShowArchivedLinks] = useState(true);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [existingFile, setExistingFile] = useState("");
  const [titleDraft, setTitleDraft] = useState<TitleDraft | null>(null);
  const [notice, setNotice] = useState<"uploaded" | "duplicate" | "saved" | "archived-link" | null>(null);
  const [savedFileId, setSavedFileId] = useState<string | null>(null);
  const [savedFile, setSavedFile] = useState<DocumentFile | null>(null);
  const [savedFileError, setSavedFileError] = useState<unknown>(null);
  const [savedFileRefresh, setSavedFileRefresh] = useState(0);
  const input = useRef<HTMLInputElement | null>(null);
  const downloadAbort = useRef<AbortController | null>(null);
  const downloadUrls = useRef(new Map<string, number>());
  const lastConfirmed = useRef<string | null>(null);
  const callbacks = useRef({ onUnauthorized, onEditingChange });
  callbacks.current = { onUnauthorized, onEditingChange };
  const readOnly = entity.archived;
  const writesDisabled = readOnly || busy || loading || uploadLocked || !!titleDraft;
  const localEditing = busy || !!titleDraft || !!selectedFile;

  function failure(problem: unknown) {
    if (problem instanceof ApiError && problem.kind === "unauthorized") callbacks.current.onUnauthorized();
    else setError(problem);
  }

  useEffect(() => {
    callbacks.current.onEditingChange(localEditing);
    return () => callbacks.current.onEditingChange(false);
  }, [localEditing]);
  useEffect(() => {
    document.getElementById("documents-title")?.focus();
  }, [ledgerId, operationId]);
  useEffect(() => () => {
    downloadAbort.current?.abort();
    for (const [url, timer] of downloadUrls.current) {
      window.clearTimeout(timer); URL.revokeObjectURL(url);
    }
    downloadUrls.current.clear();
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    setConfigurationError(null);
    void getFileConfiguration(abort.signal).then(value => {
      if (!abort.signal.aborted) setConfiguration(value);
    }).catch(problem => {
      if (abort.signal.aborted) return;
      if (problem instanceof ApiError && problem.kind === "unauthorized") callbacks.current.onUnauthorized();
      else setConfigurationError(problem);
    });
    return () => abort.abort();
  }, [session.user.id, refresh]);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setLoadError(null);
    const readLinks = operationId ? listOperationFiles(ledgerId, operationId, { include_archived: showArchivedLinks, limit: PAGE_SIZE, offset: linkOffset }, abort.signal).then(async links => {
      return Promise.all(links.map(async link => ({ link, file: await getFile(ledgerId, link.file_id, abort.signal) })));
    }) : Promise.resolve([]);
    void Promise.all([
      listFiles(ledgerId, { include_archived: showArchivedFiles, limit: PAGE_SIZE, offset: fileOffset }, abort.signal),
      readLinks,
    ]).then(([documents, links]) => {
      if (abort.signal.aborted) return;
      setFiles(documents); setLinkedFiles(links); setLoading(false);
    }).catch(problem => {
      if (abort.signal.aborted) return;
      setLoading(false); setLoadError(problem);
      if (problem instanceof ApiError && problem.kind === "unauthorized") callbacks.current.onUnauthorized();
    });
    return () => abort.abort();
  }, [ledgerId, operationId, fileOffset, linkOffset, showArchivedFiles, showArchivedLinks, refresh, session.user.id]);
  useEffect(() => {
    if (pending.status === "auth-required") callbacks.current.onUnauthorized();
    if (pending.status !== "confirmed" || !commandHere || !pending.command || !pending.receipt || lastConfirmed.current === pending.command.uploadId) return;
    lastConfirmed.current = pending.command.uploadId;
    setNotice(pending.receipt.duplicate ? "duplicate" : "uploaded"); setError(null); setSelectedFile(null);
    setSavedFile(null); setSavedFileError(null); setSavedFileId(pending.receipt.file_id);
    setSavedFileRefresh(value => value + 1);
    if (input.current) input.current.value = "";
    setFileOffset(0); setLinkOffset(0); setRefresh(value => value + 1);
    controller.dismiss();
    requestAnimationFrame(() => document.getElementById("documents-title")?.focus());
  }, [pending, commandHere, controller]);
  useEffect(() => {
    if (!savedFileId) return;
    const abort = new AbortController();
    setSavedFileError(null);
    void getFile(ledgerId, savedFileId, abort.signal).then(file => {
      if (!abort.signal.aborted) setSavedFile(file);
    }).catch(problem => {
      if (abort.signal.aborted) return;
      setSavedFileError(problem);
      if (problem instanceof ApiError && problem.kind === "unauthorized") callbacks.current.onUnauthorized();
    });
    return () => abort.abort();
  }, [ledgerId, savedFileId, savedFileRefresh, session.user.id]);
  useEffect(() => { if (titleDraft) document.getElementById("document-title-editor")?.focus(); }, [titleDraft?.file.id]);

  function clearFile() {
    if (uploadLocked || busy) return;
    controller.dismiss(); setSelectedFile(null); setError(null);
    if (input.current) input.current.value = "";
  }
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFile || !configuration || writesDisabled) return;
    setError(null); setNotice(null); setSavedFileId(null); setSavedFile(null); setSavedFileError(null);
    try {
      if (pending.status === "rejected") controller.dismiss();
      await controller.submit(session, ledgerId, { file: selectedFile, operationId }, configuration);
    } catch (problem) { failure(problem); }
  }
  async function uploadAction(action: "retry" | "check") {
    setError(null);
    try { await (action === "retry" ? controller.retry(session) : controller.reconcile(session)); }
    catch (problem) { failure(problem); }
  }
  async function download(file: DocumentFile) {
    if (busy) return;
    const abort = new AbortController();
    downloadAbort.current = abort;
    setBusy(true); setError(null);
    try {
      const blob = await downloadFile(ledgerId, file.id, abort.signal);
      if (blob.size !== file.byte_size || blob.type !== file.detected_media_type) throw new ApiError("server", 200, "download_integrity_error");
      const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
      const sha256 = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
      if (sha256 !== file.sha256) throw new ApiError("server", 200, "download_integrity_error");
      if (abort.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = file.original_filename; anchor.hidden = true;
      document.body.append(anchor);
      try { anchor.click(); } finally {
        anchor.remove();
        // Allow the browser to begin consuming the download before releasing the blob.
        downloadUrls.current.set(url, window.setTimeout(() => {
          URL.revokeObjectURL(url); downloadUrls.current.delete(url);
        }, 1000));
      }
    } catch (problem) { if (!abort.signal.aborted) failure(problem); }
    finally {
      if (downloadAbort.current === abort) downloadAbort.current = null;
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  async function archiveFile(file: DocumentFile) {
    if (writesDisabled) return;
    setBusy(true); setError(null); setNotice(null);
    try { await updateFile(session.csrf_token, ledgerId, file.id, { expected_version: file.version, archived: !file.archived }); setNotice("saved"); }
    catch (problem) { failure(problem); }
    finally { setBusy(false); setRefresh(value => value + 1); }
  }
  async function archiveLink(link: OperationFileLink) {
    if (!operationId || writesDisabled) return;
    setBusy(true); setError(null); setNotice(null);
    try { await updateLink(session.csrf_token, ledgerId, operationId, link.file_id, { expected_version: link.version, archived: !link.archived }); setNotice("saved"); }
    catch (problem) { failure(problem); }
    finally { setBusy(false); setRefresh(value => value + 1); }
  }
  async function associate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!operationId || !existingFile || writesDisabled) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      const link = await linkFile(session.csrf_token, ledgerId, operationId, existingFile);
      setNotice(link.archived ? "archived-link" : "saved"); setExistingFile(""); setLinkOffset(0);
      if (link.archived) setShowArchivedLinks(true);
    } catch (problem) { failure(problem); }
    finally { setBusy(false); setRefresh(value => value + 1); }
  }
  function editTitle(file: DocumentFile) {
    if (writesDisabled) return;
    setError(null); setNotice(null); setTitleDraft({ file, title: file.title, error: null });
  }
  async function saveTitle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!titleDraft || busy || uploadLocked || readOnly) return;
    const draft = titleDraft;
    if (!draft.title.trim() || /\p{Cc}/u.test(draft.title)) {
      setTitleDraft({ ...draft, error: new ApiError("server", 422) }); return;
    }
    setBusy(true); setNotice(null);
    try {
      await updateFile(session.csrf_token, ledgerId, draft.file.id, { expected_version: draft.file.version, title: draft.title });
      setTitleDraft(null); setNotice("saved"); setRefresh(value => value + 1);
      requestAnimationFrame(() => document.getElementById("documents-title")?.focus());
    } catch (problem) {
      if (problem instanceof ApiError && problem.kind === "unauthorized") callbacks.current.onUnauthorized();
      else setTitleDraft({ ...draft, error: problem });
    } finally { setBusy(false); }
  }
  async function reloadTitle() {
    if (!titleDraft || busy || uploadLocked) return;
    setBusy(true);
    try {
      const file = await getFile(ledgerId, titleDraft.file.id);
      setTitleDraft({ file, title: file.title, error: null }); setRefresh(value => value + 1);
      requestAnimationFrame(() => document.getElementById("document-title-editor")?.focus());
    } catch (problem) { failure(problem); }
    finally { setBusy(false); }
  }
  const canSelect = files.filter(file => !file.archived && !linkedFiles.some(item => item.link.file_id === file.id && !item.link.archived));
  const navDisabled = loading || busy || uploadLocked || !!titleDraft || !!selectedFile;
  const pendingHere = commandHere && pending.command;
  const unconfirmed = pending.status === "unknown" || pending.status === "upload-conflict";
  const renderCard = (file: DocumentFile, link?: OperationFileLink) => <FileCard key={link?.id ?? file.id} file={file} link={link} locale={locale} busy={busy || uploadLocked || !!titleDraft} readOnly={readOnly} onDownload={() => void download(file)} onEdit={() => editTitle(file)} onArchive={() => void archiveFile(file)} onLinkArchive={link ? () => void archiveLink(link) : undefined} />;
  return <section className="documents-panel">
    <section className="business-panel">
      <div className="section-heading"><div><h2 id="documents-title" tabIndex={-1}>{operationId ? t("此记录的票据", "Files for this entry") : t("票据与文件", "Documents")}</h2><p className="help-text">{operationId ? t("票据关联到整条记录，更正或取消后仍保留。归档关联不会归档原件。", "Files belong to the entry across corrections and cancellation. Archiving an association keeps the original file.") : t("保存账单、发票照片、营业执照或注册证书原件。文件仅在当前账本内共享。", "Keep original statements, invoice photos and company certificates. Files are shared only within this ledger.")}</p></div>{onClose && <button className="secondary-button" disabled={navDisabled} onClick={onClose}>{t("返回流水", "Back to transactions")}</button>}</div>
      {readOnly && <p className="archive-banner">{t("账本已归档。文件仍可查看和下载。", "This ledger is archived. Files remain available to view and download.")}</p>}
      {notice && <p className="save-notice" role="status">{notice === "uploaded" ? t("上传已确认。", "Upload confirmed.") : notice === "duplicate" ? t("上传已确认。此账本已有完全相同的文件，已复用原件。", "Upload confirmed. Exact file already exists in this ledger; the original was reused.") : notice === "archived-link" ? t("此关联已归档。请在归档关联中恢复，原件未被删除。", "This association is archived. Restore it from archived links; the original is retained.") : t("文件信息已保存。", "File information saved.")}</p>}
      {savedFileId && <div className="document-saved">
        {savedFile ? <><p className="help-text document-filename">{t("最近上传的原件", "Original from the latest upload")}: {savedFile.original_filename}{savedFile.archived ? t("（原件已归档，仍可下载）", " (archived; available to download)") : ""}</p><button className="secondary-button" disabled={busy || uploadLocked || !!titleDraft} onClick={() => void download(savedFile)}>{t("下载已保存原件", "Download saved file")}</button></> : savedFileError === null ? <p className="help-text" role="status">{t("正在读取已保存原件…", "Loading the saved file…")}</p> : <><p className="inline-error" role="alert">{documentError(savedFileError, locale)}</p><button className="secondary-button" disabled={busy || uploadLocked} onClick={() => setSavedFileRefresh(value => value + 1)}>{t("重新载入已保存原件", "Reload saved file")}</button></>}
      </div>}
      {error !== null && <p className="inline-error" role="alert">{documentError(error, locale)}</p>}
      {pendingHere && (transferring || unconfirmed) && <div className="pending-command document-pending" role="status">
        <h3>{transferring ? t("正在上传…", "Uploading…") : t("上传结果待确认", "Upload not confirmed")}</h3>
        <p className="document-filename">{pendingHere.filename}</p>
        {unconfirmed && pending.error && <p className="inline-error" role="alert">{documentError(pending.error, locale)}</p>}
        <p>{pending.status === "upload-conflict" ? t("原上传内容与服务器记录不符。请保留原文件并核对，当前上传不能确认成功。", "The original upload differs from the server record. Keep the original and inspect the stored file; this upload is not confirmed.") : t("已保留原文件和上传标识。停止传输不代表服务器取消保存；核对或重试会继续使用同一上传。", "The original file and upload identifier are retained. Stopping transmission does not cancel server storage. Checking or retrying uses this same upload.")}</p>
        <p className="help-text">{t("刷新或关闭网页会丢失本页保留的文件；重新选择前请先核对票据目录。", "Reloading or closing this page loses the retained file. Check the document catalog before choosing it again.")}</p>
        {pending.reservation?.state === "pending" && <p className="help-text">{t("服务器尚未确认原件已保存，可以重试原上传。", "The server has not confirmed stored content. You can retry the original upload.")}</p>}
        <div className="form-actions">{transferring ? <button className="secondary-button" onClick={() => controller.stop()}>{t("停止上传", "Stop upload")}</button> : <><button className="secondary-button" disabled={pending.checking || busy} onClick={() => void uploadAction("check")}>{pending.checking ? t("核对中…", "Checking…") : t("核对上传", "Check upload status")}</button><button className="primary-button" disabled={pending.checking || busy || readOnly || pending.status === "upload-conflict"} onClick={() => void uploadAction("retry")}>{t("重试原上传", "Retry original upload")}</button></>}</div>
      </div>}
      {!titleDraft && <form className="document-upload" onSubmit={upload} aria-busy={transferring}>
        <div className="field"><label htmlFor="document-upload-file">{t("上传文件", "File to upload")}</label><input ref={input} id="document-upload-file" type="file" accept=".pdf,.jpg,.jpeg,.png,.webp,application/pdf,image/jpeg,image/png,image/webp" disabled={writesDisabled} onChange={event => { controller.dismiss(); setSelectedFile(event.target.files?.[0] ?? null); setError(null); setNotice(null); }} />
          {selectedFile && <p className="help-text document-filename">{selectedFile.name} · {fileSize(selectedFile.size, locale)}</p>}
          <p className="help-text">PDF · JPEG · PNG · WebP{configuration && <> · {t("最大", "Maximum")}: {fileSize(configuration.max_upload_bytes, locale)}</>}</p>
        </div>
        {configurationError !== null && <p className="inline-error" role="alert">{documentError(configurationError, locale)}</p>}
        {commandHere && pending.status === "rejected" && <p className="inline-error" role="alert">{documentError(pending.error, locale)}</p>}
        <div className="document-upload-actions"><button className="secondary-button" type="button" disabled={!selectedFile || busy || uploadLocked} onClick={clearFile}>{t("清除选择", "Clear selection")}</button><button className="primary-button" disabled={!selectedFile || !configuration || writesDisabled}>{t("上传文件", "Upload file")}</button></div>
      </form>}
      {titleDraft && <form className="document-title-form" onSubmit={saveTitle} aria-busy={busy}>
        <h2 id="document-title-editor" tabIndex={-1}>{t("编辑文件标题", "Edit file title")}</h2><p className="help-text">{t("基于版本", "Based on version")} {titleDraft.file.version} · {titleDraft.file.original_filename}</p>
        <div className="field"><label htmlFor="document-title">{t("文件标题", "File title")}</label><input id="document-title" required maxLength={255} value={titleDraft.title} disabled={busy || readOnly || uploadLocked} onChange={event => setTitleDraft({ ...titleDraft, title: event.target.value })} /></div>
        {titleDraft.error !== null && <p className="inline-error" role="alert">{documentError(titleDraft.error, locale)}</p>}
        <div className="form-actions"><button type="button" className="secondary-button" disabled={busy || uploadLocked} onClick={() => { setTitleDraft(null); requestAnimationFrame(() => document.getElementById("documents-title")?.focus()); }}>{t("取消", "Cancel")}</button><button className="primary-button" disabled={busy || readOnly || uploadLocked}>{busy ? t("保存中…", "Saving…") : t("保存标题", "Save title")}</button></div>
        {titleDraft.error !== null && <button type="button" className="text-button reload-editor" disabled={busy || uploadLocked} onClick={() => void reloadTitle()}>{t("重新载入当前文件", "Reload current file")}</button>}
      </form>}
    </section>
    <section className="business-panel">
      <div className="section-heading"><h2>{operationId ? t("已关联票据", "Linked files") : t("文件目录", "File catalog")}</h2><button className="secondary-button" disabled={navDisabled} onClick={() => { setError(null); setRefresh(value => value + 1); }}>{t("刷新文件", "Refresh files")}</button></div>
      <label className="archive-toggle"><input type="checkbox" checked={operationId ? showArchivedLinks : showArchivedFiles} disabled={navDisabled} onChange={event => { if (operationId) { setShowArchivedLinks(event.target.checked); setLinkOffset(0); } else { setShowArchivedFiles(event.target.checked); setFileOffset(0); } }} />{operationId ? t("显示归档关联", "Show archived links") : t("显示归档文件", "Show archived files")}</label>
      {loadError !== null && <p className="inline-error" role="alert">{documentError(loadError, locale)}</p>}
      {loading ? <p className="help-text" role="status">{t("正在读取文件…", "Loading files…")}</p> : <div className="document-grid">{operationId ? linkedFiles.map(item => renderCard(item.file, item.link)) : files.map(file => renderCard(file))}</div>}
      {!loading && loadError === null && !(operationId ? linkedFiles.length : files.length) && <p className="empty-copy">{operationId ? t("此页暂无关联票据。上传原件或关联账本中已有文件。", "No linked files on this page. Upload an original or link a file already in this ledger.") : t("此页暂无文件。上传账单、发票照片或公司证件，保留原始依据。", "No files on this page. Upload statements, invoice photos or company certificates to retain the originals.")}</p>}
      <nav className="transaction-pagination" aria-label={t("文件分页", "File pages")}><button className="secondary-button" disabled={navDisabled || (operationId ? linkOffset : fileOffset) === 0} onClick={() => operationId ? setLinkOffset(value => Math.max(0, value - PAGE_SIZE)) : setFileOffset(value => Math.max(0, value - PAGE_SIZE))}>{t("上一页", "Previous page")}</button><span>{t("第", "Page")} {(operationId ? linkOffset : fileOffset) / PAGE_SIZE + 1}{locale === "zh" ? " 页" : ""}</span><button className="secondary-button" disabled={navDisabled || (operationId ? linkedFiles.length : files.length) < PAGE_SIZE || (operationId ? linkOffset : fileOffset) >= 100000} onClick={() => operationId ? setLinkOffset(value => value + PAGE_SIZE) : setFileOffset(value => value + PAGE_SIZE)}>{t("下一页", "Next page")}</button></nav>
    </section>
    {operationId && <section className="business-panel"><h2>{t("关联已有文件", "Link an existing file")}</h2><form className="document-link-form" onSubmit={associate}>
      <div className="field"><label htmlFor="document-existing">{t("已有文件", "Existing file")}</label><select id="document-existing" value={existingFile} required disabled={writesDisabled} onChange={event => setExistingFile(event.target.value)}><option value="">{t("请选择当前账本的文件", "Choose a file in this ledger")}</option>{canSelect.map(file => <option key={file.id} value={file.id}>{file.title} · {file.original_filename}</option>)}</select></div>
      <button className="primary-button" disabled={writesDisabled || !canSelect.some(file => file.id === existingFile)}>{t("关联文件", "Link file")}</button>
    </form><nav className="transaction-pagination" aria-label={t("已有文件分页", "Existing file pages")}><button className="secondary-button" disabled={navDisabled || fileOffset === 0} onClick={() => { setExistingFile(""); setFileOffset(value => Math.max(0, value - PAGE_SIZE)); }}>{t("上一组文件", "Previous files")}</button><span>{t("第", "Page")} {fileOffset / PAGE_SIZE + 1}</span><button className="secondary-button" disabled={navDisabled || files.length < PAGE_SIZE || fileOffset >= 100000} onClick={() => { setExistingFile(""); setFileOffset(value => value + PAGE_SIZE); }}>{t("下一组文件", "Next files")}</button></nav></section>}
  </section>;
}
