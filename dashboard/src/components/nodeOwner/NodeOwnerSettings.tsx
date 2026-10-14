import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { HttpError } from "@retina/shared";

import { api } from "../../api/client";
import type { OwnedNode, Publication } from "../../types";
import { PRIVATE_DESCRIPTION, PublicationOptions } from "../LocationPrivacy";
import { Notice } from "../Notice";
import { isGone } from "./gone";
import "./nodeOwnerSettings.css";

// The public answer in LocationPrivacy's words, which that module does not
// export. The test holds this copy to the radio group's.
const PUBLIC_DESCRIPTION = "Shown on the map at an approximate position, displaced by the network's location fuzz";

/** The longest label the server keeps, in characters once trimmed. */
const LABEL_MAX_CHARS = 64;

// What Python's str.strip() takes off, which is how the server trims a label.
// String.prototype.trim() differs at the edges: it also takes U+FEFF, and
// leaves U+001C to U+001F and U+0085.
const SPACE = "\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const EDGE_SPACE = new RegExp(`^[${SPACE}]+|[${SPACE}]+$`, "g");

// What the server refuses in a label: controls, lone surrogates, and the line
// and paragraph separators, which break a one-line label as a control would.
const UNPRINTABLE = /[\p{Cc}\p{Cs}\p{Zl}\p{Zp}]/u;

/** The label a draft asks for, read as the server will read it: trimmed, null
 *  for one that clears the label, or the reason it would be refused. The
 *  server's other refusal, a label that spells a node id, needs the fleet and
 *  is left to its 422. */
function readLabel(draft: string): { label: string | null } | { refused: string } {
  const label = draft.replace(EDGE_SPACE, "");
  if (!label) return { label: null };
  // In code points, as the server counts: an emoji is one character, not two.
  const length = [...label].length;
  if (length > LABEL_MAX_CHARS) {
    return { refused: `A label is at most ${LABEL_MAX_CHARS} characters; this one has ${length}.` };
  }
  if (UNPRINTABLE.test(label)) return { refused: "A label cannot contain control characters." };
  return { label };
}

interface Failure {
  /** `refused`: the server, or the check that mirrors it, turned the value
   *  down. `failed`: anything else, which may pass if tried again. */
  kind: "refused" | "failed";
  message: string;
}

function failureOf(e: unknown, attempt: string): Failure {
  // A 422's detail is the server's reason, written to be shown as it is.
  if (e instanceof HttpError && e.status === 422) return { kind: "refused", message: e.message };
  const reason = e instanceof Error && e.message ? e.message : "no answer";
  return { kind: "failed", message: `Could not ${attempt}: ${reason}` };
}

/** Where focus goes once the render a handler asked for has landed. */
type FocusTarget = "label-edit" | "label-open" | "label-input" | "privacy-change" | "privacy-choice" | "privacy-save";

interface Props {
  node: OwnedNode;
  onSaved: (node: OwnedNode) => void;
  onGone: () => void;
}

/**
 * The owner's label for a node and whether its location is published, each
 * changed in place. `onSaved` receives the node as the server answered it
 * after each change, for the page to refresh what it shows. `onGone` is called
 * when a change finds the node no longer on the account, for the page to take
 * down the owner's view, this form with it, and say so.
 */
export function NodeOwnerSettings({ node, onSaved, onGone }: Props) {
  // Another node is another set of drafts: nothing typed for one carries over,
  // and an answer still in flight for it is not handed back for this one.
  return <Settings key={node.node_id} node={node} onSaved={onSaved} onGone={onGone} />;
}

