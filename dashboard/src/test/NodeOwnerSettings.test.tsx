import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpError } from "@retina/shared";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { PRIVATE_DESCRIPTION } from "../components/LocationPrivacy";
import { NodeOwnerSettings } from "../components/nodeOwner/NodeOwnerSettings";
import type { OwnedNode } from "../types";
import { ownedNode } from "./fixtures/ownedNode";

vi.mock("../api/client", () => ({ api: { updateMyNode: vi.fn() } }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const NODE_ID = "ret0a1b2c3d";
const NODE_REF = "nde0a1b2c3d";

function renderSettings(node: OwnedNode = ownedNode({ name: NODE_REF })) {
  const onSaved = vi.fn();
  const onGone = vi.fn();
  const view = render(<NodeOwnerSettings node={node} onSaved={onSaved} onGone={onGone} />);
  return { ...view, onSaved, onGone };
}

const editButton = () => screen.getByRole("button", { name: "Edit label" });
const changeButton = () => screen.getByRole("button", { name: "Change location privacy" });
const field = () => screen.getByRole("textbox", { name: "Label" });
const saveButton = () => screen.getByRole("button", { name: "Save" });

function type(value: string) {
  fireEvent.change(field(), { target: { value } });
}

/** Types as a browser does, a character at a time, stopping where the field's
 *  maxLength would, which a browser counts in UTF-16 code units. jsdom takes a
 *  value set from a script whatever its length. */
function typeAsABrowser(text: string) {
  const input = field() as HTMLInputElement;
  for (const character of text) {
    const value = input.value + character;
    if (input.maxLength >= 0 && value.length > input.maxLength) return;
    fireEvent.change(input, { target: { value } });
  }
}

beforeEach(() => {
  vi.mocked(api.updateMyNode).mockReset();
});

describe("the label", () => {
  it("shows the owner's label and says only they see it", () => {
    renderSettings(ownedNode({ label: "Roof", name: "Roof" }));
    expect(screen.getByText("Roof")).toBeInTheDocument();
    expect(screen.getByText(/only you see it/i)).toBeInTheDocument();
    expect(screen.queryByText("No label")).not.toBeInTheDocument();
  });

  it("says when none is set and what the owner's pages show instead", () => {
    renderSettings(ownedNode({ label: null, name: NODE_REF }));
    expect(screen.getByText("No label")).toBeInTheDocument();
    expect(screen.getByText(`Your pages show the node as ${NODE_REF}. A label you add replaces that, for you alone.`))
      .toBeInTheDocument();
  });

  it("opens a labelled field in a real form, with the label selected", () => {
    renderSettings(ownedNode({ label: "Roof", name: "Roof" }));
    fireEvent.click(editButton());
    const input = field() as HTMLInputElement;
    expect(input).toHaveFocus();
    expect(input).toHaveValue("Roof");
    expect([input.selectionStart, input.selectionEnd]).toEqual([0, 4]);
    // Enter in the field submits: the Save button is the form's submitter.
    expect(saveButton()).toHaveAttribute("type", "submit");
    expect(saveButton().closest("form")).toBe(input.closest("form"));
  });

  it("saves the trimmed label, confirms it, hands back the server's node and returns focus to Edit", async () => {
    const answer = ownedNode({ label: "Roof", name: "Roof" });
    vi.mocked(api.updateMyNode).mockResolvedValue(answer);
    const { onSaved } = renderSettings();
    fireEvent.click(editButton());
    type("  Roof  ");
    fireEvent.submit(field().closest("form")!);

    expect(await screen.findByText("Label saved.")).toHaveAttribute("role", "status");
    expect(api.updateMyNode).toHaveBeenCalledTimes(1);
    expect(api.updateMyNode).toHaveBeenCalledWith(NODE_ID, { name: "Roof" });
    expect(onSaved).toHaveBeenCalledTimes(1);
    expect(onSaved.mock.calls[0][0]).toBe(answer);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText("Roof")).toBeInTheDocument();
    expect(editButton()).toHaveFocus();
  });

  it("clears the label with an empty value, sending a null name", async () => {
    vi.mocked(api.updateMyNode).mockResolvedValue(ownedNode({ label: null, name: NODE_REF }));
    renderSettings(ownedNode({ label: "Roof", name: "Roof" }));
    fireEvent.click(editButton());
    type("   ");
    fireEvent.click(screen.getByRole("button", { name: "Clear label" }));

    expect(await screen.findByText("Label cleared.")).toHaveAttribute("role", "status");
    expect(api.updateMyNode).toHaveBeenCalledWith(NODE_ID, { name: null });
    expect(screen.getByText("No label")).toBeInTheDocument();
  });

  it("trims what the server trims, which is not what String.trim takes", async () => {
    vi.mocked(api.updateMyNode).mockResolvedValue(ownedNode({ label: "Roof", name: "Roof" }));
    renderSettings();
    fireEvent.click(editButton());
    // U+001F and U+0085 are space to Python's strip() and not to trim().
    type("\u001f\u0085Roof　");
    fireEvent.click(saveButton());
    await screen.findByText("Label saved.");
    expect(api.updateMyNode).toHaveBeenCalledWith(NODE_ID, { name: "Roof" });
  });

  it("refuses a label over 64 characters without asking the server", () => {
    renderSettings();
    fireEvent.click(editButton());
    type("x".repeat(65));
    fireEvent.click(saveButton());

    const refusal = screen.getByRole("alert");
    expect(refusal).toHaveTextContent("A label is at most 64 characters; this one has 65.");
    expect(field()).toHaveAttribute("aria-invalid", "true");
    expect(field().getAttribute("aria-describedby")?.split(" ")).toContain(refusal.id);
    expect(field()).toHaveFocus();
    expect(api.updateMyNode).not.toHaveBeenCalled();
  });

  it("takes as many emoji as the server keeps, typed a key at a time", async () => {
    const label = "\u{1F4E1}".repeat(64);
    vi.mocked(api.updateMyNode).mockResolvedValue(ownedNode({ label, name: label }));
    renderSettings();
    fireEvent.click(editButton());
    // Space at either end is trimmed before the server counts, as here.
    typeAsABrowser(` ${label} `);
    expect(field()).toHaveValue(` ${label} `);
    fireEvent.click(saveButton());

    await screen.findByText("Label saved.");
    expect(api.updateMyNode).toHaveBeenCalledWith(NODE_ID, { name: label });
  });

  it("refuses the 65th emoji typed with the form's own message, without asking the server", () => {
    renderSettings();
    fireEvent.click(editButton());
    typeAsABrowser("\u{1F4E1}".repeat(65));
    fireEvent.click(saveButton());

    expect(screen.getByRole("alert")).toHaveTextContent("A label is at most 64 characters; this one has 65.");
    expect(field()).toHaveAttribute("aria-invalid", "true");
    expect(api.updateMyNode).not.toHaveBeenCalled();
  });

  it("counts characters as the server does, an emoji as one", async () => {
    const label = "\u{1F4E1}".repeat(64);
    vi.mocked(api.updateMyNode).mockResolvedValue(ownedNode({ label, name: label }));
    renderSettings();
    fireEvent.click(editButton());
    type(label);
    fireEvent.click(saveButton());
    await screen.findByText("Label saved.");
    expect(api.updateMyNode).toHaveBeenCalledWith(NODE_ID, { name: label });
  });

  it.each([
    ["a tab", "Roof\tantenna"],
    ["a bell", "Roof\u0007"],
    ["a line separator", "Roof antenna"],
  ])("refuses %s without asking the server", (_what, value) => {
    renderSettings();
    fireEvent.click(editButton());
    type(value);
    fireEvent.click(saveButton());
    expect(screen.getByRole("alert")).toHaveTextContent("A label cannot contain control characters.");
    expect(field()).toHaveValue(value);
    expect(api.updateMyNode).not.toHaveBeenCalled();
  });

  it("shows the server's refusal under the field, keeping the value, the form and the focus", async () => {
    vi.mocked(api.updateMyNode).mockRejectedValue(
      new HttpError(422, "Unprocessable Entity", { detail: "A name cannot be a node id." }),
    );
    const { onSaved } = renderSettings();
    fireEvent.click(editButton());
    type(NODE_ID);
    fireEvent.click(saveButton());

    const refusal = await screen.findByRole("alert");
    expect(refusal).toHaveTextContent("A name cannot be a node id.");
    expect(field()).toHaveValue(NODE_ID);
    expect(field()).toHaveAttribute("aria-invalid", "true");
    expect(field().getAttribute("aria-describedby")?.split(" ")).toContain(refusal.id);
    expect(field()).toHaveFocus();
    expect(onSaved).not.toHaveBeenCalled();

    // Typing again takes the refusal away.
    type("Mast");
    expect(field()).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("announces the same refusal again when it is given again", () => {
    renderSettings();
    fireEvent.click(editButton());
    type("x".repeat(65));
    fireEvent.click(saveButton());
    const first = screen.getByRole("alert");
    fireEvent.click(saveButton());
    // A new element, so a screen reader reads it out a second time.
    expect(screen.getByRole("alert")).not.toBe(first);
  });

  it("disables the controls and reads Saving… while the save is pending", async () => {
    const pending = deferred<OwnedNode>();
    vi.mocked(api.updateMyNode).mockReturnValue(pending.promise);
    renderSettings();
    fireEvent.click(editButton());
    type("Roof");
    fireEvent.click(saveButton());

    expect(field()).toBeDisabled();
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(field().closest("form")).toHaveAttribute("aria-busy", "true");
    // A save in flight cannot be called back.
    fireEvent.keyDown(field(), { key: "Escape" });
    expect(field()).toBeInTheDocument();
    fireEvent.submit(field().closest("form")!);
    expect(api.updateMyNode).toHaveBeenCalledTimes(1);

    pending.resolve(ownedNode({ label: "Roof", name: "Roof" }));
    expect(await screen.findByText("Label saved.")).toBeInTheDocument();
  });

  it("cancels with Escape without a request and returns focus to Edit", () => {
    renderSettings(ownedNode({ label: "Roof", name: "Roof" }));
    fireEvent.click(editButton());
    type("Mast");
    fireEvent.keyDown(field(), { key: "Escape" });

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(editButton()).toHaveFocus();
    expect(screen.getByText("Roof")).toBeInTheDocument();
    expect(api.updateMyNode).not.toHaveBeenCalled();
    // The abandoned draft is gone too.
    fireEvent.click(editButton());
    expect(field()).toHaveValue("Roof");
  });

  it("cancels with the Cancel button the same way", () => {
    renderSettings();
    fireEvent.click(editButton());
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(editButton()).toHaveFocus();
  });

  it("closes without a request when nothing changed", () => {
    renderSettings(ownedNode({ label: "Roof", name: "Roof" }));
    fireEvent.click(editButton());
    type(" Roof ");
    fireEvent.click(saveButton());
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(editButton()).toHaveFocus();
    expect(api.updateMyNode).not.toHaveBeenCalled();
  });

  it("hands a 404 to the page, which takes the owner's view down, and says nothing of its own", async () => {
    vi.mocked(api.updateMyNode).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    const { onSaved, onGone } = renderSettings();
    fireEvent.click(editButton());
    type("Roof");
    fireEvent.click(saveButton());

    await waitFor(() => expect(onGone).toHaveBeenCalledTimes(1));
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/no longer on your account/)).not.toBeInTheDocument();
    expect(field()).not.toHaveAttribute("aria-invalid");
  });

  it("gives any other failure's message with a way to try again", async () => {
    vi.mocked(api.updateMyNode)
      .mockRejectedValueOnce(new HttpError(503, "Service Unavailable", null))
      .mockResolvedValueOnce(ownedNode({ label: "Roof", name: "Roof" }));
    renderSettings();
    fireEvent.click(editButton());
    type("Roof");
    fireEvent.click(saveButton());

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("Could not save the label: 503 Service Unavailable");
    expect(field()).toHaveValue("Roof");
    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Label saved.")).toBeInTheDocument();
    expect(api.updateMyNode).toHaveBeenCalledTimes(2);
    expect(api.updateMyNode).toHaveBeenLastCalledWith(NODE_ID, { name: "Roof" });
  });
});

describe("location privacy", () => {
  it("describes each answer in the radio group's own words", () => {
    const { unmount } = renderSettings(ownedNode({ location_private: false }));
    expect(screen.getByText("Public")).toBeInTheDocument();
    const shown = screen.getByText(/^Shown on the map/).textContent;
    fireEvent.click(changeButton());
    const option = screen.getByRole("radio", { name: /^Public/ }).closest("label")!;
    expect(option.querySelector(".privacy-option-desc")?.textContent).toBe(shown);
    unmount();

    renderSettings(ownedNode({ location_private: true }));
    expect(screen.getByText("Private")).toBeInTheDocument();
    expect(screen.getByText(PRIVATE_DESCRIPTION)).toBeInTheDocument();
  });

  it("makes the location private after a pick and a confirm", async () => {
    const answer = ownedNode({ location_private: true });
    vi.mocked(api.updateMyNode).mockResolvedValue(answer);
    const { onSaved } = renderSettings(ownedNode({ location_private: false }));
    fireEvent.click(changeButton());
    expect(screen.getByRole("radiogroup", { name: "Location" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /^Public/ })).toHaveFocus();

    // Picking is not saving: the public map changes only on Save.
    fireEvent.click(screen.getByRole("radio", { name: /^Private/ }));
    expect(api.updateMyNode).not.toHaveBeenCalled();
    fireEvent.click(saveButton());

    expect(await screen.findByText("Location is now private.")).toHaveAttribute("role", "status");
    expect(api.updateMyNode).toHaveBeenCalledWith(NODE_ID, { location_private: true });
    expect(onSaved.mock.calls[0][0]).toBe(answer);
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.getByText("Private")).toBeInTheDocument();
    expect(changeButton()).toHaveFocus();
  });

  it("makes the location public again", async () => {
    vi.mocked(api.updateMyNode).mockResolvedValue(ownedNode({ location_private: false }));
    renderSettings(ownedNode({ location_private: true }));
    fireEvent.click(changeButton());
    fireEvent.click(screen.getByRole("radio", { name: /^Public/ }));
    fireEvent.click(saveButton());
    expect(await screen.findByText("Location is now public.")).toBeInTheDocument();
    expect(api.updateMyNode).toHaveBeenCalledWith(NODE_ID, { location_private: false });
  });

  it("disables the choice and reads Saving… while the change is pending", async () => {
    const pending = deferred<OwnedNode>();
    vi.mocked(api.updateMyNode).mockReturnValue(pending.promise);
    renderSettings();
    fireEvent.click(changeButton());
    fireEvent.click(screen.getByRole("radio", { name: /^Private/ }));
    fireEvent.click(saveButton());

    for (const radio of screen.getAllByRole("radio")) expect(radio).toBeDisabled();
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    pending.resolve(ownedNode({ location_private: true }));
    expect(await screen.findByText("Location is now private.")).toBeInTheDocument();
  });

  it("shows a failed change inline with a way to try again", async () => {
    vi.mocked(api.updateMyNode)
      .mockRejectedValueOnce(new HttpError(500, "Internal Server Error", null))
      .mockResolvedValueOnce(ownedNode({ location_private: true }));
    const { onSaved } = renderSettings();
    fireEvent.click(changeButton());
    fireEvent.click(screen.getByRole("radio", { name: /^Private/ }));
    fireEvent.click(saveButton());

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("Could not change location privacy: 500 Internal Server Error");
    // The pick stands, and focus is back on the button that was pressed.
    expect(screen.getByRole("radio", { name: /^Private/ })).toBeChecked();
    expect(saveButton()).toHaveFocus();
    expect(onSaved).not.toHaveBeenCalled();

    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Location is now private.")).toBeInTheDocument();
    expect(onSaved).toHaveBeenCalledTimes(1);
  });

  it("hands a 404 to the page, which takes the owner's view down, and says nothing of its own", async () => {
    vi.mocked(api.updateMyNode).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    const { onSaved, onGone } = renderSettings();
    fireEvent.click(changeButton());
    fireEvent.click(screen.getByRole("radio", { name: /^Private/ }));
    fireEvent.click(saveButton());

    await waitFor(() => expect(onGone).toHaveBeenCalledTimes(1));
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("cancels with Escape without a request and returns focus to Change", () => {
    renderSettings();
    fireEvent.click(changeButton());
    const privateOption = screen.getByRole("radio", { name: /^Private/ });
    fireEvent.click(privateOption);
    fireEvent.keyDown(privateOption, { key: "Escape" });
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(changeButton()).toHaveFocus();
    expect(screen.getByText("Public")).toBeInTheDocument();
    expect(api.updateMyNode).not.toHaveBeenCalled();
  });

  it("closes without a request when the answer is unchanged", () => {
    renderSettings(ownedNode({ location_private: true }));
    fireEvent.click(changeButton());
    fireEvent.click(saveButton());
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(api.updateMyNode).not.toHaveBeenCalled();
  });

  it("waits for a label save in flight before saving, so neither answer undoes the other", async () => {
    const pending = deferred<OwnedNode>();
    vi.mocked(api.updateMyNode).mockReturnValueOnce(pending.promise);
    renderSettings();
    fireEvent.click(editButton());
    type("Roof");
    fireEvent.click(saveButton());
    fireEvent.click(changeButton());
    fireEvent.click(screen.getByRole("radio", { name: /^Private/ }));

    const privacySave = within(screen.getByRole("radio", { name: /^Private/ }).closest("form")!).getByRole("button", {
      name: "Save",
    });
    expect(privacySave).toBeDisabled();
    pending.resolve(ownedNode({ label: "Roof", name: "Roof" }));
    await screen.findByText("Label saved.");
    expect(privacySave).toBeEnabled();
  });
});

describe("under StrictMode, as main.tsx mounts the console", () => {
  it("moves focus, saves once and confirms, through effects run twice", async () => {
    vi.mocked(api.updateMyNode)
      .mockRejectedValueOnce(new HttpError(422, "Unprocessable Entity", { detail: "A name cannot be a node id." }))
      .mockResolvedValueOnce(ownedNode({ label: "Roof", name: "Roof" }));
    const onSaved = vi.fn();
    render(
      <StrictMode>
        <NodeOwnerSettings node={ownedNode({ name: NODE_REF })} onSaved={onSaved} onGone={vi.fn()} />
      </StrictMode>,
    );
    fireEvent.click(editButton());
    expect(field()).toHaveFocus();
    type(NODE_ID);
    fireEvent.click(saveButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("A name cannot be a node id.");
    expect(field()).toHaveFocus();

    type("Roof");
    fireEvent.click(saveButton());
    expect(await screen.findByText("Label saved.")).toBeInTheDocument();
    expect(onSaved).toHaveBeenCalledTimes(1);
    expect(editButton()).toHaveFocus();
  });
});

describe("a new node from the page", () => {
  it("updates what is shown", () => {
    const { rerender, onSaved, onGone } = renderSettings(ownedNode({ label: null, name: NODE_REF }));
    rerender(<NodeOwnerSettings node={ownedNode({ label: "Mast", name: "Mast", location_private: true })} onSaved={onSaved} onGone={onGone} />);
    expect(screen.getByText("Mast")).toBeInTheDocument();
    expect(screen.getByText("Private")).toBeInTheDocument();
    expect(screen.queryByText("No label")).not.toBeInTheDocument();
  });

  it("keeps a draft and a confirmation when the page refreshes the same node", async () => {
    const answer = ownedNode({ label: "Roof", name: "Roof" });
    vi.mocked(api.updateMyNode).mockResolvedValue(answer);
    const { rerender, onSaved, onGone } = renderSettings();
    fireEvent.click(editButton());
    type("Ro");
    rerender(<NodeOwnerSettings node={ownedNode({ name: NODE_REF, status: "offline" })} onSaved={onSaved} onGone={onGone} />);
    expect(field()).toHaveValue("Ro");

    type("Roof");
    fireEvent.click(saveButton());
    await screen.findByText("Label saved.");
    // What the page does with onSaved: pass the answer straight back.
    rerender(<NodeOwnerSettings node={onSaved.mock.calls[0][0]} onSaved={onSaved} onGone={onGone} />);
    expect(screen.getByText("Label saved.")).toBeInTheDocument();
    expect(screen.getByText("Roof")).toBeInTheDocument();
  });

  it("starts afresh for another node", () => {
    const { rerender, onSaved, onGone } = renderSettings();
    fireEvent.click(editButton());
    type("Roof");
    rerender(<NodeOwnerSettings node={ownedNode({ node_id: "ret9f8e7d6c", label: "Shed", name: "Shed" })} onSaved={onSaved} onGone={onGone} />);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText("Shed")).toBeInTheDocument();
  });

  it("hands nothing back for a node the page has moved on from", async () => {
    const pending = deferred<OwnedNode>();
    vi.mocked(api.updateMyNode).mockReturnValueOnce(pending.promise);
    const { rerender, onSaved, onGone } = renderSettings();
    fireEvent.click(editButton());
    type("Roof");
    fireEvent.click(saveButton());
    rerender(<NodeOwnerSettings node={ownedNode({ node_id: "ret9f8e7d6c", name: "Shed" })} onSaved={onSaved} onGone={onGone} />);
    pending.resolve(ownedNode({ label: "Roof", name: "Roof" }));
    await pending.promise;
    await Promise.resolve();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.queryByText("Label saved.")).not.toBeInTheDocument();
  });

  it("hands no 404 back for a node the page has moved on from", async () => {
    const pending = deferred<OwnedNode>();
    vi.mocked(api.updateMyNode).mockReturnValueOnce(pending.promise);
    const { rerender, onSaved, onGone } = renderSettings();
    fireEvent.click(editButton());
    type("Roof");
    fireEvent.click(saveButton());
    rerender(<NodeOwnerSettings node={ownedNode({ node_id: "ret9f8e7d6c", name: "Shed" })} onSaved={onSaved} onGone={onGone} />);
    pending.reject(new HttpError(404, "Not Found", { detail: "Node not found" }));
    await pending.promise.catch(() => {});
    await Promise.resolve();
    expect(onGone).not.toHaveBeenCalled();
  });
});
