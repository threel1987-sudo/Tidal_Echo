// 对比 123(正常) vs 456(被挤) 的顶部区域,并排 + 网格线
import { readFileSync, writeFileSync } from "node:fs";
import zlib from "node:zlib";

function decodePNG(path) {
  const buf = readFileSync(path);
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
  let p = 0, prev = Buffer.alloc(stride);
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
    else for (let x = 0; x < w; x++) { const i4 = (y * w + x) * 4; out[i4] = out[i4+1] = out[i4+2] = line[x * bpp]; out[i4+3] = 255; }
    prev = line;
  }
  return { w, h, data: out };
}

function cropScale(img, y0, y1, scale) {
  const w = Math.round(img.w * scale), h = Math.round((y1 - y0) * scale);
  const out = Buffer.alloc(w * h * 4);
  for (let oy = 0; oy < h; oy++) {
    const sy = Math.min(img.h - 1, y0 + Math.floor(oy / scale));
    for (let ox = 0; ox < w; ox++) {
      const sx = Math.min(img.w - 1, Math.floor(ox / scale));
      const si = (sy * img.w + sx) * 4, di = (oy * w + ox) * 4;
      out[di] = img.data[si]; out[di+1] = img.data[si+1]; out[di+2] = img.data[si+2]; out[di+3] = 255;
    }
  }
  return { w, h, data: out };
}

function savePNG(path, w, h, rgba) {
  const stride = w * 4;
  const raw = Buffer.alloc((stride + 1) * h);
  for (let y = 0; y < h; y++) { raw[y * (stride + 1)] = 0; rgba.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride); }
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(type), data]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(td) >>> 0);
    return Buffer.concat([len, td, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 6;
  const png = Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
  writeFileSync(path, png);
  console.log("saved", path, w + "x" + h);
}

const A = decodePNG("/workspace/.uploads/7ecbd647-ffef-4c8d-b93b-0a104e01e58a_123.png");
const B = decodePNG("/workspace/.uploads/513c201a-53d4-4d53-8a65-c2db523645f4_456.png");
console.log("A", A.w + "x" + A.h, "B", B.w + "x" + B.h);

const y0 = 0, y1 = 1000;        // 顶部 1000 设备px
const scale = 0.5;              // 缩到一半
const ca = cropScale(A, y0, y1, scale), cb = cropScale(B, y0, y1, scale);

const gap = 20;
const W = ca.w + gap + cb.w, H = Math.max(ca.h, cb.h);
const out = Buffer.alloc(W * H * 4);
out.fill(255);
ca.data.copy(out, 0, 0, 0, ca.w * ca.h * 4);
cb.data.copy(out, (ca.w + gap) * 4, 0, 0, cb.w * cb.h * 4);

// 水平网格线:每 100 设备px(每 50 输出px)
for (let gy = 0; gy <= y1 - y0; gy += 100) {
  const oy = Math.round(gy * scale);
  if (oy >= H) continue;
  for (let x = 0; x < W; x++) {
    const i = (oy * W + x) * 4;
    if (x < ca.w + 4) { out[i] = 0; out[i+1] = 200; out[i+2] = 255; out[i+3] = 255; }
    else if (x > ca.w + gap - 4) { out[i] = 255; out[i+1] = 0; out[i+2] = 160; out[i+3] = 255; }
  }
}
savePNG("/workspace/.side2.png", W, H, out);