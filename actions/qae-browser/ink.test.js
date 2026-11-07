// The icon-off-center rule's pixel half (ink.js), on screenshots made here. Run by tests/test_ui_audit.py;
// tests/test_qae_ui_audit.py reads real chromium screenshots of tests/fixtures/ui-audit/ where a browser is installed.
const assert = require("node:assert");
const zlib = require("node:zlib");
const { test } = require("node:test");

const { decodePng, inkOffset, offCenter } = require("./ink.js");

const paeth = (a, b, c) => {
  const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
};

// A PNG of `width` x `height` whose pixel (x, y) is `pixel(x, y)`, each row under the next of PNG's five filters, as
// RGB or, with `alpha`, RGBA. The decoder does not read CRCs, so they are left zero.
// One row as PNG stores it: its filter's number, then each byte less the filter's guess from the row above.
function filtered(row, above, filter, channels) {
  const out = Buffer.alloc(row.length + 1);
  out[0] = filter;
  for (let i = 0; i < row.length; i++) {
    const left = i >= channels ? row[i - channels] : 0, up = above[i], corner = i >= channels ? above[i - channels] : 0;
    out[i + 1] = (row[i] - [0, left, up, (left + up) >> 1, paeth(left, up, corner)][filter]) & 255;
  }
  return out;
}

function png(width, height, pixel, alpha = false) {
  const channels = alpha ? 4 : 3, stride = width * channels, rows = [];
  let above = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const row = Buffer.alloc(stride);
    for (let x = 0; x < width; x++) row.set(alpha ? [...pixel(x, y), 255] : pixel(x, y), x * channels);
    rows.push(filtered(row, above, y % 5, channels));
    above = row;
  }
  const chunk = (kind, body) => {
    const head = Buffer.alloc(8);
    head.writeUInt32BE(body.length);
    head.write(kind, 4, "latin1");
    return Buffer.concat([head, body, Buffer.alloc(4)]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = alpha ? 6 : 2;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", header),
    chunk("IDAT", zlib.deflateSync(Buffer.concat(rows))), chunk("IEND", Buffer.alloc(0))]);
}

// A 32px button at (100, 50) with a 1px border and 8px corners, as icon-page.js describes it, on a page of `page`
// colour, filled `fill`, with `ink` drawing where `at(x, y)` says (x, y in the button's own px).
const BUTTON = { clip: { x: 100, y: 50, width: 32, height: 32 }, center: { x: 116, y: 66 },
  scan: { left: 102, top: 52, right: 130, bottom: 80, radius: [6, 6, 6, 6] } };
const PAGE = [17, 17, 17], FILL = [37, 99, 235], INK = [255, 255, 255];
const corner = (x, y) => Math.hypot(Math.max(0, Math.abs(x + 0.5 - 16) - 8), Math.max(0, Math.abs(y + 0.5 - 16) - 8)) > 8;
const button = (at, scale = 1) => decodePng(png(32 * scale, 32 * scale, (i, j) => {
  const [x, y] = [i / scale, j / scale];
  return corner(x, y) ? PAGE : at(x, y) ? INK : FILL;
}));
const square = (left, top, size = 10) => (x, y) => x >= left && x < left + size && y >= top && y < top + size;

test("a screenshot decodes the same through every PNG filter, as RGB and as RGBA", () => {
  const pixel = (x, y) => [x * 20 % 256, y * 30 % 256, (x * y) % 256];
  for (const alpha of [false, true]) {
    const image = decodePng(png(7, 11, pixel, alpha));
    assert.deepStrictEqual([image.width, image.height], [7, 11]);
    for (let y = 0; y < 11; y++) for (let x = 0; x < 7; x++) {
      assert.deepStrictEqual([...image.rgb.subarray((y * 7 + x) * 3, (y * 7 + x) * 3 + 3)], pixel(x, y));
    }
  }
  assert.strictEqual(decodePng(Buffer.from("not a png")), null);
});

test("the ink's offset is its box's middle from the content box's, and the corners outside the border are not ink", () => {
  assert.deepStrictEqual(inkOffset(button(square(11, 11)), BUTTON), { dx: 0, dy: 0 });
  assert.deepStrictEqual(inkOffset(button(square(14, 10)), BUTTON), { dx: 3, dy: -1 });
  // At twice the pixels per CSS px the offset is still in CSS px.
  assert.deepStrictEqual(inkOffset(button(square(14, 10), 2), { ...BUTTON }), { dx: 3, dy: -1 });
});

test("ink that reaches the box's rim (a bar, a border, a shadow) cannot be told from the icon, and no ink is no sample", () => {
  assert.strictEqual(inkOffset(button((x, y) => square(11, 11)(x, y) || y >= 28), BUTTON), null);
  assert.strictEqual(inkOffset(button(() => false), BUTTON), null);
  assert.strictEqual(inkOffset(null, BUTTON), null);
});

test("a faint track counts as ink: a ring with one bright arc is centered", () => {
  const ring = (x, y) => Math.abs(Math.hypot(x + 0.5 - 16, y + 0.5 - 16) - 7) < 1;
  const image = decodePng(png(32, 32, (x, y) => {
    if (corner(x, y)) return PAGE;
    if (!ring(x, y)) return FILL;
    return y < 12 ? INK : FILL.map(v => Math.round(v + 0.3 * (255 - v)));   // the top arc white, the track 30% white
  }));
  assert.deepStrictEqual(inkOffset(image, BUTTON), { dx: 0, dy: 0 });
});

test("a control is off-center only when every frame is, on either axis, by more than 1.5px", () => {
  const right = { dx: 2.5, dy: 0 }, low = { dx: 0.2, dy: -1.8 }, near = { dx: 1.5, dy: 1.5 };
  assert.match(offCenter([right, low, right]), /in each of 3 frames: 2\.5px right; 1\.8px high; 2\.5px right$/);
  assert.strictEqual(offCenter([right, near, right]), null);   // a symmetric spinner caught between frames
  assert.strictEqual(offCenter([right, null, right]), null);   // a frame that could not be read clears it
  assert.strictEqual(offCenter([]), null);
});
