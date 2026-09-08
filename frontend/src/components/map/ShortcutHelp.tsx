interface ShortcutHelpProps {
  visible: boolean;
  onClose: () => void;
}

// Keep in sync with `shortcutMap` in LiveAircraftMap.tsx — this table is the
// user-facing documentation of those bindings.  (It previously listed an "O"
// binding that never existed and omitted Shift+X / P / M / N.)
const SHORTCUTS: Array<[string, string]> = [
  ["?",       "Show / hide this help"],
  ["/",       "Focus the aircraft search box"],
  ["Escape",  "Clear search / deselect aircraft"],
  ["Space",   "Pause / resume live feed"],
  ["F",       "Toggle filter panel"],
  ["L",       "Toggle aircraft labels"],
  ["T",       "Toggle trails"],
  ["C",       "Toggle node coverage zones"],
  ["I",       "Toggle illuminators (TX towers)"],
  ["G",       "Toggle ground-truth overlay"],
  ["D",       "Toggle detection arcs"],
  ["A",       "Colour aircraft by altitude"],
  ["R",       "Range rings around selected"],
  ["S",       "Live stats panel"],
  ["X",       "Export selected aircraft trail (CSV)"],
  ["Shift+X", "Export all aircraft trails (CSV)"],
  ["P",       "Pin / unpin selected aircraft"],
  ["M",       "Locate me"],
  ["N",       "Toggle emergency-squawk sound"],
];

export default function ShortcutHelp({ visible, onClose }: ShortcutHelpProps) {
  if (!visible) return null;
  return (
    <div className="shortcut-backdrop" onClick={onClose}>
      <div
        className="shortcut-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Keyboard shortcuts"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="shortcut-dialog-header">
          <strong>Keyboard shortcuts</strong>
          <button className="shortcut-dialog-close" onClick={onClose} title="Close">
            ×
          </button>
        </div>
        <table>
          <tbody>
            {SHORTCUTS.map(([key, label]) => (
              <tr key={key}>
                <td>
                  <kbd className="shortcut-key">{key}</kbd>
                </td>
                <td>{label}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="shortcut-dialog-footer">
          Shortcuts are ignored while typing in the search box.
        </div>
      </div>
    </div>
  );
}
