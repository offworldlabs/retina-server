import L, { type LatLng, type Map as LeafletMap } from "leaflet";

/**
 * Keeps a view that a fit framed on an owner's true receiver positions out of
 * the URL hash.
 *
 * A signed-in owner's own nodes are drawn where they really are
 * (nodeListing.ts), and with no aircraft to frame, FitBounds frames the nodes.
 * The hash mirrors the map's centre to four decimals (useUrlHashState.ts), so a
 * link copied from that view would carry the owner's true receiver position,
 * where it used to carry the published, displaced one.
 *
 * So a fit that frames any true position holds the view back from the hash,
 * and the hold outlasts the fit. A panel opening or the list collapsing
 * resizes the map, and Leaflet answers with a moveend at the same centre
 * (invalidateSize keeps it); a popup's auto-pan nudges it. Either would write
 * it all the same. The hold ends when the user moves the map's centre
 * themselves: where they take it from there is theirs to share. A zoom about
 * the centre, as the +/- buttons and keys make, keeps it, because the centre
 * it would write is still the fit's. A later fit on aircraft or on published
 * positions ends it too, and so does Follow centring the map on an aircraft,
 * because the view each lands on holds nothing private.
 *
 * The user moving the map means a move one of Leaflet's navigation handlers
 * starts, not the gesture alone. A gesture can start nothing: a zoom key at the
 * zoom limit, an arrow key while the fit is still panning (Leaflet ignores it),
 * two fingers that never move. Leaflet ends some of those with a moveend at the
 * view it already had, and ending the hold on the gesture would let that
 * moveend write the fit's centre. So a gesture only opens a window, as long as
 * the handler takes to start its move, and a movestart inside it makes that
 * move the user's. Leaflet fires movestart for every move it makes and for none
 * of those moveends. The user's move ends the hold once it takes the centre a
 * pixel or more from where it started. Leaflet fires move before each moveend,
 * so HashSync's moveend finds the hold already ended. A zoom about the centre
 * lands within half a pixel of it, as Leaflet rounds the view to whole pixels.
 * A drag is already a move by the time Leaflet says it started, so dragstart
 * ends the hold at once.
 *
 * A move starts from the centre the map had when the gesture came, or when
 * the move began. The two differ only while the fit is still panning: Leaflet
 * zooms a key, a button or a double-click about the first, and the wheel,
 * which stops the pan before it zooms, about the second.
 */

interface Hold {
  /** The view on screen came from a fit framed on a true position. */
  held: boolean;
  /** Gestures whose window is open: a movestart while any is the user's. */
  gestures: Set<object>;
  /** Where the user's move under way started, until its moveend. */
  from: LatLng[] | null;
}

// Held weakly, and off Leaflet's own objects, as worldWrap.ts keeps its state.
const holds = new WeakMap<LeafletMap, Hold>();

function holdOf(map: LeafletMap): Hold {
  let hold = holds.get(map);
  if (!hold) {
    hold = { held: false, gestures: new Set(), from: null };
    holds.set(map, hold);
  }
  return hold;
}

/** Set by FitBounds before each fit: whether the view it moves to frames a
 *  true receiver position. Before the move rather than after, because a fit
 *  too far to animate lands at once and its moveend has to find the hold. The
 *  open windows, and a move of the user's under way, close with it, so the
 *  fit's own move does not end the hold it has just set. */
export function holdViewForFit(map: LeafletMap, framesTruePosition: boolean): void {
  const hold = holdOf(map);
  hold.held = framesTruePosition;
  hold.gestures.clear();
  hold.from = null;
}

/** Ends the hold for a move that replaces the view with one that holds nothing
 *  private: Follow centring the map on an aircraft. */
export function releaseView(map: LeafletMap): void {
  holdOf(map).held = false;
}

/** Whether the map's current view has to stay out of the URL hash. */
export function viewHeldFromHash(map: LeafletMap): boolean {
  return holds.get(map)?.held ?? false;
}

/** Leaflet's Keyboard handler: the keyCodes it pans and zooms by. */
interface KeyboardTable {
  keyCodes?: Record<string, number[]>;
}

/** Leaflet's BoxZoom handler, which knows whether a box is being drawn. */
interface BoxZoomState {
  moved?: () => boolean;
}

/**
 * Ends the hold on the user's own navigation, by every handler Leaflet's map
 * has by default and its zoom buttons. Returns the uninstaller.
 */
