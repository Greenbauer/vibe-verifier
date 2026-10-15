import assert from "node:assert/strict";
import test from "node:test";
import { CoverUpload } from "../src/cover-upload.js";

const file = (name: string, type: string) => ({ name, type });

test("the dropzone shows a drag-hover state and crops only the first dropped file", () => {
  const upload = new CoverUpload();
  upload.dragOver();
  assert.equal(upload.dragging, true);
  upload.dragLeave();
  assert.equal(upload.dragging, false);
  assert.equal(upload.multiple, false);

  upload.drop([file("first.png", "image/png"), file("second.jpg", "image/jpeg")]);
  assert.deepEqual(upload.cropped, ["first.png"]);
});

test("a dropped file that is not PNG or JPG is ignored", () => {
  const upload = new CoverUpload();
  upload.drop([file("cover.gif", "image/gif")]);
  assert.deepEqual(upload.cropped, []);
});
