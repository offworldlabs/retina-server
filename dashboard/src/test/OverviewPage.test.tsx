import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HttpError } from "@retina/shared";

import appCss from "../App.css?raw";
import { api } from "../api/client";
import { CLAIM_PATH } from "../components/ClaimPath";
import type { FeedAircraft, MyClaim, OwnedNode, OwnerAnalytics } from "../types";
import OverviewPage from "../pages/user/OverviewPage";
import { ownedNode } from "./fixtures/ownedNode";
import { declarations } from "./paletteTokens";

vi.mock("../api/client", () => ({
  api: {
    nodes: vi.fn(), analytics: vi.fn(), aircraft: vi.fn(),
    myNodes: vi.fn(), myAnalytics: vi.fn(), myAircraft: vi.fn(),
    myClaims: vi.fn(), resendMyClaim: vi.fn(),
  },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: () => null,
}));

const state = vi.hoisted(() => ({ auth: { polledRadarRegistration: false } }));
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

// A private node is on no public listing, so the owner's own copy is the only
// one the page has, and it carries the node_id the page must not show.
const OWNED = ownedNode({
  node_id: "ret1a2b3c4d",
  node_ref: "nde0123456789",
  name: "nde0123456789",
  position_status: "missing_rx",
  location_private: true,
});
const REF = "nde0123456789";

// A node its owner has labelled: the list sends the label as its name too.
const LABELLED = ownedNode({
  node_id: "ret5e6f7a8b",
  node_ref: "nde5e6f7a8b9c0d",
  name: "Rooftop",
  label: "Rooftop",
  position_status: "missing_tx",
});

// Somebody else's node, on the public listing with analytics of its own.
const THEIRS_REF = "ndezzzzzzzzzzzz";
const THEIRS = {
  status: "active",
  name: "Their rooftop",
  config_hash: "feedfacecafe",
  position_status: "missing_tx",
};

const summary = (detections: number, tracks: number) => ({
  metrics: { total_detections: detections, total_tracks: tracks },
});

function renderWith(
  mine: object[],
  {
    fleet = {},
    analytics = {},
    aircraft = [],
    claims = [],
  }: { fleet?: object; analytics?: object; aircraft?: FeedAircraft[]; claims?: MyClaim[] | Error } = {},
) {
  vi.mocked(api.myNodes).mockResolvedValue(mine as OwnedNode[]);
  vi.mocked(api.myAnalytics).mockResolvedValue({ nodes: analytics } as OwnerAnalytics);
  vi.mocked(api.myAircraft).mockResolvedValue({ aircraft });
  if (claims instanceof Error) vi.mocked(api.myClaims).mockRejectedValue(claims);
  else vi.mocked(api.myClaims).mockResolvedValue(claims);
  // Answered, though the page must not ask: the whole network's listing, its
  // summaries (the public, fuzzed figures, which differ from the owner's) and
  // its aircraft.
  vi.mocked(api.nodes).mockResolvedValue({ nodes: fleet });
  vi.mocked(api.analytics).mockResolvedValue({ nodes: { [REF]: summary(7, 3), [THEIRS_REF]: summary(900, 70) } });
  vi.mocked(api.aircraft).mockResolvedValue({ aircraft: [{ hex: "ff0001" }, { hex: "ff0002" }, { hex: "ff0003" }] });
  render(
    <MemoryRouter initialEntries={["/overview"]}>
      <Routes>
        <Route path="/overview" element={<OverviewPage />} />
        <Route path="/nodes/:ref" element={<Landed kind="detail" />} />
        <Route path="/radars/:ref" element={<Landed kind="radar" />} />
      </Routes>
    </MemoryRouter>,
  );
}

function Landed({ kind }: { kind: string }) {
  const navigate = useNavigate();
  return (
    <div>
      {`${kind} ${useParams().ref}`}
      <button type="button" onClick={() => navigate(-1)}>Back</button>
    </div>
  );
}

const labels = (card: HTMLElement) => [...card.querySelectorAll(".meta-label")].map((el) => el.textContent);

// By the tile's label, since the cards below reuse some of the same words.
const stat = (label: string) =>
  [...document.querySelectorAll(".stat-label")].find((el) => el.textContent === label)!.closest(".stat-card");

async function ownedCard(): Promise<HTMLElement> {
  const names = await screen.findAllByText(REF);
  return names.map((el) => el.closest<HTMLElement>(".node-card")).find(Boolean)!;
}

