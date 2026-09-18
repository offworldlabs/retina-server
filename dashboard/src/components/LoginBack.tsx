import { useLocation, useNavigate } from "react-router-dom";

/**
 * The way off a sign-in card.
 *
 * None on the admin console: every route there wants a session and would send
 * the caller straight back to the card. Only open pages draw the header's
 * sign-in link, and it marks the entry, so popping back lands on something the
 * visitor can see. Anyone else goes to the map.
 */
export default function LoginBack({ isAdmin }) {
  const navigate = useNavigate();
  const location = useLocation();

  if (isAdmin) return null;

  const [label, go] = location.state?.fromOpenPage
    ? ["Back", () => navigate(-1)]
    : ["Back to the map", () => navigate("/map")];

  return (
    <button type="button" className="login-back" onClick={go}>
      <span aria-hidden="true">←</span> {label}
    </button>
  );
}
