// Aim input. Kept side-effect free so the module graph loads cleanly headless.
export const AIM_STEPS = 24;

export function readAim(step) {
  const t = (step % AIM_STEPS) / AIM_STEPS;
  return { angle: t * Math.PI * 2, power: 0.35 + t * 0.4 };
}

export function aimVector(aim) {
  return { vx: Math.cos(aim.angle) * aim.power, vy: Math.sin(aim.angle) * aim.power };
}
