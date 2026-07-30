// Painting. The canvas must never stay black: the fairway, the gravity well and
// the HUD are all drawn on every frame.
export function draw(ctx, state, width, height) {
  if (!ctx) {
    return;
  }
  const w = width || 480;
  const h = height || 320;

  const sky = ctx.createLinearGradient(0, 0, 0, h);
  sky.addColorStop(0, '#1d3f6e');
  sky.addColorStop(1, '#0e2038');
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, w, h);

  ctx.fillStyle = '#2f6b3a';
  ctx.fillRect(0, state.level.floor, w, h - state.level.floor);

  ctx.fillStyle = '#8fb8ff';
  ctx.beginPath();
  ctx.arc(state.level.cup.x, state.level.cup.y, 22, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = '#f4f7ff';
  ctx.beginPath();
  ctx.arc(state.ball.x, state.ball.y, 6, 0, Math.PI * 2);
  ctx.fill();

  drawHud(ctx, state, w);
}

export function drawHud(ctx, state, width) {
  ctx.fillStyle = 'rgba(9, 14, 24, 0.72)';
  ctx.fillRect(0, 0, width, 28);
  ctx.fillStyle = '#e8ecf5';
  ctx.font = '14px system-ui, sans-serif';
  ctx.fillText(state.level.name + '  par ' + state.level.par, 10, 19);
  ctx.fillText('strokes ' + state.strokes, width - 96, 19);
}
