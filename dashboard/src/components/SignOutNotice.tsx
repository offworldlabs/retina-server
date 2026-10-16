import { useEffect, useRef } from "react";
import type { RequestFailure } from "../utils/requestFailure";

interface SignOutNoticeProps {
  failure: RequestFailure;
  /** A retry has gone unconfirmed as well, so the words have to change: a
   *  screen reader announces an alert again only when its text does. */
  repeated: boolean;
  /** A retry is in flight. */
  pending: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}

/**
 * Said when the server did not confirm a sign-out.
 *
 * This device has dropped the identity regardless, but the session cookie is
 * httponly and only the server can clear it, so the session may well still be
 * live: a reload would sign the person straight back in. On a shared machine
 * that matters, so the notice stays until a retry goes through or it is
 * dismissed, and says so in plain words rather than as a status code.
 */
export default function SignOutNotice({ failure, repeated, pending, onRetry, onDismiss }: SignOutNoticeProps) {
  const retry = useRef<HTMLButtonElement>(null);
  const dismiss = useRef<HTMLButtonElement>(null);

  // The Sign out item that had focus went with the account menu, which drops
  // focus on the page itself, and Tab would resume from the header rather than
  // reach this. Taken only when nothing holds it: a page that has put focus
  // somewhere since, such as the login card's field, keeps it. Taken again
  // when a retry is refused for good, which takes Try again from under it;
  // Dismiss has it when there is no Try again.
  useEffect(() => {
    const active = document.activeElement;
    if (!active || active === document.body) (retry.current ?? dismiss.current)?.focus();
  }, [failure.retryable]);

  return (
    <div className="signout-notice" role="alert">
      {/* Feather's alert-triangle, in the house style of the header's icons. */}
      <svg
        className="signout-notice-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      <div className="signout-notice-body">
        <p className="signout-notice-title">Sign-out not confirmed</p>
        <p className="signout-notice-text">
          You are signed out on this device, but the server {repeated ? "still has not confirmed" : "did not confirm"}{" "}
          it, so your session may still be active: reloading the page could sign you back in. {failure.message}
        </p>
        {/* Only where trying again can help: a refusal for good would meet
            the same answer. */}
        {failure.retryable && (
          <div className="signout-notice-actions">
            <button
              ref={retry}
              type="button"
              className="btn btn-secondary btn-sm"
              // Not `disabled`, which would drop the focus a keyboard user has
              // on it for as long as the request takes.
              aria-disabled={pending || undefined}
              onClick={() => {
                if (!pending) onRetry();
              }}
            >
              {pending ? "Trying again…" : "Try again"}
            </button>
          </div>
        )}
      </div>
      <button
        ref={dismiss}
        type="button"
        className="signout-notice-dismiss"
        aria-label="Dismiss"
        title="Dismiss"
        onClick={onDismiss}
      >
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
  );
}
