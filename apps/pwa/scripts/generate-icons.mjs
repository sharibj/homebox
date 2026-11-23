import { deflateSync } from "node:zlib";
import { writeFileSync } from "node:fs";

function crc32(bytes) {
  let crc = -1;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ -1) >>> 0;
}
function chunk(type, bytes) {
  const name = Buffer.from(type);
  const length = Buffer.alloc(4); length.writeUInt32BE(bytes.length);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(Buffer.concat([name, bytes])));
  return Buffer.concat([length, name, bytes, crc]);
}
const lines = [[50,78,96,53],[96,53,142,78],[142,78,142,126],[142,126,96,152],[96,152,50,126],
  [50,126,50,78],[50,78,96,104],[96,104,142,78],[96,104,96,152],[73,65,119,91],[119,91,119,110]];
function distance(x, y, line) {
  const [x1,y1,x2,y2] = line, dx=x2-x1,dy=y2-y1;
  const t=Math.max(0,Math.min(1,((x-x1)*dx+(y-y1)*dy)/(dx*dx+dy*dy)));
  return Math.hypot(x-x1-t*dx,y-y1-t*dy);
}
for (const size of [192,512]) {
  const data = Buffer.alloc((size*4+1)*size);
  for (let y=0;y<size;y++) {
    for (let x=0;x<size;x++) {
      const ink = lines.some(line => distance(x*192/size,y*192/size,line)<4.2);
      const offset = y*(size*4+1)+1+x*4;
      data.set(ink ? [230,239,192,255] : [23,79,67,255],offset);
    }
  }
  const header = Buffer.alloc(13);header.writeUInt32BE(size,0);header.writeUInt32BE(size,4);header[8]=8;header[9]=6;
  writeFileSync(`public/icon-${size}.png`,Buffer.concat([
    Buffer.from([137,80,78,71,13,10,26,10]),chunk("IHDR",header),chunk("IDAT",deflateSync(data)),chunk("IEND",Buffer.alloc(0)),
  ]));
}