const cards = () => [...document.querySelectorAll<HTMLElement>(".node-card")];
const attentionRows = () => [...document.querySelectorAll<HTMLTableRowElement>("tbody tr")];

/** The one link inside `el`, which must be named `name`. */
function onlyLink(el: HTMLElement, name: string): HTMLElement {
  expect(within(el).getAllByRole("link")).toHaveLength(1);
  return within(el).getByRole("link", { name });
}

describe("OverviewPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    state.auth = { polledRadarRegistration: false };
  });

  it("names an owned node by its node_ref", async () => {
    renderWith([OWNED]);
    expect(await screen.findAllByText(REF)).toHaveLength(2);
    expect(screen.queryByText("ret1a2b3c4d")).not.toBeInTheDocument();
  });

  it("shows a dash, not the node_id, for a node with neither a name nor a ref", async () => {
    renderWith([{ ...OWNED, node_ref: null, name: null }]);
    await screen.findByText("My Nodes");
    expect(screen.queryByText(/ret1a2b3c4d/)).not.toBeInTheDocument();
    expect(screen.getByRole("table").querySelector("td")).toHaveTextContent(/^—$/);
    expect(document.querySelector(".node-name")).toHaveTextContent(/—$/);
  });

  it("leaves every node the caller does not own out of the page", async () => {
    renderWith([OWNED], {
      fleet: { [THEIRS_REF]: THEIRS },
      analytics: { [REF]: summary(5, 2), [THEIRS_REF]: summary(900, 70) },
    });
    await screen.findByText("My Nodes");
    expect(screen.queryByText("Their rooftop")).not.toBeInTheDocument();
    expect(stat("Nodes Online")).toHaveTextContent("1 / 1");
    expect(stat("Frame Detections")).toHaveTextContent("5");
    expect(stat("Tracks")).toHaveTextContent("2");
    expect(document.querySelectorAll(".node-card")).toHaveLength(1);
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(api.nodes).not.toHaveBeenCalled();
  });

  it("reads a private node's detections from the owner's analytics, not the network's", async () => {
    renderWith([OWNED], { analytics: { [REF]: summary(1234, 56) } });
    const card = await ownedCard();
    expect(card).toHaveTextContent("Detections1,234");
    expect(card).toHaveTextContent("Tracks56");
    expect(stat("Frame Detections")).toHaveTextContent("1,234");
    expect(api.myAnalytics).toHaveBeenCalled();
    expect(api.analytics).not.toHaveBeenCalled();
  });

  it("shows zero, not the network's figure, for a node the owner's analytics has nothing on yet", async () => {
    renderWith([OWNED]);
    const card = await ownedCard();
    expect(card).toHaveTextContent("Detections0");
    expect(card).toHaveTextContent("Tracks0");
  });

  it("shows a private node's config hash, which the public listing leaves out", async () => {
    renderWith([{ ...OWNED, config_hash: "0123456789abcdef" }]);
    expect(await screen.findByText("01234567")).toBeInTheDocument();
  });

  it("counts aircraft from the owner feed, not the public one", async () => {
    renderWith([OWNED], { aircraft: [{ hex: "aa0001", lat: 0, lon: 0 }, { hex: "aa0002", lat: 0, lon: 0 }] });
    await screen.findByText("My Nodes");
    expect(stat("Live Aircraft")).toHaveTextContent("2");
    expect(api.aircraft).not.toHaveBeenCalled();
  });

  it("counts a node the owner's list has never seen as offline", async () => {
    renderWith([OWNED, { ...OWNED, node_id: "ret00000000", node_ref: "nde000000000000", status: "never_connected" }]);
    await screen.findByText("My Nodes");
    expect(stat("Nodes Online")).toHaveTextContent("1 / 2");
  });

  it("charts detections only where there is more than one node to compare", async () => {
    renderWith([OWNED]);
    await screen.findByText("My Nodes");
    expect(screen.queryByText("Detections by Node")).not.toBeInTheDocument();
  });

  it("charts detections across two or more nodes", async () => {
    renderWith([OWNED, { ...OWNED, node_id: "ret00000000", node_ref: "nde000000000000" }]);
    expect(await screen.findByText("Detections by Node")).toBeInTheDocument();
  });

  it("gives each node's frequency and location on its card", async () => {
    renderWith([{ ...OWNED, frequency: 195_000_000, rx_lat: 51.5, rx_lon: -0.12 }]);
    const card = await ownedCard();
    expect(card).toHaveTextContent("Frequency195.000 MHz");
    expect(card).toHaveTextContent("Location51.500, -0.120");
  });

  it("gives a named node's ref, the handle the map knows it by", async () => {
    renderWith([{ ...OWNED, name: "Rooftop" }]);
    const card = (await screen.findByText(REF)).closest<HTMLElement>(".node-card")!;
    expect(card).toHaveTextContent("Rooftop");
    expect(labels(card)).toContain("Ref");
    expect(card).toHaveTextContent(`Ref${REF}`);
  });

  it.each([
    ["named by it", { name: REF }],
    ["with no name at all", { name: null }],
  ])("gives no ref for a node %s, which its card is already titled with", async (_, fields) => {
    renderWith([{ ...OWNED, ...fields }]);
    expect(labels(await ownedCard())).not.toContain("Ref");
  });

  it("titles a labelled node by its label, gives its ref beneath, and names its row the same", async () => {
    renderWith([LABELLED]);
    await screen.findAllByRole("link", { name: "Rooftop" });
    const [card] = cards();
    expect(card.querySelector(".node-name")).toHaveTextContent(/Rooftop$/);
    expect(onlyLink(card, "Rooftop")).toHaveAttribute("href", "/nodes/nde5e6f7a8b9c0d");
    expect(labels(card)).toContain("Ref");
    expect(card.querySelector(".node-meta")).toHaveTextContent("Refnde5e6f7a8b9c0d");
    expect(onlyLink(attentionRows()[0], "Rooftop")).toHaveAttribute("href", "/nodes/nde5e6f7a8b9c0d");
  });

  // Each card and row is its node's link and nothing else: the stylesheet
  // stretches the link over it, so a pointer and the keyboard reach the same
  // one control.
  it("links each node's name to its page, in its card and its Needs Attention row", async () => {
    renderWith([OWNED]);
    const links = await screen.findAllByRole("link", { name: REF });
    expect(links.map((a) => a.getAttribute("href"))).toEqual([`/nodes/${REF}`, `/nodes/${REF}`]);
  });

  it("links a polled radar's name to the page that manages it", async () => {
    renderWith([{ ...OWNED, polled: { liveness: "streaming" } }]);
    const links = await screen.findAllByRole("link", { name: REF });
    expect(links.map((a) => a.getAttribute("href"))).toEqual([`/radars/${REF}`, `/radars/${REF}`]);
  });

  it("gives each node card and each Needs Attention row exactly one link, named by its node", async () => {
    renderWith([OWNED, LABELLED]);
    await screen.findAllByRole("link", { name: "Rooftop" });
    const [ownCard, labelledCard, ...extraCards] = cards();
    const [ownRow, labelledRow, ...extraRows] = attentionRows();
    expect([...extraCards, ...extraRows]).toEqual([]);
    expect(onlyLink(ownCard, REF)).toHaveAttribute("href", `/nodes/${REF}`);
    expect(onlyLink(labelledCard, "Rooftop")).toHaveAttribute("href", "/nodes/nde5e6f7a8b9c0d");
    expect(onlyLink(ownRow, REF)).toHaveAttribute("href", `/nodes/${REF}`);
    expect(onlyLink(labelledRow, "Rooftop")).toHaveAttribute("href", "/nodes/nde5e6f7a8b9c0d");
  });

  it.each([
    ["card", cards],
    ["Needs Attention row", attentionRows],
  ])("lets a node's link in its %s take focus, as a tab stop of its own", async (_, places) => {
    renderWith([OWNED]);
    await ownedCard();
    const link = onlyLink(places()[0], REF);
    expect(link).not.toHaveAttribute("tabindex");
    link.focus();
    expect(document.activeElement).toBe(link);
  });

  it.each([
    ["card", cards],
    ["Needs Attention row", attentionRows],
  ])("follows the link in a node's %s to its page", async (_, places) => {
    renderWith([OWNED]);
    await ownedCard();
    fireEvent.click(onlyLink(places()[0], REF));
    expect(await screen.findByText(`detail ${REF}`)).toBeInTheDocument();
  });

  it("follows the link in a polled radar's card to the page that manages it", async () => {
    renderWith([{ ...OWNED, polled: { liveness: "streaming" } }]);
    fireEvent.click(onlyLink(await ownedCard(), REF));
    expect(await screen.findByText(`radar ${REF}`)).toBeInTheDocument();
  });

  // The row and the card hold no handler of their own: a click that reaches
  // them other than through the link goes nowhere. The stylesheet's overlay is
  // what sends such a click to the link in a browser.
  it("navigates nowhere on a click on a card or row outside its link", async () => {
    renderWith([OWNED]);
    const card = await ownedCard();
    const [row] = attentionRows();
    const targets = [
      card,
      card.querySelector<HTMLElement>(".node-meta")!,
      within(card).getByText("Private"),
      row,
      row.cells[1],
      within(row).getByText("No receiver position"),
    ];
    for (const target of targets) fireEvent.click(target);
    expect(screen.getByRole("heading", { name: "My Nodes" })).toBeInTheDocument();
    expect(screen.queryByText(/^(detail|radar) /)).not.toBeInTheDocument();
  });

  // React attaches handlers at the root, so an onclick attribute would be
  // missing even with one; the click test above is what proves there is none.
  it("puts no tabindex, role, click handler or inline style on a card or row", async () => {
    renderWith([OWNED, LABELLED]);
    await screen.findAllByRole("link", { name: "Rooftop" });
    const hosts = [...cards(), ...document.querySelectorAll<HTMLElement>("tr")];
    expect(hosts).toHaveLength(5);
    for (const el of hosts) {
      expect(el).not.toHaveAttribute("tabindex");
      expect(el).not.toHaveAttribute("role");
      expect(el).not.toHaveAttribute("onclick");
      expect(el).not.toHaveAttribute("style");
    }
  });

  it("names a node with no page in plain text, with no link to follow", async () => {
    renderWith([{ ...OWNED, node_ref: null, name: "Shed" }]);
    expect(await screen.findAllByText("Shed")).toHaveLength(2);
    expect(screen.queryAllByRole("link")).toEqual([]);
    expect(cards()[0]).toHaveClass("unlinked");
    expect(attentionRows()[0]).not.toHaveClass("linked");
  });

  // Navigating twice would leave Back on the node's page.
  it.each([
    ["Needs Attention row", 0],
    ["card", 1],
  ])("follows a name's link once from the %s, so Back returns to My Nodes", async (_, index) => {
    renderWith([OWNED]);
    const links = await screen.findAllByRole("link", { name: REF });
    fireEvent.click(links[index]);
    await screen.findByText(`detail ${REF}`);
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("heading", { name: "My Nodes" })).toBeInTheDocument();
  });

  it("marks a private node on its card", async () => {
    renderWith([OWNED]);
    expect((await ownedCard()).querySelector(".node-name")).toHaveTextContent("Private");
  });

  it("names the claim path in the page header", async () => {
    renderWith([OWNED]);
    const header = (await screen.findByRole("heading", { name: "My Nodes" })).closest(".page-header");
    expect(header).toHaveTextContent(CLAIM_PATH);
  });

  it("tells an owner with no nodes that none is on their account yet, and how one joins", async () => {
    renderWith([]);
    const card = (await screen.findByRole("heading", { name: "Nodes" })).closest<HTMLElement>(".card")!;
    expect(card).toHaveTextContent(`No nodes on your account yet. ${CLAIM_PATH}`);
    expect(card.querySelector(".node-grid")).toBeNull();
  });

  it("offers to add a stock blah2 radar where registration is open", async () => {
    state.auth = { polledRadarRegistration: true };
    renderWith([]);
    expect(await screen.findByRole("link", { name: "Add a stock blah2 radar" })).toHaveAttribute("href", "/radars/new");
  });

  it("offers nothing of the kind where it is not", async () => {
    renderWith([]);
    await screen.findByText("No nodes on your account yet.");
    expect(screen.queryByRole("link", { name: "Add a stock blah2 radar" })).toBeNull();
  });
});

