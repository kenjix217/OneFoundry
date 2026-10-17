import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from 'react';

type Size = { width: number; height: number };
const DEFAULT_SIZE: Size = { width: 100, height: 100 };
const clamp = (value: number, min: number) => Math.min(100, Math.max(min, value));

export function useTaskChatSizing() {
  const [expanded, setExpanded] = useState(false);
  const [size, setSize] = useState<Size>(DEFAULT_SIZE);
  const frameRef = useRef<HTMLElement>(null);
  const expandRef = useRef<HTMLButtonElement>(null);
  const priorFocus = useRef<HTMLElement | null>(null);
  const drag = useRef<{ id: number; x: number; y: number; size: Size; width: number; height: number } | null>(null);

  useEffect(() => {
    if (!expanded || !frameRef.current) return;
    const frame = frameRef.current;
    const disabled: { element: HTMLElement; inert: boolean }[] = [];
    let child: HTMLElement = frame;
    while (child.parentElement) {
      for (const sibling of child.parentElement.children) {
        if (sibling instanceof HTMLElement && sibling !== child && !['SCRIPT', 'STYLE'].includes(sibling.tagName)) {
          disabled.push({ element: sibling, inert: sibling.inert }); sibling.inert = true;
        }
      }
      child = child.parentElement;
    }
    expandRef.current?.focus();
    return () => {
      for (const item of disabled) item.element.inert = item.inert;
      if (priorFocus.current?.isConnected) priorFocus.current.focus();
    };
  }, [expanded]);

  function toggle() {
    if (!expanded) priorFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : expandRef.current;
    setExpanded((value) => !value);
  }

  function keyDown(event: KeyboardEvent<HTMLElement>) {
    if (!expanded || event.defaultPrevented) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setExpanded(false); return; }
    if (event.key !== 'Tab') return;
    const controls = [...(frameRef.current?.querySelectorAll<HTMLElement>('button, input, select, textarea, summary, a[href], [tabindex="0"]') ?? [])]
      .filter((element) => !element.matches(':disabled') && !element.closest('[hidden], [inert]') &&
        getComputedStyle(element).display !== 'none' && getComputedStyle(element).visibility !== 'hidden');
    // A closed details element exposes only its summary to keyboard focus.
    const visible = controls.filter((element) => !element.closest('details:not([open])') || element.tagName === 'SUMMARY');
    const first = visible[0]; const last = visible.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }

  function startResize(event: PointerEvent<HTMLButtonElement>) {
    if (expanded || event.button !== 0) return;
    const container = frameRef.current?.querySelector('.task-chat-layout')?.getBoundingClientRect();
    if (!container?.width || !container.height) return;
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, size, width: container.width, height: container.height };
    event.currentTarget.setPointerCapture(event.pointerId); event.preventDefault();
  }

  function resize(event: PointerEvent<HTMLButtonElement>) {
    const value = drag.current;
    if (!value || event.pointerId !== value.id) return;
    setSize({ width: clamp(value.size.width + (event.clientX - value.x) / value.width * 100, 65),
      height: clamp(value.size.height + (event.clientY - value.y) / value.height * 100, 60) });
  }

  function endResize(event: PointerEvent<HTMLButtonElement>, cancel = false) {
    if (!drag.current || event.pointerId !== drag.current.id) return;
    if (cancel) setSize(drag.current.size);
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }

  function resizeKey(event: KeyboardEvent<HTMLButtonElement>) {
    const changes = { ArrowLeft: [-5, 0], ArrowRight: [5, 0], ArrowUp: [0, -5], ArrowDown: [0, 5] }[event.key];
    if (!changes) return;
    event.preventDefault(); setSize((prior) => ({ width: clamp(prior.width + changes[0], 65), height: clamp(prior.height + changes[1], 60) }));
  }

  const style = { '--task-chat-width': `${size.width}%`, '--task-chat-height': `${size.height}%` } as CSSProperties;
  return { expanded, size, setSize, style, frameRef, expandRef, toggle, keyDown,
    collapse: () => setExpanded(false), reset: () => setSize(DEFAULT_SIZE),
    resizeHandle: <button type="button" className="task-chat-resize-handle" aria-label="Resize chat with arrow keys or drag"
      title="Drag to resize; arrow keys also resize" hidden={expanded} onPointerDown={startResize}
      onPointerMove={resize} onPointerUp={(event) => endResize(event)} onPointerCancel={(event) => endResize(event, true)}
      onLostPointerCapture={() => { drag.current = null; }} onKeyDown={resizeKey}>◢</button> };
}

export function TaskChatSizeControls({ sizing }: { sizing: ReturnType<typeof useTaskChatSizing> }) {
  return <>
    <details className="task-chat-size-controls" hidden={sizing.expanded}>
      <summary>Resize chat</summary><div>
        <label>Chat width <input type="range" min="65" max="100" step="1" value={sizing.size.width}
          onChange={(event) => sizing.setSize((prior) => ({ ...prior, width: Number(event.target.value) }))} />
          <output>{Math.round(sizing.size.width)}%</output></label>
        <label>Chat height <input type="range" min="60" max="100" step="1" value={sizing.size.height}
          onChange={(event) => sizing.setSize((prior) => ({ ...prior, height: Number(event.target.value) }))} />
          <output>{Math.round(sizing.size.height)}%</output></label>
        <button type="button" onClick={sizing.reset}>Reset size</button>
        <small>Phones use the available width and height. Size is bounded to keep messages usable.</small>
      </div>
    </details>
    <button ref={sizing.expandRef} className="button button-outline" type="button" aria-pressed={sizing.expanded}
      onClick={sizing.toggle}>{sizing.expanded ? 'Exit full screen' : 'Expand chat'}</button>
  </>;
}
