import { useEffect, useState } from "react";
import { BrainCircuit, Check, LoaderCircle, RotateCcw, X } from "lucide-react";


export function ToastRegion({ toast, onDismiss, onUndo, onView }) {
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    setPaused(false);
  }, [toast?.id]);

  useEffect(() => {
    if (!toast || paused || toast.working) return undefined;
    const timeout = window.setTimeout(onDismiss, 8000);
    return () => window.clearTimeout(timeout);
  }, [toast, paused, onDismiss]);

  if (!toast) return <div className="toast-region" aria-live="polite" aria-atomic="true" />;
  const canUndo = toast.undoableMemoryIds?.length > 0;

  return (
    <div className="toast-region" aria-live="polite" aria-atomic="true">
      <section
        className="memory-toast"
        role="status"
        onMouseEnter={() => setPaused(true)}
        onMouseLeave={() => setPaused(false)}
        onFocusCapture={() => setPaused(true)}
        onBlurCapture={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false);
        }}
        data-ui="memory-toast"
      >
        <span className="toast-mark" aria-hidden="true">
          {toast.working ? <LoaderCircle className="spin" size={17} /> : toast.operation === "UNDO" ? <Check size={17} /> : <BrainCircuit size={17} />}
        </span>
        <strong>{toast.message}</strong>
        {toast.memoryIds?.length > 0 && <button type="button" onClick={onView}>查看</button>}
        {canUndo && <button type="button" onClick={onUndo} disabled={toast.working}><RotateCcw size={14} />{toast.working ? "撤销中" : "撤销"}</button>}
        <button className="toast-close" type="button" onClick={onDismiss} aria-label="关闭记忆提示"><X size={15} /></button>
      </section>
    </div>
  );
}
