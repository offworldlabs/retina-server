import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import appCss from "../App.css?raw";
import { DataTable, useColumnSpan, type ColumnHint } from "../components/DataTable";
import tableCss from "../components/dataTable.css?raw";
import { stubViewport } from "./matchMedia";
import { declarations } from "./paletteTokens";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const css = tableCss.replace(/\/\*[\s\S]*?\*\//g, "");
const app = appCss.replace(/\/\*[\s\S]*?\*\//g, "");

/** The body of the @media block at `query`. */
function media(query: string): string {
  const open = css.indexOf("{", css.indexOf(`@media ${query} {`));
  expect(open, query).toBeGreaterThan(0);
  for (let i = open, depth = 0; i < css.length; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}" && --depth === 0) return css.slice(open + 1, i);
  }
  return "";
}

/** One attribute of every body cell, row by row, a card's More left out. */
function stamped(container: HTMLElement, attribute = "data-label") {
  return Array.from(container.querySelectorAll("tbody > tr"), (tr) =>
    Array.from(tr.querySelectorAll(":scope > td:not([data-card-more])"), (td) => td.getAttribute(attribute)),
  );
}

/** Writes the span a full-width row has at the current width. */
function Span({ columns, hints }: { columns: number; hints?: readonly (ColumnHint | undefined)[] }) {
  return <output>{useColumnSpan(columns, hints)}</output>;
}

/** A row its own component renders, as the contribution page's are. */
function NodeRow({ id }: { id: string }) {
  return (
    <tr>
      <td>{id}</td>
      <td>Online</td>
      <td>3</td>
    </tr>
  );
}

/** A row that adds its last cell by itself, without the table rendering. */
function GrowingRow() {
  const [grown, setGrown] = useState(false);
  return (
    <tr>
      <td>ret0a1b</td>
      <td>
        <button onClick={() => setGrown(true)}>Grow</button>
      </td>
      {grown && <td>late</td>}
    </tr>
  );
}

describe("a DataTable's cells", () => {
  it("carry their column's header text, from a string or an element, and keep a label the caller set", () => {
    stubViewport(390);
    const { container } = render(
      <DataTable headers={["Node", <abbr key="snr" title="Signal to noise">SNR</abbr>, ""]} count={2}>
        <tr>
          <td>ret0a1b</td>
          <td data-label="Signal">12 dB</td>
          <td>
            <button>Retry</button>
          </td>
        </tr>
        <tr>
          <td>ret0c2d</td>
          <td>9 dB</td>
          <td />
        </tr>
      </DataTable>,
    );
    expect(stamped(container)).toEqual([
      ["Node", "Signal", null],
      ["Node", "SNR", null],
    ]);
  });

  it("reach rows a component or a fragment renders, and leave a spanning cell unlabelled", () => {
    const headers = ["Node", "Status", "Tracks"];
    const { container, rerender } = render(
      <DataTable headers={headers} count={3}>
        <NodeRow id="ret0a1b" />
        <>
          <tr className="rank-gap">
            <td colSpan={3}>4 more</td>
          </tr>
          <tr>
            <td colSpan={2}>Not ranked yet</td>
            <td>0</td>
          </tr>
        </>
      </DataTable>,
    );
    expect(stamped(container)).toEqual([["Node", "Status", "Tracks"], [null], [null, "Tracks"]]);

    rerender(
      <DataTable headers={headers} count={0} empty="No nodes">
        {null}
      </DataTable>,
    );
    expect(stamped(container)).toEqual([[null]]);
  });

  it("are labelled when rows arrive with a later render, or a row adds a cell by itself", async () => {
    const rows = (ids: string[]) =>
      ids.map((id) => (
        <tr key={id}>
          <td>{id}</td>
          <td>Online</td>
        </tr>
      ));
    const { container, rerender } = render(
      <DataTable headers={["Node", "Status"]} count={1}>
        {rows(["ret0a1b"])}
      </DataTable>,
    );
    rerender(
      <DataTable headers={["Node", "Status"]} count={2}>
        {rows(["ret0a1b", "ret0c2d"])}
      </DataTable>,
    );
    expect(stamped(container)).toEqual([
      ["Node", "Status"],
      ["Node", "Status"],
    ]);

    const growing = render(
      <DataTable headers={["Node", "Action", "Note"]} count={1}>
        <GrowingRow />
      </DataTable>,
    );
    fireEvent.click(within(growing.container).getByRole("button", { name: "Grow" }));
    // The table did not render: what labels the new cell is its watch on the body.
    await act(async () => {});
    expect(stamped(growing.container)).toEqual([["Node", "Action", "Note"]]);
  });

  it("carry each column's hint, as its header does, and a .cell-primary cell heads its card where no column does", () => {
    const hints: ColumnHint[] = ["title", "primary", "secondary", "hidden"];
    const { container } = render(
      <DataTable headers={["Node", "Status", "Tracks", "Owner"]} count={1} hints={hints}>
        <tr>
          <td>ret0a1b</td>
          <td>Online</td>
          <td>3</td>
          <td>ada@example.com</td>
        </tr>
      </DataTable>,
    );
    expect(Array.from(container.querySelectorAll("th"), (th) => th.getAttribute("data-hint"))).toEqual(hints);
    expect(stamped(container, "data-hint")).toEqual([hints]);

    const plain = render(
      <DataTable headers={["Status", "User"]} count={1}>
        <tr>
          <td>Active</td>
          <td className="cell-primary">ada@example.com</td>
        </tr>
      </DataTable>,
    );
    expect(stamped(plain.container, "data-hint")).toEqual([[null, "title"]]);
  });
});

describe("a DataTable on a tablet", () => {
  it("leaves out the hidden columns, the busy row spanning only those left", () => {
    stubViewport(820);
    render(
      <DataTable headers={["Node", "Status", "Owner"]} count={0} hints={[undefined, undefined, "hidden"]} loading>
        {null}
      </DataTable>,
    );
    expect(screen.getByText("Loading…")).toHaveAttribute("colspan", "2");
    expect(declarations(media("(max-width: 1023.98px)"), '.table-wrapper > table > thead > tr > th[data-hint="hidden"]')).toEqual(
      { display: "none" },
    );
  });
});

describe("useColumnSpan", () => {
  const hints: (ColumnHint | undefined)[] = [undefined, "secondary", undefined, "hidden"];

  it("counts the columns a full-width row spans: those not hidden below 1024px, all from there up", () => {
    const viewport = stubViewport(1440);
    render(<Span columns={4} hints={hints} />);
    expect(screen.getByRole("status")).toHaveTextContent("4");
    viewport.resize(820);
    expect(screen.getByRole("status")).toHaveTextContent("3");
    viewport.resize(390);
    expect(screen.getByRole("status")).toHaveTextContent("3");
    viewport.resize(1024);
    expect(screen.getByRole("status")).toHaveTextContent("4");
  });

  it("counts every column where the table has no hints", () => {
    const viewport = stubViewport(1440);
    render(<Span columns={4} />);
    for (const width of [1440, 820, 390]) {
      viewport.resize(width);
      expect(screen.getByRole("status")).toHaveTextContent("4");
    }
  });
});

describe("a DataTable's cards on a phone", () => {
  const hints: ColumnHint[] = [undefined, undefined, "secondary"];

  it("draw a label in the console's text face, whatever face the cell is set in", () => {
    const phone = media("(max-width: 640px)");
    const label = declarations(phone, '.table-wrapper[data-phone="cards"] > table > tbody > tr > td[data-label]::before {');
    const stack = (value: string) => value.replace(/\s+/g, " ");
    expect(label["font-family"], "the label sets no face").toBeDefined();
    expect(stack(label["font-family"])).toBe(stack(declarations(app, "\nbody {")["font-family"]));
  });

  it("wrap a cell's value even where the page keeps its cells on one line", () => {
    const phone = media("(max-width: 640px)");
    expect(declarations(phone, '.table-wrapper[data-phone="cards"] > table > tbody > tr > td {')["white-space"]).toBe("normal");
  });

  it("hide the header row from sight only, and lay each row out as a card", () => {
    const phone = media("(max-width: 640px)");
    expect(declarations(phone, '.table-wrapper[data-phone="cards"] > table > thead {')["clip-path"]).toBe("inset(50%)");
    expect(declarations(phone, '.table-wrapper[data-phone="cards"] > table > tbody > tr {')).toMatchObject({
      display: "flex",
      "flex-direction": "column",
    });
    expect(declarations(phone, '.table-wrapper[data-phone="cards"] > table > tbody > tr > td[data-label]::before {').content).toBe(
      "attr(data-label)",
    );
  });

  it("keep their secondary lines behind a More button that is gone at 1024px", () => {
    const viewport = stubViewport(390);
    const { container } = render(
      <DataTable headers={["Node", "Status", "Tracks"]} count={1} hints={hints}>
        <tr>
          <td>ret0a1b</td>
          <td>Online</td>
          <td>3</td>
        </tr>
      </DataTable>,
    );
    const row = container.querySelector("tbody > tr") as HTMLTableRowElement;
    const more = within(row).getByRole("button", { name: "More" });
    expect(row.lastElementChild).toContainElement(more);
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(row).not.toHaveAttribute("data-card-open");
    // A closed card shows no secondary line; the button is a finger's height.
    const phone = media("(max-width: 640px)");
    expect(phone).toMatch(/tr:not\(\[data-card-open\]\) > td\[data-hint="secondary"\][^{]*\{\s*display: none;/);
    expect(declarations(phone, "td[data-card-more] > button {")["min-height"]).toBe("44px");

    fireEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    expect(row).toHaveAttribute("data-card-open");
    fireEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(row).not.toHaveAttribute("data-card-open");

    fireEvent.click(more);
    viewport.resize(1024);
    expect(within(row).queryByRole("button")).toBeNull();
    expect(row.cells).toHaveLength(3);
    expect(row).not.toHaveAttribute("data-card-open");
  });

  it("keep one More, last in each card, while rows are added, removed and reordered", async () => {
    stubViewport(390);
    const errors = vi.spyOn(console, "error");
    const table = (ids: string[]) => (
      <DataTable headers={["Node", "Status", "Tracks"]} count={ids.length} hints={hints}>
        {ids.map((id) => (
          <tr key={id} data-node={id}>
            <td>{id}</td>
            <td>Online</td>
            <td>3</td>
          </tr>
        ))}
      </DataTable>
    );
    const { container, rerender } = render(table(["a", "b", "c"]));
    const row = (id: string) => container.querySelector(`tr[data-node="${id}"]`) as HTMLElement;
    fireEvent.click(within(row("b")).getByRole("button", { name: "More" }));

    rerender(table(["c", "b", "d"]));
    const rows = Array.from(container.querySelectorAll("tbody > tr"));
    expect(rows.map((r) => r.getAttribute("data-node"))).toEqual(["c", "b", "d"]);
    for (const r of rows) {
      expect(r.querySelectorAll("[data-card-more]")).toHaveLength(1);
      expect(r.lastElementChild).toHaveAttribute("data-card-more");
    }
    expect(stamped(container)).toEqual(rows.map(() => ["Node", "Status", "Tracks"]));
    expect(row("b")).toHaveAttribute("data-card-open");
    expect(within(row("b")).getByRole("button")).toHaveAttribute("aria-expanded", "true");

    // A cell React appends lands after the More, which moves back to the end.
    const growing = render(
      <DataTable headers={["Node", "Action", "Note"]} count={1} hints={["title", "secondary"]}>
        <GrowingRow />
      </DataTable>,
    );
    const grown = growing.container.querySelector("tbody > tr") as HTMLElement;
    expect(grown.lastElementChild).toHaveAttribute("data-card-more");
    fireEvent.click(within(grown).getByRole("button", { name: "Grow" }));
    expect(grown.lastElementChild).toHaveTextContent("late");
    await act(async () => {});
    expect(grown.lastElementChild).toHaveAttribute("data-card-more");
    expect(grown.querySelectorAll("[data-card-more]")).toHaveLength(1);
    expect(stamped(growing.container, "data-hint")).toEqual([["title", "secondary", null]]);
    expect(errors).not.toHaveBeenCalled();
  });

  it("come with a Sort by select that hands the choice to the page, gone at 1024px", () => {
    const viewport = stubViewport(390);
    const onChange = vi.fn();
    const sort = {
      options: [
        { value: "detections", label: "Detections" },
        { value: "tracks", label: "Tracks" },
      ],
      value: "detections",
      onChange,
    };
    render(
      <DataTable headers={["Node"]} count={0} sort={sort}>
        {null}
      </DataTable>,
    );
    const select = screen.getByLabelText("Sort by");
    expect(select).toHaveValue("detections");
    fireEvent.change(select, { target: { value: "tracks" } });
    expect(onChange).toHaveBeenCalledWith("tracks");

    viewport.resize(1024);
    expect(screen.queryByLabelText("Sort by")).toBeNull();
  });
});

describe("a DataTable on a desktop", () => {
  it("draws the same table as before at 1440, but for data attributes", () => {
    stubViewport(1440);
    const { container } = render(
      <DataTable
        headers={["Node", "Status", "Tracks"]}
        count={2}
        hints={["title", "secondary", "hidden"]}
        sort={{ options: [{ value: "node", label: "Node" }], value: "node", onChange: () => {} }}
      >
        <tr className="selected">
          <td className="cell-primary">ret0a1b</td>
          <td>Online</td>
          <td>3</td>
        </tr>
        <tr>
          <td colSpan={2}>Not ranked yet</td>
          <td>0</td>
        </tr>
      </DataTable>,
    );
    expect(container.innerHTML.replace(/ data-[\w-]+="[^"]*"/g, "")).toBe(
      '<div class="table-wrapper"><table><thead><tr><th>Node</th><th>Status</th><th>Tracks</th></tr></thead>' +
        '<tbody><tr class="selected"><td class="cell-primary">ret0a1b</td><td>Online</td><td>3</td></tr>' +
        '<tr><td colspan="2">Not ranked yet</td><td>0</td></tr></tbody></table></div>',
    );
  });

  it("is left to App.css: every rule of the table's stylesheet sits in a max-width media block", () => {
    const top: string[] = [];
    for (let i = 0, depth = 0, start = 0; i < css.length; i++) {
      if (css[i] === "{" && depth++ === 0) top.push(css.slice(start, i).trim());
      else if (css[i] === "}" && --depth === 0) start = i + 1;
    }
    expect(top).toEqual(["@media (max-width: 1023.98px)", "@media (max-width: 640px)"]);
  });
});
