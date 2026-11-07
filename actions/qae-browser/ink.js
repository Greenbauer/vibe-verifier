// The icon-off-center rule's pixel half (icon-page.js finds the controls): where a control's icon or spinner is drawn,
// read from screenshots of the control, since a CSS-drawn mark or a 3D-transformed spinner draws its ink far from the
// boxes the DOM reports. A subscribed app's pending button swapped its label for a spinner of three orbits, each one
// border edge of a tilted circle: inline-flex centered its box, but its ink sat right of the middle (2026-11-07).
//
// The screenshot is decoded here, in Node (zlib is built in), never in the app's page, whose content security policy
// may refuse a data: image.
const zlib = require("zlib");

const INK = 16;        // a channel this far from the background is ink: a ring's 30%-white track on a blue button is 47 to 65
const EDGE = 1;        // px of the scanned box's rim that ink may not reach: ink there is a border, a shadow or a bar, not an icon
const ICON_TOL = 1.5;  // px an icon's ink may sit off the middle of its control's content box

const paeth = (a, b, c) => {
  const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
};

// A PNG's header and its image data, joined, or null for a file that is not a PNG.
function chunks(png) {
  if (!png || png.length < 8 || png.readUInt32BE(0) !== 0x89504e47) return null;
  let head = null;
  const data = [];
  for (let at = 8; at + 8 <= png.length;) {
    const size = png.readUInt32BE(at), kind = png.toString("latin1", at + 4, at + 8), body = png.subarray(at + 8, at + 8 + size);
    if (kind === "IHDR") head = { width: body.readUInt32BE(0), height: body.readUInt32BE(4), depth: body[8], type: body[9], interlace: body[12] };
    else if (kind === "IDAT") data.push(body);
    else if (kind === "IEND") break;
    at += 12 + size;
  }
  return head && { head, data: Buffer.concat(data) };
}

// Undoes one row's PNG filter in place, from the row above it.
function unfilter(row, above, filter, channels) {
  for (let i = 0; i < row.length; i++) {
    const left = i >= channels ? row[i - channels] : 0, up = above[i], corner = i >= channels ? above[i - channels] : 0;
    row[i] = (row[i] + [0, left, up, (left + up) >> 1, paeth(left, up, corner)][filter]) & 255;
  }
}

// A screenshot's pixels as {width, height, rgb}, or null for a PNG this does not read (only 8-bit RGB or RGBA,
// not interlaced: what Chromium writes).
function decodePng(png) {
  const read = chunks(png), channels = read && { 2: 3, 6: 4 }[read.head.type];
  if (!channels || read.head.depth !== 8 || read.head.interlace) return null;
  const { width, height } = read.head, raw = zlib.inflateSync(read.data), stride = width * channels;
  const rgb = Buffer.alloc(width * height * 3);
  let above = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const row = Buffer.from(raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1)));
    unfilter(row, above, raw[y * (stride + 1)], channels);
    for (let x = 0; x < width; x++) row.copy(rgb, (y * width + x) * 3, x * channels, x * channels + 3);
    above = row;
  }
  return { width, height, rgb };
}

// Whether the point (x, y) is inside `box` with its rounded corners ({left, top, right, bottom, radius: [tl, tr, br, bl]}).
function inside(box, x, y) {
  if (x < box.left || x > box.right || y < box.top || y > box.bottom) return false;
  const corners = [[box.left, box.top], [box.right, box.top], [box.right, box.bottom], [box.left, box.bottom]];
  return corners.every(([cx, cy], k) => {
    const r = box.radius[k], ox = cx === box.left ? cx + r : cx - r, oy = cy === box.top ? cy + r : cy - r;
    const out = (cx === box.left ? x < ox : x > ox) && (cy === box.top ? y < oy : y > oy);
    return !out || Math.hypot(x - ox, y - oy) <= r;
  });
}

// The pixels of `image` inside the control's scanned box, each [column, row, its offset in image.rgb].
function scanned(image, { clip, scan }) {
  const scale = image.width / clip.width, out = [];
  for (let j = 0; j < image.height; j++) for (let i = 0; i < image.width; i++) {
    if (inside(scan, clip.x + (i + 0.5) / scale, clip.y + (j + 0.5) / scale)) out.push([i, j, (j * image.width + i) * 3]);
  }
  return out;
}

// The colour most of `pixels` show, as [r, g, b].
function background(image, pixels) {
  const counts = new Map();
  for (const [, , p] of pixels) {
    const key = image.rgb.readUIntBE(p, 3);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const most = [...counts].reduce((a, b) => (b[1] > a[1] ? b : a))[0];
  return [most >> 16, (most >> 8) & 255, most & 255];
}

// The box the ink pixels fill, in the window's CSS px.
function inkBox(ink, { clip }, scale) {
  const box = { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity };
  for (const [i, j] of ink) {
    box.left = Math.min(box.left, i); box.right = Math.max(box.right, i + 1);
    box.top = Math.min(box.top, j); box.bottom = Math.max(box.bottom, j + 1);
  }
  return { left: clip.x + box.left / scale, right: clip.x + box.right / scale, top: clip.y + box.top / scale, bottom: clip.y + box.bottom / scale };
}

// How far the ink in one screenshot of `control` sits from its content box's middle, {dx, dy} in CSS px, or null when
// it cannot be told: no ink, or ink that reaches the scanned box's rim. The control is {clip, scan, center} from
// icon-page.js, in the window's CSS px: the screenshot's area, the box inside its border (corners rounded) to look in,
// and the content box's middle. The background is the colour most of the scanned box shows.
function inkOffset(image, control) {
  const pixels = image ? scanned(image, control) : [];
  if (!pixels.length) return null;
  const tone = background(image, pixels);
  const ink = pixels.filter(([, , p]) => tone.some((v, k) => Math.abs(image.rgb[p + k] - v) > INK));
  if (!ink.length) return null;
  const { scan, center } = control, box = inkBox(ink, control, image.width / control.clip.width);
  if (box.left < scan.left + EDGE || box.right > scan.right - EDGE || box.top < scan.top + EDGE || box.bottom > scan.bottom - EDGE) return null;
  return { dx: (box.left + box.right) / 2 - center.x, dy: (box.top + box.bottom) / 2 - center.y };
}

// The finding's detail when every sample's ink sits more than ICON_TOL off the middle (a spinner turns, so the side may
// change), else null. A sample that could not be told, or one sample near the middle, clears the control: a symmetric
// spinner caught between frames is not a defect.
function offCenter(samples) {
  if (!samples.length || samples.some(s => !s || Math.max(Math.abs(s.dx), Math.abs(s.dy)) <= ICON_TOL)) return null;
  const side = s => [[s.dx, "right", "left"], [s.dy, "low", "high"]].filter(([d]) => Math.abs(d) > ICON_TOL)
    .map(([d, plus, minus]) => `${Math.abs(d).toFixed(1)}px ${d > 0 ? plus : minus}`).join(" and ");
  const where = samples.length > 1 ? `in each of ${samples.length} frames: ${samples.map(side).join("; ")}` : side(samples[0]);
  return `its icon's ink sits off the middle of the control ${where}`;
}

module.exports = { decodePng, inkOffset, offCenter, inside, ICON_TOL };
