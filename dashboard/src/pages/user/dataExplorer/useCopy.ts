import { useEffect, useRef, useState } from "react";
import { writeClipboard } from "../../../utils/clipboard";

/** How long a copy's outcome stays on its button before the label returns. */
export const COPY_FEEDBACK_MS = 1200;

/** A clipboard write whose outcome is reported on the button that asked for
 *  it, since nothing else on the page changes when a copy lands. The outcome
 *  is null when the button should show its own label. */
export function useCopy(): [string | null, (text: string) => Promise<void>] {
  const [outcome, setOutcome] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const copy = async (text: string) => {
    const result = (await writeClipboard(text)) ? "Copied" : "Copy failed";
    setOutcome(result);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setOutcome(null), COPY_FEEDBACK_MS);
  };

  return [outcome, copy];
}
