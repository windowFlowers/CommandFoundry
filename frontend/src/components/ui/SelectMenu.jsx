import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";


function normalizeOption(option, index) {
  if (typeof option === "string") {
    return { value: option, label: option, id: `select-option-${index}` };
  }
  const value = String(option?.value ?? option?.label ?? "");
  return {
    value,
    label: String(option?.label ?? value),
    id: option?.id || `select-option-${index}`,
    disabled: Boolean(option?.disabled),
  };
}


/**
 * A small, keyboard accessible select menu used wherever the desktop UI needs
 * a choice.  It intentionally does not use the platform <select> element so
 * Electron and browser builds share the same visual and interaction model.
 */
export function SelectMenu({
  value,
  options = [],
  onChange,
  id,
  ariaLabel,
  disabled = false,
  dataUi,
  className = "",
  placeholder = "请选择",
}) {
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const listboxRef = useRef(null);
  const listboxId = useId();
  const labelId = useId();
  const normalizedOptions = options.map(normalizeOption).filter((option) => option.value);
  const selectedIndex = Math.max(0, normalizedOptions.findIndex((option) => option.value === String(value ?? "")));
  const selected = normalizedOptions[selectedIndex];
  const [open, setOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(selectedIndex);

  useEffect(() => {
    if (!open) return undefined;
    function closeOnOutside(event) {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutside);
    return () => document.removeEventListener("pointerdown", closeOnOutside);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setHighlightedIndex(selectedIndex);
  }, [open, selectedIndex]);

  useEffect(() => {
    if (!open || !listboxRef.current || highlightedIndex < 0) return;
    const option = listboxRef.current.querySelector(`[data-option-index="${highlightedIndex}"]`);
    option?.scrollIntoView({ block: "nearest" });
  }, [open, highlightedIndex]);

  function moveHighlight(direction) {
    if (!normalizedOptions.length) return;
    const start = highlightedIndex >= 0 ? highlightedIndex : selectedIndex;
    let next = start;
    for (let count = 0; count < normalizedOptions.length; count += 1) {
      next = (next + direction + normalizedOptions.length) % normalizedOptions.length;
      if (!normalizedOptions[next].disabled) {
        setHighlightedIndex(next);
        return;
      }
    }
  }

  function highlightBoundary(direction) {
    const indexes = normalizedOptions.map((option, index) => ({ option, index })).filter(({ option }) => !option.disabled);
    if (!indexes.length) return;
    setHighlightedIndex(direction === "start" ? indexes[0].index : indexes.at(-1).index);
  }

  function choose(option) {
    if (disabled || option.disabled) return;
    onChange?.(option.value);
    setOpen(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  }

  function onTriggerKeyDown(event) {
    if (disabled) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!open) setOpen(true);
      moveHighlight(1);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) setOpen(true);
      moveHighlight(-1);
      return;
    }
    if (event.key === "Home" && open) {
      event.preventDefault();
      highlightBoundary("start");
      return;
    }
    if (event.key === "End" && open) {
      event.preventDefault();
      highlightBoundary("end");
      return;
    }
    if ((event.key === "Enter" || event.key === " ") && open) {
      event.preventDefault();
      const option = normalizedOptions[highlightedIndex];
      if (option) choose(option);
      return;
    }
    if (event.key === "Escape") {
      if (open) {
        event.preventDefault();
        setOpen(false);
      }
      return;
    }
    if (event.key === "Tab" && open) setOpen(false);
  }

  return (
    <div ref={rootRef} className={`select-menu ${className}`.trim()} data-ui={dataUi}>
      <span id={labelId} className="sr-only">{ariaLabel || "选择"}</span>
      <button
        ref={triggerRef}
        className="select-menu-trigger"
        type="button"
        id={id}
        role="combobox"
        aria-label={ariaLabel || "选择"}
        aria-labelledby={ariaLabel ? undefined : labelId}
        aria-controls={listboxId}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-activedescendant={open && normalizedOptions[highlightedIndex] ? `${listboxId}-${normalizedOptions[highlightedIndex].id}` : undefined}
        disabled={disabled}
        data-ui={dataUi}
        onClick={() => { if (!disabled) setOpen((current) => !current); }}
        onKeyDown={onTriggerKeyDown}
      >
        <span className="select-menu-value">{selected?.label || placeholder}</span>
        <ChevronDown className={open ? "rotate" : ""} size={16} aria-hidden="true" />
      </button>
      {open && (
        <div ref={listboxRef} id={listboxId} className="select-menu-listbox" role="listbox" aria-label={ariaLabel || "选项"}>
          {normalizedOptions.map((option, index) => (
            <button
              key={option.id}
              id={`${listboxId}-${option.id}`}
              className={`select-menu-option ${option.value === String(value ?? "") ? "selected" : ""} ${index === highlightedIndex ? "highlighted" : ""}`.trim()}
              type="button"
              role="option"
              aria-selected={option.value === String(value ?? "")}
              disabled={option.disabled}
              data-option-index={index}
              onMouseEnter={() => { if (!option.disabled) setHighlightedIndex(index); }}
              onClick={() => choose(option)}
            >
              <span>{option.label}</span>
              {option.value === String(value ?? "") && <Check size={15} aria-hidden="true" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
