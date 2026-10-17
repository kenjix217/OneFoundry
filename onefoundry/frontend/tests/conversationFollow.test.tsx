import { expect, it } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { useConversationFollow } from '../src/useConversationFollow';

function Transcript({ conversation, text }: { conversation: string; text: string }) {
  const ref = useConversationFollow(conversation);
  return <div ref={ref}><p>{text}</p></div>;
}

it('follows appended text until the reader scrolls away, then follows a newly selected conversation', async () => {
  const result = render(<Transcript conversation="first" text="First" />);
  const node = result.container.firstElementChild as HTMLDivElement;
  Object.defineProperties(node, {
    scrollHeight: { configurable: true, get: () => 600 },
    clientHeight: { configurable: true, get: () => 100 },
  });
  result.rerender(<Transcript conversation="first" text="First and more" />);
  await waitFor(() => expect(node.scrollTop).toBe(600));
  node.scrollTop = 100;
  fireEvent.scroll(node);
  result.rerender(<Transcript conversation="first" text="First and even more" />);
  await waitFor(() => expect(node.scrollTop).toBe(100));
  result.rerender(<Transcript conversation="second" text="Different conversation" />);
  await waitFor(() => expect(node.scrollTop).toBe(600));
});
