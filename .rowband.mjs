import { readFileSync, writeFileSync } from "node:fs";
import zlib from "node:zlib";

function decodePNG(path){
  const buf = readFileSync(path);
  let pos = 8, w = 0, h = 0, colorType = 0, idat = [];
  while (pos < buf.length){
    const len = buf.readUInt32BE(pos), type = buf.toString("ascii", pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === "IHDR"){ w = data.readUInt32BE(0); h = data.readUInt32BE(4); colorType = data[9]; }
    else if (type === "IDAT") idat.push(data);
    else if (type === "IEND") break;
    pos += 12 + len;
  }
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const bpp = colorType === 6 ? 4 : colorType === 2 ? 3 : 1;
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
function lum(img, x, y){ const i = (y * img.w + x) * 4; return 0.299*img.data[i] + 0.587*img.data[i+1] + 0.114*img.data[i+2]; }
// 统计一行里"明显暗"的像素分布:mid / left / right 三段
function rowStat(img, y, thr){
  const W = img.w;
  const seg = { l: [0, Math.floor(W*0.16)], m: [Math.floor(W*0.18), Math.floor(W*0.82)], r: [Math.floor(W*0.84), W] };
  const out = {};
  for (const [k, [x0, x1]] of Object.entries(seg)){
    let n = 0; let sum = 0;
    for (let x = x0; x < x1; x++){ const L = lum(img, x, y); if (L < thr){ n++; sum += 255 - L; } }
    out[k] = n ? Math.round(sum / n) : 0;
  }
  return { y, ...out, n: out.l || out.m || out.r };
}
function analyze(path, label){
  const img = decodePNG(path);
  console.log(`\n===== ${label} (${img.w}x${img.h}) =====`);
  console.log("  y(dev)|  y(css) | mid(cnt:dark) | left(cnt:dark) | right(cnt:dark)");
  const lines = [];
  // 只看有内容的行,合并相邻块
  for (let y = 130; y < 700; y++){
    const s = rowStat(img, y, 150);
    if (s.m > 0 || s.l > 0 || s.r > 0) lines.push(s);
  }
  // 压缩成块
  let cur = null;
  for (const s of lines){
    if (!cur) cur = { y0: s.y, y1: s.y, rows: [s] };
    else if (s.y - cur.y1 <= 4) { cur.y1 = s.y; cur.rows.push(s); }
    else { flush(cur); cur = { y0: s.y, y1: s.y, rows: [s] }; }
  }
  if (cur) flush(cur);
  function flush(c){
    const pk = Math.max(...c.rows.map(r => r.m), ...c.rows.map(r => r.l), ...c.rows.map(r => r.r));
    console.log(`${String(c.y0).padStart(6)}-${String(c.y1).padStart(6)} | ${(c.y0/3).toFixed(0)}-${(c.y1/3).toFixed(0)} | peak ${pk}`);
  }
}
analyze("/workspace/.uploads/7ecbd647-ffef-4c8d-b93b-0a104e01e58a_123.png", "123 正常");
analyze("/workspace/.uploads/513c201a-53d4-4d53-8a65-c2db523645f4_456.png", "456 挤压后");