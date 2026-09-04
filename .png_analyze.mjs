// PNG 解码小工具:读取两张截图,输出顶部区域的精确布局信息 + 裁剪顶部条带供细看
import { readFileSync, writeFileSync } from "node:fs";
import zlib from "node:zlib";

function decodePNG(path){
  const buf = readFileSync(path);
  if (buf.readUInt32BE(0) !== 0x89504E47) throw new Error("not png");
  let pos = 8, w = 0, h = 0, colorType = 0, idat = [];
  while (pos < buf.length){
    const len = buf.readUInt32BE(pos); const type = buf.toString("ascii", pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === "IHDR"){ w = data.readUInt32BE(0); h = data.readUInt32BE(4); colorType = data[9]; }
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
  for (let y = 0; y < h; y++){
    const f = raw[p++];
    const line = Buffer.from(raw.subarray(p, p + stride)); p += stride;
    for (let x = 0; x < stride; x++){
      const a = x >= bpp ? line[x - bpp] : 0, b = prev[x], c = x >= bpp ? prev[x - bpp] : 0;
      let v = line[x];
      if (f === 1) v += a; else if (f === 2) v += b; else if (f === 3) v += (a + b) >> 1;
      else if (f === 4){ const pp = a + b - c, pa = Math.abs(pp - a), pb = Math.abs(pp - b), pc = Math.abs(pp - c); v += (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c); }
      line[x] = v & 255;
    }
    if (bpp === 4) line.copy(out, y * stride);
    else { for (let x = 0; x < w; x++){ const i4 = (y * w + x) * 4; out[i4] = out[i4+1] = out[i4+2] = line[x * bpp]; out[i4+3] = 255; } }
    prev = line;
  }
  return { w, h, data: out };
}

function gray(img, x, y){ const i = (y * img.w + x) * 4; return (img.data[i] + img.data[i+1] + img.data[i+2]) / 3; }

function darkRows(img, y0, y1, x0, x1, thr = 120){
  const rows = [];
  for (let y = y0; y < y1; y++){
    let cnt = 0;
    for (let x = x0; x < x1; x++) if (gray(img, x, y) < thr) cnt++;
    rows.push({ y, cnt });
  }
  return rows;
}
function bands(rows, minCnt = 3, gap = 4){
  const out = []; let cur = null;
  for (const r of rows){
    if (r.cnt >= minCnt){
      if (!cur) cur = { y0: r.y, y1: r.y, peak: r.cnt };
      else { cur.y1 = r.y; cur.peak = Math.max(cur.peak, r.cnt); }
    } else if (cur && r.y - cur.y1 > gap){ out.push(cur); cur = null; }
  }
  if (cur) out.push(cur);
  return out;
}

function analyze(path){
  const img = decodePNG(path);
  const W = img.w, H = img.h;
  console.log("== " + path.split("/").pop(), W + "x" + H);
  const cx0 = Math.floor(W * 0.30), cx1 = Math.floor(W * 0.70);
  const isl = bands(darkRows(img, 0, Math.floor(H * 0.2), cx0, cx1, 60), 50);
  console.log("island:", isl.length ? `y ${isl[0].y0}-${isl[0].y1} (h=${isl[0].y1 - isl[0].y0})` : "not found");
  const mid = darkRows(img, 0, 480, Math.floor(W * 0.18), Math.floor(W * 0.82), 130);
  console.log("mid bands :", bands(mid, 4).map(b => `${b.y0}-${b.y1}(p${b.peak})`).join("  "));
  const left = darkRows(img, 0, 480, 0, Math.floor(W * 0.16), 130);
  console.log("left bands:", bands(left, 4).map(b => `${b.y0}-${b.y1}(p${b.peak})`).join("  "));
  const right = darkRows(img, 0, 480, Math.floor(W * 0.84), W, 130);
  console.log("right bands:", bands(right, 4).map(b => `${b.y0}-${b.y1}(p${b.peak})`).join("  "));
  const clock = bands(darkRows(img, 0, 120, Math.floor(W * 0.02), Math.floor(W * 0.30), 110), 3);
  console.log("clock band:", clock.slice(0, 3).map(b => `${b.y0}-${b.y1}(p${b.peak})`).join("  "));
  return img;
}

const i1 = analyze("/workspace/.uploads/7ecbd647-ffef-4c8d-b93b-0a104e01e58a_123.png");
const i2 = analyze("/workspace/.uploads/513c201a-53d4-4d53-8a65-c2db523645f4_456.png");

function crc32(buf){
  let c = ~0;
  for (let i = 0; i < buf.length; i++){ c ^= buf[i]; for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xEDB88320 & -(c & 1)); }
  return ~c >>> 0;
}
function chunk(t, d){
  const c = Buffer.alloc(12 + d.length);
  c.writeUInt32BE(d.length, 0); c.write(t, 4, "ascii"); d.copy(c, 8);
  c.writeUInt32BE(crc32(Buffer.concat([Buffer.from(t, "ascii"), d])), 8 + d.length);
  return c;
}
function cropTop(img, out, h = 480){
  const w = img.w, stride = w * 4;
  const raw = Buffer.alloc((stride + 1) * h);
  for (let y = 0; y < h; y++){
    raw[y * (stride + 1)] = 0;
    img.data.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  }
  const sig = Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]);
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 6; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  writeFileSync(out, Buffer.concat([sig, chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]));
  console.log("saved", out);
}
cropTop(i1, "/tmp/top_123.png");
cropTop(i2, "/tmp/top_456.png");