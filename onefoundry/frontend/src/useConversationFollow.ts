import { useEffect, useRef, type RefObject } from 'react';

/** Follow new conversation content until the reader deliberately scrolls away. */
export function useConversationFollow(key: string | null): RefObject<HTMLDivElement | null> {
  const transcript = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = transcript.current;
    if (!node) return;
    let following = true;
    const atBottom = () => node.scrollHeight - node.scrollTop - node.clientHeight <= 80;
    const scrollToBottom = () => { if (following) node.scrollTop = node.scrollHeight; };
    const onScroll = () => { following = atBottom(); };
    node.addEventListener('scroll', onScroll, { passive: true });
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
    return () => { node.removeEventListener('scroll', onScroll); observer?.disconnect(); mutations.disconnect(); };
  }, [key]);
  return transcript;
}
