import { useEffect, useRef, type RefObject } from 'react';

/** Follow new conversation content until the reader deliberately scrolls away. */
export function useConversationFollowControl(key: string | null): {
  transcript: RefObject<HTMLDivElement | null>; pauseForReading: () => void;
} {
  const transcript = useRef<HTMLDivElement>(null);
  const paused = useRef(false);
  const following = useRef(true);
  const resumeIntent = useRef(false);
  useEffect(() => {
    const node = transcript.current;
    if (!node) return;
    paused.current = false;
    following.current = true;
    resumeIntent.current = false;
    const atBottom = () => node.scrollHeight - node.scrollTop - node.clientHeight <= 80;
    const scrollToBottom = () => { if (following.current && !paused.current) node.scrollTop = node.scrollHeight; };
    const onScroll = () => {
      if (paused.current) {
        if (resumeIntent.current && atBottom()) {
          paused.current = false; following.current = true; resumeIntent.current = false;
        }
      } else following.current = atBottom();
    };
    const onScrollIntent = () => { resumeIntent.current = true; };
    const onKeyDown = (event: KeyboardEvent) => {
      if (['ArrowDown', 'PageDown', 'End', ' '].includes(event.key)) onScrollIntent();
    };
    node.addEventListener('scroll', onScroll, { passive: true });
    node.addEventListener('wheel', onScrollIntent, { passive: true });
    node.addEventListener('touchstart', onScrollIntent, { passive: true });
    node.addEventListener('pointerdown', onScrollIntent, { passive: true });
    node.addEventListener('keydown', onKeyDown);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(scrollToBottom);
    if (observer) {
      observer.observe(node);
      for (const child of node.children) observer.observe(child);
    }
    const mutations = new MutationObserver(() => {
      if (observer) for (const child of node.children) observer.observe(child);
      scrollToBottom();
    });
    mutations.observe(node, { childList: true, subtree: true, characterData: true });
    scrollToBottom();
    return () => {
      node.removeEventListener('scroll', onScroll);
      node.removeEventListener('wheel', onScrollIntent);
      node.removeEventListener('touchstart', onScrollIntent);
      node.removeEventListener('pointerdown', onScrollIntent);
      node.removeEventListener('keydown', onKeyDown);
      observer?.disconnect(); mutations.disconnect();
    };
  }, [key]);
  return { transcript, pauseForReading: () => {
    paused.current = true; following.current = false; resumeIntent.current = false;
  } };
}

export function useConversationFollow(key: string | null): RefObject<HTMLDivElement | null> {
  return useConversationFollowControl(key).transcript;
}
