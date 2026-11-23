import { useLocation, useNavigate } from "react-router-dom";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { COARSE_TABLET, PHONE } from "../styles/breakpoints";

interface LoginBackProps {
  isAdmin?: boolean;
}

/**
 * On a phone, or a tablet driven by touch, the button grows to a finger's 44px
 * and moves up by the 8px it gained above, so its label stays where it was and
 * the box ends at the card's 48px top padding.
 */
const FINGER_TARGET = { top: 4, minHeight: 44 };

/**
 * The way off a sign-in card.
 *
 * None on the admin console: every route there wants a session and would send
 * the caller straight back to the card. Only open pages draw the header's
 * sign-in link, and it marks the entry, so popping back lands on something the
 * visitor can see. Anyone else goes to the map.
 */
export default function LoginBack({ isAdmin = false }: LoginBackProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const phone = useMediaQuery(PHONE);
  const coarseTablet = useMediaQuery(COARSE_TABLET);

  if (isAdmin) return null;

  const [label, go] = location.state?.fromOpenPage
    ? ["Back", () => navigate(-1)]
    : ["Back to the map", () => navigate("/map")];

  return (
    <button type="button" className="login-back" style={phone || coarseTablet ? FINGER_TARGET : undefined} onClick={go}>
      <span aria-hidden="true">←</span> {label}
    </button>
  );
}
