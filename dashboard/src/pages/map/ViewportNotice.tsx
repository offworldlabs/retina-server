import "./ViewportNotice.css";

/**
 * Tells a visitor to the public map that the view is wider than the feed
 * serves. The real-fleet feed sends only the aircraft inside the visible box,
 * and the server cuts a box longer than its cap down around its centre, so a
 * view zoomed far out shows the aircraft near its middle and none at its
 * edges. useAircraftFeed says when the latest answer was cut
 * (`viewportClipped`).
 *
 * A status the map announces, not a control: nothing in it takes a pointer,
 * so the map under it pans and zooms as before. Nothing is rendered while the
 * whole view is served, so the map is then exactly as it was.
 */
export default function ViewportNotice({ visible }: { visible: boolean }) {
  if (!visible) return null;
  return (
    <div className="viewport-notice" role="status" aria-live="polite">
      <span className="viewport-notice-text">
        <svg className="viewport-notice-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
          <circle cx="7" cy="7" r="4.5" />
          <path d="M10.4 10.4 14 14M7 5v4M5 7h4" />
        </svg>
        Zoom in to see all aircraft
      </span>
    </div>
  );
}
