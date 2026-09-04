import { readFileSync } from "fs";
import zlib from "zlib";

function decodePNG(path) {
  const buf = readFileSync(path);
  if (buf.readUInt32BE(0) !== 0x89504e47) throw new Error("not png");
  let pos = 8, w = 0, h = 0, colorType = 0, idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos), type = buf.toString("ascii", pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === "IHDR") { w = data.readUInt32BE(0); h = data.readUInt32BE(4); colorType = data[9]; }
    else if (type === "IDAT") idat.push(data);
    else if (type === "IEND") break;
    pos += 12 + len;
  }
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const bpp = colorType === 6 ? 4 : colorType === 2 ? 3 : 1;
  const stride = w * bpp;
  const out = Buffer.alloc(w * h * 4);
  let p = 0;
  let prev = Buffer.alloc(stride);
  for (let y = 0; y < h; y++) {
    const f = raw[p++];
    const line = Buffer.from(raw.subarray(p, p + stride)); p += stride;
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? line[x - bpp] : 0, b = prev[x], c = x >= bpp ? prev[x - bpp] : 0;
      let v = line[x];
      if (f === 1) v += a; else if (f === 2) v += b; else if (f === 3) v += (a + b) >> 1;
      else if (f === 4) { const pp = a + b - c, pa = Math.abs(pp - a), pb = Math.abs(pp - b), pc = Math.abs(pp - c); v += (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c); }
      line[x] = v & 255;
    }
    if (bpp === 4) line.copy(out, y * stride);
    else { for (let x = 0; x < w; x++) { const i4 = (y * w + x) * 4; out[i4] = out[i4+1] = out[i4+2] = line[x * bpp]; out[i4+3] = 255; } }
    prev = line;
  }
  return { w, h, data: out };
}

function lum(img, x, y) {
  const i = (y * img.w + x) * 4;
  return 0.299 * img.data[i] + 0.587 * img.data[i+1] + 0.114 * img.data[i+2];
}

function profile(img, y0, y1, x0, x1, thresh) {
  const rows = [];
  for (let y = y0; y < y1; y++) {
    let n = 0, sum = 0;
    for (let x = x0; x < x1; x++) {
      const L = lum(img, x, y);
      if (L < thresh) { n++; sum += (255 - L); }
    }
    rows.push({ y, n, avg: n ? Math.round(sum / n) : 0 });
  }
  return rows;
}

function dump(name, img) {
  const W = img.w;
  console.log(`\n=== ${name} ===`);
  console.log("  y |  mid(18-82%)      | left(0-16%) | right(84-100%)");
  console.log("    |  cnt  avgd         | cnt avgd     | cnt avgd");
  const mid = profile(img, 150, 520, Math.floor(W*0.18), Math.floor(W*0.82), 140);
  const left = profile(img, 150, 520, 0, Math.floor(W*0.16), 140);
  const right = profile(img, 150, 520, Math.floor(W*0.84), W, 140);
  for (let i = 0; i < mid.length; i += 3) {
    const m = mid[i], l = left[i], r = right[i];
    console.log(`${String(m.y).padStart(3)} | ${String(m.n).padStart(6)} ${String(m.n ? m.avg : 0).padStart(3)}        | ${String(l.n).padStart(4)} ${String(l.n ? l.avg : 0).padStart(3)}      | ${String(r.n).padStart(4)} ${String(r.n ? r.avg : 0).padStart(3)}`);
  }
}

const a = decodePNG("/workspace/.uploads/7ecbd647-ffef-4c8d-b93b-0a104e01e58a_123.png");
const b = decodePNG("/workspace/.uploads/513c201a-53d4-4d53-8a65-c2db523645f4_456.png");
dump("123 OK", a);
dump("456 squeezed", b);