import { readFileSync } from "node:fs";
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
function bandStats(path, label, y0, y1){
  const img = decodePNG(path);
  const x0 = Math.floor(img.w * 0.30), x1 = Math.floor(img.w * 0.70);
  let veryDark = 0, dark = 0, mid = 0, maxD = 0;
  for (let y = y0; y < y1; y++){
    for (let x = x0; x < x1; x++){
      const i = (y * img.w + x) * 4;
      const L = 0.299*img.data[i] + 0.587*img.data[i+1] + 0.114*img.data[i+2];
      const d = 255 - L;
      if (d > maxD) maxD = d;
      if (d > 140) veryDark++;
      else if (d > 80) dark++;
      else if (d > 40) mid++;
    }
  }
  console.log(`${label} y${y0}-${y1}: 深黑(>140)=${veryDark} 灰(>80)=${dark} 浅灰(>40)=${mid} 最黑=${maxD}`);
}
// 第一行文字带
bandStats("/workspace/.uploads/7ecbd647-ffef-4c8d-b93b-0a104e01e58a_123.png", "123 第一行带", 194, 242);
bandStats("/workspace/.uploads/513c201a-53d4-4d53-8a65-c2db523645f4_456.png", "456 第一行带", 189, 244);
// 第二行带
bandStats("/workspace/.uploads/7ecbd647-ffef-4c8d-b93b-0a104e01e58a_123.png", "123 第二行带", 248, 252);
bandStats("/workspace/.uploads/7ecbd647-ffef-4c8d-b93b-0a104e01e58a_123.png", "123 第二行带2", 252, 310);
bandStats("/workspace/.uploads/513c201a-53d4-4d53-8a65-c2db523645f4_456.png", "456 第二行带", 272, 308);