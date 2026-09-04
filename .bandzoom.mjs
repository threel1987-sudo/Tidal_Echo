import { readFileSync, writeFileSync } from "node:fs";
import zlib from "node:zlib";
function decodePNG(path){
  const buf = readFileSync(path);
  let pos = 8, w = 0, h = 0, ct = 0, idat = [];
  while (pos < buf.length){
    const len = buf.readUInt32BE(pos), type = buf.toString("ascii", pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === "IHDR"){ w = data.readUInt32BE(0); h = data.readUInt32BE(4); ct = data[9]; }
    else if (type === "IDAT") idat.push(data);
    else if (type === "IEND") break;
    pos += 12 + len;
  }
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const bpp = ct === 6 ? 4 : ct === 2 ? 3 : 1;
  const stride = w * bpp, out = Buffer.alloc(w * h * 4);
  let p = 0, prev = Buffer.alloc(stride);
  for (let y = 0; y < h; y++){
    const f = raw[p++], line = Buffer.from(raw.subarray(p, p + stride)); p += stride;
    for (let x = 0; x < stride; x++){
      const a = x >= bpp ? line[x - bpp] : 0, b = prev[x], c = x >= bpp ? prev[x - bpp] : 0;
      let v = line[x];
      if (f === 1) v += a; else if (f === 2) v += b; else if (f === 3) v += (a + b) >> 1;
      else if (f === 4){ const pp = a + b - c, pa = Math.abs(pp - a), pb = Math.abs(pp - b), pc = Math.abs(pp - c); v += (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c); }
      line[x] = v & 255;
    }
    if (bpp === 4) line.copy(out, y * stride);
    else for (let x = 0; x < w; x++){ const i4 = (y * w + x) * 4; out[i4] = out[i4+1] = out[i4+2] = line[x * bpp]; out[i4+3] = 255; }
    prev = line;
  }
  return { w, h, data: out };
}
function crc32(b){ let c = ~0; for (let i = 0; i < b.length; i++){ c ^= b[i]; for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xEDB88320 & -(c & 1)); } return ~c >>> 0; }
function chunk(t, d){ const c = Buffer.alloc(12 + d.length); c.writeUInt32BE(d.length, 0); c.write(t, 4, "ascii"); d.copy(c, 8); c.writeUInt32BE(crc32(Buffer.concat([Buffer.from(t, "ascii"), d])), 8 + d.length); return c; }
function savePNG(path, w, h, data){
  const stride = w * 4, raw = Buffer.alloc((stride + 1) * h);
  for (let y = 0; y < h; y++){ raw[y * (stride + 1)] = 0; data.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride); }
  const sig = Buffer.from([0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A]);
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8]=8; ihdr[9]=6;
  writeFileSync(path, Buffer.concat([sig, chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]));
}
function crop(path, out, x0, x1, y0, y1, scale=3){
  const img = decodePNG(path);
  const w = x1 - x0, h = y1 - y0, W = w * scale, H = h * scale, o = Buffer.alloc(W * H * 4);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++){
    const sx = Math.min(img.w - 1, x0 + Math.floor(x / scale)), sy = Math.min(img.h - 1, y0 + Math.floor(y / scale));
    const s = (sy * img.w + sx) * 4, d = (y * W + x) * 4;
    o[d] = o[d+1] = o[d+2] = (img.data[s] + img.data[s+1] + img.data[s+2]) / 3; o[d+3] = 255;
  }
  savePNG(out, W, H, o);
}
const P123 = "/workspace/.uploads/7ecbd647-ffef-4c8d-b93b-0a104e01e58a_123.png";
const P456 = "/workspace/.uploads/513c201a-53d4-4d53-8a65-c2db523645f4_456.png";
crop(P123, "/tmp/b123_1.png", 350, 900, 186, 250);   // 123 第一行
crop(P123, "/tmp/b123_2.png", 350, 900, 248, 315);   // 123 第二/三行
crop(P456, "/tmp/b456_1.png", 350, 900, 182, 250);   // 456 第一行
crop(P456, "/tmp/b456_2.png", 350, 900, 262, 315);   // 456 第二块
console.log("done");