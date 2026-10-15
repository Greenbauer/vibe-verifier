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
    for (const file of files.filter((each) => ACCEPTED.includes(each.type))) {
      this.cropped.push(file.name);
    }
  }
}