const inMinutes = (minutes: number) => Date.now() / 1000 + minutes * 60;

const LIVE_CLAIM: MyClaim = { node_ref: "nde1111111111aa", email: "ada@example.com", pending: true, expires_at: inMinutes(10) };
const LAPSED_CLAIM: MyClaim = { node_ref: "nde2222222222bb", email: "old@example.com", pending: true, expires_at: inMinutes(-5) };
const NONE_OUT: MyClaim = { node_ref: "nde3333333333cc", email: "new@example.com", pending: false, expires_at: null };

const waiting = async () =>
  (await screen.findByRole("heading", { name: "Waiting for you" })).closest<HTMLElement>(".card")!;
const claimRow = (card: HTMLElement, nodeRef: string) =>
  within(card).getByText(nodeRef).closest<HTMLElement>("li")!;

describe("the claims waiting on the owner's address", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    state.auth = { polledRadarRegistration: false };
  });

  it("lists each node with the address it was given and whether its link is live", async () => {
    renderWith([OWNED], { claims: [LIVE_CLAIM, LAPSED_CLAIM, NONE_OUT] });
    const card = await waiting();

    const live = claimRow(card, LIVE_CLAIM.node_ref);
    expect(live).toHaveTextContent("Given ada@example.com");
    expect(live).toHaveTextContent(/Link is live until .*\d{2}/);
    const lapsed = claimRow(card, LAPSED_CLAIM.node_ref);
    expect(lapsed).toHaveTextContent("Given old@example.com");
    expect(lapsed).toHaveTextContent("Link has expired");
    expect(lapsed).not.toHaveTextContent(/live/);
    // No link out at all reads as expired too.
    expect(claimRow(card, NONE_OUT.node_ref)).toHaveTextContent("Link has expired");
    expect(within(card).getAllByRole("button", { name: "Send the link again" })).toHaveLength(3);
    expect(api.myClaims).toHaveBeenCalledTimes(1);
  });

  it("shows nothing for an account with no claims waiting", async () => {
    renderWith([OWNED], { claims: [] });
    await screen.findByRole("heading", { name: "My Nodes" });
    await screen.findAllByText(REF);
    expect(screen.queryByText("Waiting for you")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send the link again" })).not.toBeInTheDocument();
  });

  it("carries on with the rest of the page when the claims cannot be read", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderWith([OWNED], { claims: new HttpError(500, "", null) });

    expect(await screen.findAllByText(REF)).toHaveLength(2);
    expect(api.myClaims).toHaveBeenCalled();
    expect(stat("Nodes Online")).toHaveTextContent("1 / 1");
    expect(screen.queryByText("Waiting for you")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/Could not (load|refresh)/)).not.toBeInTheDocument();
  });

  it("sends the mail again for the row's node, and says so on that row alone", async () => {
    renderWith([OWNED], { claims: [LIVE_CLAIM, LAPSED_CLAIM] });
    vi.mocked(api.resendMyClaim).mockResolvedValue({ status: "sent" });
    const card = await waiting();

    fireEvent.click(within(claimRow(card, LAPSED_CLAIM.node_ref)).getByRole("button", { name: "Send the link again" }));

    await waitFor(() =>
      expect(within(claimRow(card, LAPSED_CLAIM.node_ref)).getByRole("status")).toHaveTextContent(
        "Sent. The link lasts fifteen minutes.",
      ),
    );
    expect(api.resendMyClaim).toHaveBeenCalledTimes(1);
    expect(api.resendMyClaim).toHaveBeenCalledWith(LAPSED_CLAIM.node_ref);
    expect(within(claimRow(card, LIVE_CLAIM.node_ref)).queryByRole("status")).toBeNull();
  });

  it("holds the row's button while its mail is being sent", async () => {
    renderWith([OWNED], { claims: [LIVE_CLAIM] });
    let answer: (v: { status: "sent" }) => void = () => {};
    vi.mocked(api.resendMyClaim).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const card = await waiting();

    fireEvent.click(within(card).getByRole("button", { name: "Send the link again" }));

    const button = within(card).getByRole("button", { name: "Sending…" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(api.resendMyClaim).toHaveBeenCalledTimes(1);
    await act(async () => answer({ status: "sent" }));
  });

  it("reads the list again after a send, so the new link's time shows", async () => {
    renderWith([OWNED], { claims: [LAPSED_CLAIM] });
    vi.mocked(api.resendMyClaim).mockResolvedValue({ status: "sent" });
    const card = await waiting();
    expect(card).toHaveTextContent("Link has expired");
    vi.mocked(api.myClaims).mockResolvedValue([{ ...LAPSED_CLAIM, expires_at: inMinutes(15) }]);

    fireEvent.click(within(card).getByRole("button", { name: "Send the link again" }));

    await waitFor(() => expect(api.myClaims).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(claimRow(card, LAPSED_CLAIM.node_ref)).toHaveTextContent(/Link is live until .*\d{2}/));
    // The nodes beside it stay on screen throughout.
    expect(screen.getAllByText(REF).length).toBeGreaterThan(0);
  });

  it("says why a send failed on its row, and lets the owner try again", async () => {
    renderWith([OWNED], { claims: [LIVE_CLAIM] });
    vi.mocked(api.resendMyClaim).mockRejectedValue(new HttpError(429, "", null));
    const card = await waiting();

    fireEvent.click(within(card).getByRole("button", { name: "Send the link again" }));

    const row = claimRow(card, LIVE_CLAIM.node_ref);
    await waitFor(() =>
      expect(within(row).getByRole("alert")).toHaveTextContent("Too many links have been asked for. Try again later."),
    );
    expect(within(row).getByRole("button", { name: "Send the link again" })).toBeEnabled();
    expect(api.myClaims).toHaveBeenCalledTimes(1);
  });

  it("keeps the shared words for any other failure of a send", async () => {
    renderWith([OWNED], { claims: [LIVE_CLAIM] });
    vi.mocked(api.resendMyClaim).mockRejectedValue(new HttpError(503, "", null));
    const card = await waiting();

    fireEvent.click(within(card).getByRole("button", { name: "Send the link again" }));

    await waitFor(() =>
      expect(within(claimRow(card, LIVE_CLAIM.node_ref)).getByRole("alert")).toHaveTextContent(
        "This service is unavailable right now. Try again later.",
      ),
    );
  });

  it("says the claim is no longer waiting when the send meets a 404, and reads the list again", async () => {
    renderWith([OWNED], { claims: [LIVE_CLAIM, LAPSED_CLAIM] });
    vi.mocked(api.resendMyClaim).mockRejectedValue(new HttpError(404, "", { detail: "Not Found" }));
    const card = await waiting();
    vi.mocked(api.myClaims).mockResolvedValue([LIVE_CLAIM]);

    fireEvent.click(within(claimRow(card, LAPSED_CLAIM.node_ref)).getByRole("button", { name: "Send the link again" }));

    await waitFor(() => expect(api.myClaims).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(card).getAllByRole("listitem")).toHaveLength(1));
    expect(within(card).getByRole("status")).toHaveTextContent(`Node ${LAPSED_CLAIM.node_ref} is no longer waiting for you.`);
    expect(card).not.toHaveTextContent(/HTTP 404|refused the request/);
    expect(screen.queryByRole("alert")).toBeNull();
    // The row that is still waiting is untouched.
    expect(claimRow(card, LIVE_CLAIM.node_ref)).toHaveTextContent("Given ada@example.com");
  });

  it("keeps saying so when the 404 was for the only claim, and the list is empty after", async () => {
    renderWith([OWNED], { claims: [LAPSED_CLAIM] });
    vi.mocked(api.resendMyClaim).mockRejectedValue(new HttpError(404, "", null));
    const card = await waiting();
    vi.mocked(api.myClaims).mockResolvedValue([]);

    fireEvent.click(within(card).getByRole("button", { name: "Send the link again" }));

    await waitFor(() => expect(api.myClaims).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(card).queryAllByRole("listitem")).toHaveLength(0));
    expect(within(card).getByRole("status")).toHaveTextContent("is no longer waiting for you.");
  });
});