export function releaseViewOnNavigation(map: LeafletMap): () => void {
  const hold = holdOf(map);
  const container = map.getContainer();
  const doc = container.ownerDocument;

  // The centre at the latest gesture made on a held view, and at the latest
  // move or moveend.
  let gestureAt: LatLng | null = null;
  let movedTo: LatLng | null = null;

  /** Opens a gesture's window and hands `until` the function that closes it. */
  const open = (until: (close: () => void) => void) => {
    const gesture = {};
    hold.gestures.add(gesture);
    if (hold.held) gestureAt = map.getCenter();
    until(() => { hold.gestures.delete(gesture); });
  };
  // A handler starts its move in its own turn, or in the next frame for an
  // animated zoom (Map._tryAnimatedZoom). Queued on Leaflet's own frame
  // scheduler, so this frame is that one or a later one, and the timeout runs
  // once every callback of the frame has.
  const nextFrame = (close: () => void) => {
    L.Util.requestAnimFrame(() => { setTimeout(close); });
  };

  // Not getCenter here: a zoom that does not animate resets the map's pane
  // before its movestart, and the centre reads stale until the zoom lands.
  const onMovestart = () => {
    if (hold.held && hold.gestures.size > 0 && !hold.from) {
      hold.from = [gestureAt, movedTo].filter((at): at is LatLng => at !== null);
    }
  };
  // In pixels at the zoom the map is at, which is where Leaflet rounds.
  const onMove = () => {
    const centre = map.getCenter();
    if (hold.from?.every((at) => map.project(at).distanceTo(map.project(centre)) >= 1)) {
      hold.held = false;
      hold.from = null;
    }
    movedTo = centre;
  };
  const onMoveend = () => {
    hold.from = null;
    movedTo = map.getCenter();
  };
  const onDragstart = () => { hold.held = false; };

  // Captured on the document, ahead of the handlers Leaflet attaches: the
  // Keyboard's listener is on the document, DoubleClickZoom answers the map's
  // own dblclick, BoxZoom's mouseup is on the document.
  const keyCodes = new Set(Object.values((map.keyboard as unknown as KeyboardTable).keyCodes ?? {}).flat());
  const onKeydown = (e: KeyboardEvent) => {
    // Map.Keyboard acts only while the map itself has focus, and only on its
    // own keys. Not Tab or Enter: those focus or open a marker, and the pan
    // that brings it into view is not the user moving the map.
    if (e.target === container && keyCodes.has(e.keyCode)) open(nextFrame);
  };
  const onDblclick = (e: MouseEvent) => {
    if (container.contains(e.target as Node)) open(nextFrame);
  };
  const onMouseup = () => {
    // A box zoom ends on this mouseup, with a fit to the box.
    if ((map.boxZoom as unknown as BoxZoomState).moved?.()) open(nextFrame);
  };
  // On the container, where Map.ScrollWheelZoom listens: a popup stops the
  // wheel before it gets there, and then the map does not zoom either. The
  // zoom waits out the debounce from the first wheel of a burst, so the window
  // waits it out from this one.
  const onWheel = () => {
    open((close) => { setTimeout(() => nextFrame(close), map.options.wheelDebounceTime ?? 40); });
  };
  // Map.TouchZoom starts its move on the first touchmove of a two-finger touch,
  // whenever that comes, so the window stays open until a finger lifts. One
  // finger is a drag, which dragstart answers.
  let pinch: (() => void) | null = null;
  const onTouchstart = (e: TouchEvent) => {
    if (e.touches.length === 2 && !pinch) open((close) => { pinch = close; });
  };
  const onTouchend = (e: TouchEvent) => {
    if (pinch && e.touches.length < 2) {
      pinch();
      pinch = null;
    }
  };
  // The +/- buttons stop their own click, so it is caught on the way down.
  const zoomButtons = map.zoomControl?.getContainer();
  const onZoomButton = () => open(nextFrame);

  const mapEvents = { movestart: onMovestart, move: onMove, moveend: onMoveend, dragstart: onDragstart };
  map.on(mapEvents);
  doc.addEventListener("keydown", onKeydown, true);
  doc.addEventListener("dblclick", onDblclick, true);
  doc.addEventListener("mouseup", onMouseup, true);
  doc.addEventListener("touchend", onTouchend, true);
  doc.addEventListener("touchcancel", onTouchend, true);
  container.addEventListener("wheel", onWheel, { passive: true });
  container.addEventListener("touchstart", onTouchstart, { passive: true });
  zoomButtons?.addEventListener("click", onZoomButton, true);
  return () => {
    map.off(mapEvents);
    doc.removeEventListener("keydown", onKeydown, true);
    doc.removeEventListener("dblclick", onDblclick, true);
    doc.removeEventListener("mouseup", onMouseup, true);
    doc.removeEventListener("touchend", onTouchend, true);
    doc.removeEventListener("touchcancel", onTouchend, true);
    container.removeEventListener("wheel", onWheel);
    container.removeEventListener("touchstart", onTouchstart);
    zoomButtons?.removeEventListener("click", onZoomButton, true);
    pinch?.();
    pinch = null;
  };
}
