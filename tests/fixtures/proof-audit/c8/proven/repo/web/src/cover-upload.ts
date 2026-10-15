// The cover upload dropzone: one PNG or JPEG file, as the legacy uploader's accept list.
const ACCEPTED = ["image/png", "image/jpeg"];

export class CoverUpload {
  cropped: string[] = [];
  dragging = false;

  constructor(readonly multiple = false) {}

  dragOver(): void {
    this.dragging = true;
  }

  dragLeave(): void {
    this.dragging = false;
  }

  drop(files: { name: string; type: string }[]): void {
    this.dragging = false;
    const first = files[0];
    if (first && ACCEPTED.includes(first.type)) this.cropped = [first.name];
  }
}
