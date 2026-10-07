// Camera/gesture indicator (six-feature expansion, Phase 4).
// Rendered in BOTH HUD modes (StatusBar full view + CompactWidget compact
// view) from the daemon's camera_state {active} broadcast — the explicit
// Timeline-gap lesson: an indicator must exist in every surface.

interface Props {
  active: boolean;
}

export default function CameraIndicator({ active }: Props) {
  if (!active) return null;
  return (
    <span
      data-testid="camera-indicator"
      className="px-2 py-0.5 rounded bg-teal-500/20 border border-teal-500/60 text-teal-300"
      title="Gesture camera active"
    >
      <span className="text-teal-400">●</span> CAM
    </span>
  );
}
