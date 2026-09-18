import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NodePicker } from "../../pages/user/dataExplorer/NodePicker";
import type { RegistryNode } from "../../pages/user/dataExplorer/nodes";
import { defaultFilters, type ExplorerFilters } from "../../pages/user/dataExplorer/urlState";

const TODAY = "2026-09-17";

function node(id: string, name: string, synthetic = false): RegistryNode {
  return { id, name, status: "online", synthetic, lat: 51.5, lon: -0.1, uncertaintyKm: null };
}

const REGISTRY = new Map<string, RegistryNode>([
  ["ret-london", node("ret-london", "London")],
  ["ret-oxford", node("ret-oxford", "Oxford")],
  ["synth-1", node("synth-1", "Sim one", true)],
]);

const COUNTS: Record<string, number> = { "ret-london": 42, "ret-oxford": 7, "synth-1": 900 };

function open(over: Partial<ExplorerFilters> = {}, discovered = new Set<string>()) {
  const onChange = vi.fn();
  render(
    <NodePicker
      filters={{ ...defaultFilters(TODAY), ...over }}
      nodes={REGISTRY}
      discovered={discovered}
      fileCountFor={(id) => COUNTS[id] ?? 0}
      onChange={onChange}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /nodes$/ }));
  return { onChange };
}

/** The selection the last onChange call carried. */
function selectionFrom(onChange: ReturnType<typeof vi.fn>) {
  const { calls } = onChange.mock;
  return (calls[calls.length - 1][0] as ExplorerFilters).nodeSel;
}

describe("NodePicker", () => {
  it("offers every node in the registry", () => {
    open();
    expect(screen.getByRole("checkbox", { name: /London/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Sim one/ })).toBeInTheDocument();
  });

  it("offers a node seen only in an archive key", () => {
    open({}, new Set(["ret-ghost"]));
    expect(screen.getByRole("checkbox", { name: /ret-ghost/ })).toBeInTheDocument();
  });

  it("shows how many files each node has", () => {
    open();
    const row = screen.getByRole("checkbox", { name: /London/ }).closest("label")!;
    expect(within(row).getByText("42")).toBeInTheDocument();
  });

  it("selects everything as null, so nodes discovered later are included too", () => {
    const { onChange } = open({ nodeSel: new Set(["ret-london"]) });
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    expect(selectionFrom(onChange)).toBeNull();
  });

  it("clears to an empty set, which is not the same as all", () => {
    const { onChange } = open();
    fireEvent.click(screen.getByRole("button", { name: "None" }));
    const selection = selectionFrom(onChange);
    expect(selection).toBeInstanceOf(Set);
    expect(selection!.size).toBe(0);
  });

  it("selects exactly the non-synthetic nodes for Real", () => {
    const { onChange } = open();
    fireEvent.click(screen.getByRole("button", { name: "Real only" }));
    expect([...selectionFrom(onChange)!].sort()).toEqual(["ret-london", "ret-oxford"]);
  });

  it("counts a key-discovered node's prefix as synthetic for the Real preset", () => {
    // It is in no registry entry, so there is no server flag to read and the
    // prefix is the only thing that can classify it.
    const { onChange } = open({}, new Set(["e2e-ghost"]));
    fireEvent.click(screen.getByRole("button", { name: "Real only" }));
    expect([...selectionFrom(onChange)!]).not.toContain("e2e-ghost");
  });

  it("keeps a key-discovered node with an ordinary id in the Real preset", () => {
    const { onChange } = open({}, new Set(["ret-ghost"]));
    fireEvent.click(screen.getByRole("button", { name: "Real only" }));
    expect([...selectionFrom(onChange)!]).toContain("ret-ghost");
  });

  it("toggles one node without disturbing the others", () => {
    const { onChange } = open({ nodeSel: new Set(["ret-london", "ret-oxford"]) });
    fireEvent.click(screen.getByRole("checkbox", { name: /Oxford/ }));
    expect([...selectionFrom(onChange)!]).toEqual(["ret-london"]);
  });

  it("turns a tick on an all-nodes view into an explicit set", () => {
    const { onChange } = open({ nodeSel: null });
    fireEvent.click(screen.getByRole("checkbox", { name: /Oxford/ }));
    const selection = selectionFrom(onChange)!;
    expect(selection.has("ret-oxford")).toBe(false);
    expect(selection.has("ret-london")).toBe(true);
  });

  it("narrows the list by search without changing the selection", () => {
    const { onChange } = open();
    fireEvent.change(screen.getByLabelText(/search node/i), { target: { value: "oxf" } });
    expect(screen.queryByRole("checkbox", { name: /London/ })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Oxford/ })).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });
});
