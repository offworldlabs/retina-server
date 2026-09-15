import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DataTable } from "../components/DataTable";

const rows = (
  <>
    <tr>
      <td>ret0a1b</td>
      <td>Online</td>
    </tr>
    <tr>
      <td>ret0c2d</td>
      <td>Offline</td>
    </tr>
  </>
);

describe("DataTable", () => {
  it("wraps a table with the headers in order and the rows underneath", () => {
    const { container } = render(
      <DataTable headers={["Node", "Status"]} count={2}>
        {rows}
      </DataTable>,
    );
    expect(container.firstElementChild).toHaveClass("table-wrapper");
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toEqual(["Node", "Status"]);
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(screen.getByText("ret0c2d")).toBeInTheDocument();
  });

  it("shows the empty text across every column when there are no rows", () => {
    render(
      <DataTable headers={["Node", "Status", "Uptime"]} count={0} empty="No nodes found">
        {null}
      </DataTable>,
    );
    const cell = screen.getByText("No nodes found");
    expect(cell).toHaveAttribute("colspan", "3");
    expect(cell).toHaveClass("table-note");
  });

  it("shows nothing in the body when there are no rows and no empty text", () => {
    render(
      <DataTable headers={["Node"]} count={0}>
        {[]}
      </DataTable>,
    );
    expect(screen.getAllByRole("row")).toHaveLength(1);
  });

  it("shows a busy row instead of the rows while loading", () => {
    render(
      <DataTable headers={["Node", "Status"]} count={2} loading>
        {rows}
      </DataTable>,
    );
    expect(screen.getByText("Loading…")).toHaveAttribute("colspan", "2");
    expect(screen.queryByText("ret0a1b")).toBeNull();
  });

  it("accepts markup in a header", () => {
    render(
      <DataTable headers={["Node", <abbr key="snr" title="Signal to noise">SNR</abbr>]} count={0}>
        {null}
      </DataTable>,
    );
    expect(screen.getByTitle("Signal to noise")).toBeInTheDocument();
  });
});