function Settings({ node, onSaved, onGone }: Props) {
  const id = useId();
  const labelTitleId = `${id}label`;
  const labelHintId = `${id}label-hint`;
  const labelErrorId = `${id}label-error`;
  const privacyTitleId = `${id}privacy`;

  // The node as last heard of: the page's, or the server's answer to a save
  // here, whichever came last. A new one from the page is taken during render,
  // so no frame shows the old one beside it.
  const [seen, setSeen] = useState(node);
  const [current, setCurrent] = useState(node);
  if (node !== seen) {
    setSeen(node);
    setCurrent(node);
  }

  const [labelOpen, setLabelOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [labelSaving, setLabelSaving] = useState(false);
  const [labelFailure, setLabelFailure] = useState<Failure | null>(null);
  const [labelDone, setLabelDone] = useState<string | null>(null);
  // Remounts the refusal, so the same reason given twice is announced twice.
  const [refusals, setRefusals] = useState(0);

  const [privacyOpen, setPrivacyOpen] = useState(false);
  const [choice, setChoice] = useState<Publication>("public");
  const [privacySaving, setPrivacySaving] = useState(false);
  const [privacyFailure, setPrivacyFailure] = useState<Failure | null>(null);
  const [privacyDone, setPrivacyDone] = useState<string | null>(null);

  // One change at a time. Each answers with the whole node, and two in flight
  // could land out of order, the later answer putting back what the earlier
  // one changed.
  const saving = labelSaving || privacySaving;

  const labelEdit = useRef<HTMLButtonElement>(null);
  const labelInput = useRef<HTMLInputElement>(null);
  const privacyChange = useRef<HTMLButtonElement>(null);
  const privacyForm = useRef<HTMLFormElement>(null);
  const privacySave = useRef<HTMLButtonElement>(null);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Set by a handler, taken after the commit it caused, once the element it
  // names is on screen and enabled again.
  const focusNext = useRef<FocusTarget | null>(null);
  useEffect(() => {
    const target = focusNext.current;
    if (!target) return;
    focusNext.current = null;
    if (target === "label-edit") labelEdit.current?.focus();
    else if (target === "label-input") labelInput.current?.focus();
    else if (target === "label-open") {
      // Selected, so typing replaces the label and an arrow key keeps it.
      labelInput.current?.focus();
      labelInput.current?.select();
    }
    else if (target === "privacy-change") privacyChange.current?.focus();
    else if (target === "privacy-save") privacySave.current?.focus();
    else privacyForm.current?.querySelector<HTMLInputElement>("input:checked")?.focus();
  });

  function openLabel() {
    setDraft(current.label ?? "");
    setLabelFailure(null);
    setLabelDone(null);
    setLabelOpen(true);
    focusNext.current = "label-open";
  }

  function closeLabel() {
    setLabelOpen(false);
    setLabelFailure(null);
    focusNext.current = "label-edit";
  }

  async function saveLabel() {
    if (saving) return;
    const read = readLabel(draft);
    if ("refused" in read) {
      setLabelFailure({ kind: "refused", message: read.refused });
      setRefusals((n) => n + 1);
      focusNext.current = "label-input";
      return;
    }
    // Nothing to change, and nothing for the server to record.
    if (read.label === (current.label || null)) {
      closeLabel();
      return;
    }
    setLabelSaving(true);
    setLabelFailure(null);
    let updated: OwnedNode;
    try {
      updated = await api.updateMyNode(current.node_id, { name: read.label });
    } catch (e) {
      if (!alive.current) return;
      setLabelSaving(false);
      if (isGone(e)) {
        onGone();
        return;
      }
      setLabelFailure(failureOf(e, "save the label"));
      setRefusals((n) => n + 1);
      focusNext.current = "label-input";
      return;
    }
    if (!alive.current) return;
    setLabelSaving(false);
    setCurrent(updated);
    setLabelOpen(false);
    setLabelDone(updated.label ? "Label saved." : "Label cleared.");
    focusNext.current = "label-edit";
    onSaved(updated);
  }

  function openPrivacy() {
    setChoice(current.location_private ? "private" : "public");
    setPrivacyFailure(null);
    setPrivacyDone(null);
    setPrivacyOpen(true);
    focusNext.current = "privacy-choice";
  }

  function closePrivacy() {
    setPrivacyOpen(false);
    setPrivacyFailure(null);
    focusNext.current = "privacy-change";
  }

  async function savePrivacy() {
    if (saving) return;
    const makePrivate = choice === "private";
    if (makePrivate === current.location_private) {
      closePrivacy();
      return;
    }
    setPrivacySaving(true);
    setPrivacyFailure(null);
    let updated: OwnedNode;
    try {
      updated = await api.updateMyNode(current.node_id, { location_private: makePrivate });
    } catch (e) {
      if (!alive.current) return;
      setPrivacySaving(false);
      if (isGone(e)) {
        onGone();
        return;
      }
      setPrivacyFailure(failureOf(e, "change location privacy"));
      focusNext.current = "privacy-save";
      return;
    }
    if (!alive.current) return;
    setPrivacySaving(false);
    setCurrent(updated);
    setPrivacyOpen(false);
    setPrivacyDone(updated.location_private ? "Location is now private." : "Location is now public.");
    focusNext.current = "privacy-change";
    onSaved(updated);
  }

  function submitLabel(e: FormEvent) {
    e.preventDefault();
    void saveLabel();
  }

  function submitPrivacy(e: FormEvent) {
    e.preventDefault();
    void savePrivacy();
  }

  // Escape belongs to the form it closes, not to a drawer or a map behind it.
  // A save in flight cannot be called back, so it is waited for.
  function cancelOnEscape(e: KeyboardEvent, close: () => void, pending: boolean) {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    if (!pending) close();
  }

  const fieldRefused = labelFailure?.kind === "refused";
  const clearing = !draft.replace(EDGE_SPACE, "") && !!current.label;
  // With no label, the page shows the node's own name, or its ref when it has none.
  const shownInstead = current.label ? null : (current.name ?? current.node_ref);

  return (
    <div className="card">
      <div className="card-header">
        <h3>Label and privacy</h3>
      </div>
      <div className="card-body">
        <div className="node-settings-row">
          <div className="node-settings-main">
            <h4 className="node-settings-title" id={labelTitleId}>
              Label
            </h4>
            {labelOpen ? (
              <form
                className="node-settings-form"
                onSubmit={submitLabel}
                onKeyDown={(e) => cancelOnEscape(e, closeLabel, labelSaving)}
                aria-busy={labelSaving}
                noValidate
              >
                {/* No maxLength: a browser counts it in UTF-16 units, an emoji
                    as two, and would stop short a label the server keeps.
                    readLabel counts as the server does, on Save. */}
                <input
                  ref={labelInput}
                  className="input"
                  value={draft}
                  autoComplete="off"
                  disabled={labelSaving}
                  aria-labelledby={labelTitleId}
                  aria-invalid={fieldRefused || undefined}
                  aria-describedby={fieldRefused ? `${labelErrorId} ${labelHintId}` : labelHintId}
                  onChange={(e) => {
                    setDraft(e.target.value);
                    if (fieldRefused) setLabelFailure(null);
                  }}
                />
                {fieldRefused && (
                  <p key={refusals} id={labelErrorId} className="node-settings-error" role="alert">
                    {labelFailure.message}
                  </p>
                )}
                <p id={labelHintId} className="node-settings-note">
                  Up to {LABEL_MAX_CHARS} characters, seen only by you. Leave it empty to clear the label.
                </p>
                {labelFailure?.kind === "failed" && (
                  <Notice tone="error" onRetry={() => void saveLabel()}>
                    {labelFailure.message}
                  </Notice>
                )}
                <div className="btn-row">
                  <button type="submit" className="btn btn-primary" disabled={saving}>
                    {labelSaving ? "Saving…" : clearing ? "Clear label" : "Save"}
                  </button>
                  <button type="button" className="btn btn-secondary" disabled={labelSaving} onClick={closeLabel}>
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <>
                <p className={current.label ? "node-settings-value" : "node-settings-value unset"}>
                  {current.label || "No label"}
                </p>
                <p className="node-settings-note">
                  {current.label
                    ? "Your pages show it in place of the node's own name. Only you see it."
                    : shownInstead
                      ? `Your pages show the node as ${shownInstead}. A label you add replaces that, for you alone.`
                      : "A label you add names the node on your pages, for you alone."}
                </p>
              </>
            )}
            <p className="node-settings-status" role="status">
              {labelDone && (
                <>
                  <Tick />
                  {labelDone}
                </>
              )}
            </p>
          </div>
          {!labelOpen && (
            <button
              ref={labelEdit}
              type="button"
              className="btn btn-secondary btn-sm"
              aria-label="Edit label"
              onClick={openLabel}
            >
              Edit
            </button>
          )}
        </div>

        <div className="node-settings-row">
          <div className="node-settings-main">
            <h4 className="node-settings-title" id={privacyTitleId}>
              Location
            </h4>
            {privacyOpen ? (
              <form
                ref={privacyForm}
                className="node-settings-form"
                onSubmit={submitPrivacy}
                onKeyDown={(e) => cancelOnEscape(e, closePrivacy, privacySaving)}
                aria-busy={privacySaving}
                noValidate
              >
                <div role="radiogroup" aria-labelledby={privacyTitleId}>
                  <PublicationOptions
                    name={`${id}publication`}
                    value={choice}
                    disabled={privacySaving}
                    onChange={setChoice}
                  />
                </div>
                {privacyFailure && (
                  <Notice
                    tone="error"
                    onRetry={privacyFailure.kind === "failed" ? () => void savePrivacy() : undefined}
                  >
                    {privacyFailure.message}
                  </Notice>
                )}
                <div className="btn-row">
                  <button ref={privacySave} type="submit" className="btn btn-primary" disabled={saving}>
                    {privacySaving ? "Saving…" : "Save"}
                  </button>
                  <button type="button" className="btn btn-secondary" disabled={privacySaving} onClick={closePrivacy}>
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <>
                <p className="node-settings-value">{current.location_private ? "Private" : "Public"}</p>
                <p className="node-settings-note">
                  {current.location_private ? PRIVATE_DESCRIPTION : PUBLIC_DESCRIPTION}
                </p>
              </>
            )}
            <p className="node-settings-status" role="status">
              {privacyDone && (
                <>
                  <Tick />
                  {privacyDone}
                </>
              )}
            </p>
          </div>
          {!privacyOpen && (
            <button
              ref={privacyChange}
              type="button"
              className="btn btn-secondary btn-sm"
              aria-label="Change location privacy"
              onClick={openPrivacy}
            >
              Change
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Tick() {
  return (
    <svg
      className="node-settings-tick"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M3.5 8.5l3 3 6-7" />
    </svg>
  );
}