describe("the stylesheet behind a card's or row's link", () => {
  const css = appCss.replace(/\/\*[\s\S]*?\*\//g, "");

  it("lays the link over the whole card", () => {
    expect(declarations(css, "\n.node-card.linked {").position).toBe("relative");
    expect(declarations(css, "\n.node-card.linked .node-link::after {").position).toBe("absolute");
  });

  // Safari skipped a positioned row as a containing block (WebKit bug 240961):
  // every row's overlay would cover the table and each click go to the last.
  it("hangs a row's overlay off its first cell, never the row itself", () => {
    expect(declarations(css, "\ntr.linked > td:first-child {").position).toBe("relative");
    expect(css).not.toMatch(/(^|\n)tr\.linked\s*\{[^}]*position/);
    expect(declarations(css, "\n.linked-rows .table-wrapper {")["container-type"]).toBe("inline-size");
    expect(declarations(css, "\ntr.linked .node-link::after {").width).toBe("100cqi");
  });

  it("rings the whole card or row while its link has keyboard focus", () => {
    expect(declarations(css, "\n.linked .node-link:focus-visible::after {").outline).toBe("2px solid var(--accent)");
    expect(declarations(css, "\n.linked .node-link:focus-visible {").outline).toBe("none");
  });

  it("gives a card with no page neither the pointer nor the hover of one", () => {
    expect(declarations(css, "\n.node-card.unlinked {").cursor).toBe("auto");
    expect(declarations(css, "\n.node-card.unlinked:hover {")["border-color"]).toBe("var(--border)");
  });
});
