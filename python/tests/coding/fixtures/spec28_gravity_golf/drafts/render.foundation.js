// Painting (foundation slice: fairway and ball only, no gravity well, no HUD).
export function draw(ctx, state, width, height) {
  if (!ctx) {
    return;
  }
  const w = width || 480;
  const h = height || 320;

  ctx.fillStyle = '#16304f';
  ctx.fillRect(0, 0, w, h);

  ctx.fillStyle = '#2f6b3a';
  ctx.fillRect(0, state.level.floor, w, h - state.level.floor);

  ctx.fillStyle = '#f4f7ff';
  ctx.beginPath();
  ctx.arc(state.ball.x, state.ball.y, 6, 0, Math.PI * 2);
  ctx.fill();
}
