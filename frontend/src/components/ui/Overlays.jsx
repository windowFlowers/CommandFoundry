import { useEffect, useId, useRef } from "react";
import { X } from "lucide-react";

const overlayStack = [];
const focusableSelector = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function isolateOutsideLayer(layer) {
  const mutations = [];
  let current = layer;

  while (current && current !== document.body) {
    const parent = current.parentElement;
    if (!parent) break;

    [...parent.children].forEach((sibling) => {
      if (sibling === current || !(sibling instanceof HTMLElement)) return;
      [sibling, ...sibling.querySelectorAll('[role="dialog"][aria-modal="true"]')].forEach((element) => {
        mutations.push({
          element,
          inert: element.hasAttribute("inert"),
          ariaHidden: element.getAttribute("aria-hidden"),
        });
        element.setAttribute("inert", "");
        element.setAttribute("aria-hidden", "true");
      });
    });
    current = parent;
  }

  return () => {
    mutations.reverse().forEach(({ element, inert, ariaHidden }) => {
      if (inert) element.setAttribute("inert", "");
      else element.removeAttribute("inert");
      if (ariaHidden === null) element.removeAttribute("aria-hidden");
      else element.setAttribute("aria-hidden", ariaHidden);
    });
  };
}

function useOverlayFocus(open, onClose, layerRef, containerRef, initialFocusRef, returnFocusRef) {
  const onCloseRef = useRef(onClose);
  const returnFocusTargetRef = useRef(null);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return undefined;
    const token = Symbol("overlay");
    const activeElement = document.activeElement;
    // StrictMode replays effects after focus has already moved into the overlay.
    // Preserve the last focus target from outside instead of replacing it with
    // an element that will disappear when a conditionally mounted modal closes.
    if (!containerRef.current?.contains(activeElement)) returnFocusTargetRef.current = activeElement;
    overlayStack.push(token);

    const preferred = initialFocusRef?.current;
    const first = containerRef.current?.querySelector(focusableSelector);
    (preferred || first || containerRef.current)?.focus?.();
    const restoreIsolation = isolateOutsideLayer(layerRef.current);

    const getFocusable = () => [...(containerRef.current?.querySelectorAll(focusableSelector) || [])]
      .filter((element) => element.getAttribute("aria-hidden") !== "true" && !element.closest("[inert]"));

    const handleKeyDown = (event) => {
      if (overlayStack.at(-1) !== token) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = getFocusable();
      if (!focusable.length) {
        event.preventDefault();
        containerRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!containerRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    const handleFocusIn = (event) => {
      if (overlayStack.at(-1) !== token || containerRef.current?.contains(event.target)) return;
      const [firstFocusable] = getFocusable();
      (initialFocusRef?.current || firstFocusable || containerRef.current)?.focus?.();
    };

    document.addEventListener("keydown", handleKeyDown, true);
    document.addEventListener("focusin", handleFocusIn, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown, true);
      document.removeEventListener("focusin", handleFocusIn, true);
      const index = overlayStack.lastIndexOf(token);
      if (index >= 0) overlayStack.splice(index, 1);
      restoreIsolation();
      const requestedReturnFocus = returnFocusRef?.current;
      window.requestAnimationFrame(() => {
        const target = requestedReturnFocus || returnFocusTargetRef.current;
        if (target instanceof HTMLElement && document.body.contains(target) && !target.closest("[inert]")) target.focus();
      });
    };
  }, [open, layerRef, containerRef, initialFocusRef, returnFocusRef]);
}

export function Drawer({ open, onClose, eyebrow, title, label, children, className = "", dataUi }) {
  const layerRef = useRef(null);
  const containerRef = useRef(null);
  const closeRef = useRef(null);
  const titleId = useId();
  useOverlayFocus(open, onClose, layerRef, containerRef, closeRef);

  return <div ref={layerRef} className="drawer-layer" aria-hidden={!open} inert={open ? undefined : ""}>
    <div
      className={`drawer-scrim ${open ? "visible" : ""}`}
      onClick={onClose}
      aria-hidden="true"
      data-ui={`${dataUi}-scrim`}
    />
    <aside
      ref={containerRef}
      className={`settings-drawer ${className} ${open ? "open" : ""}`.trim()}
      aria-hidden={!open}
      aria-labelledby={titleId}
      role="dialog"
      aria-modal="true"
      tabIndex={-1}
      inert={open ? undefined : ""}
      data-ui={dataUi}
    >
      <header className="overlay-heading">
        <div><span className="eyebrow">{eyebrow}</span><h2 id={titleId}>{title}</h2></div>
        <button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label={`关闭${label}`}><X size={19} /></button>
      </header>
      {children}
    </aside>
  </div>;
}

export function Modal({ title, children, onClose, actions, dataUi = "modal", descriptionId, initialFocusRef, returnFocusRef, closeDisabled = false }) {
  const layerRef = useRef(null);
  const containerRef = useRef(null);
  const closeRef = useRef(null);
  const titleId = useId();
  useOverlayFocus(true, onClose, layerRef, containerRef, initialFocusRef || closeRef, returnFocusRef);

  return <div ref={layerRef} className="modal-layer" data-ui={`${dataUi}-layer`}>
    <div className="modal-scrim" onClick={closeDisabled ? undefined : onClose} aria-hidden="true" />
    <section ref={containerRef} className="modal-card" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1} data-ui={dataUi}>
      <header><h3 id={titleId}>{title}</h3><button ref={closeRef} className="icon-button" type="button" onClick={onClose} disabled={closeDisabled} aria-label={`关闭${title}`}><X size={18} /></button></header>
      <div className="modal-content">{children}</div>
      {actions && <footer>{actions}</footer>}
    </section>
  </div>;
}
