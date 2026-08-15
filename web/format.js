const encoder = new TextEncoder();

function asBytes(value) {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  throw new TypeError("expected an ArrayBuffer or typed array");
}

function concatBytes(parts) {
  const normalized = parts.map(asBytes);
  const total = normalized.reduce((sum, part) => sum + part.byteLength, 0);
  const result = new Uint8Array(total);
  let offset = 0;
  for (const part of normalized) {
    result.set(part, offset);
    offset += part.byteLength;
  }
  return result;
}

const crcTable = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < table.length; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
    }
    table[index] = value >>> 0;
  }
  return table;
})();

export function crc32(value) {
  const bytes = asBytes(value);
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc = crcTable[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function adler32(value) {
  const bytes = asBytes(value);
  let a = 1;
  let b = 0;
  for (const byte of bytes) {
    a = (a + byte) % 65521;
    b = (b + a) % 65521;
  }
  return ((b << 16) | a) >>> 0;
}

function zlibStore(value) {
  const bytes = asBytes(value);
  const blocks = [new Uint8Array([0x78, 0x01])];
  let offset = 0;
  do {
    const length = Math.min(65535, bytes.length - offset);
    const final = offset + length >= bytes.length;
    const block = new Uint8Array(5 + length);
    const view = new DataView(block.buffer);
    block[0] = final ? 1 : 0;
    view.setUint16(1, length, true);
    view.setUint16(3, (~length) & 0xffff, true);
    block.set(bytes.subarray(offset, offset + length), 5);
    blocks.push(block);
    offset += length;
    if (bytes.length === 0) break;
  } while (offset < bytes.length);

  const checksum = new Uint8Array(4);
  new DataView(checksum.buffer).setUint32(0, adler32(bytes), false);
  blocks.push(checksum);
  return concatBytes(blocks);
}

function pngChunk(type, data) {
  const typeBytes = encoder.encode(type);
  const payload = asBytes(data);
  const checksum = crc32(concatBytes([typeBytes, payload]));
  const chunk = new Uint8Array(12 + payload.length);
  const view = new DataView(chunk.buffer);
  view.setUint32(0, payload.length, false);
  chunk.set(typeBytes, 4);
  chunk.set(payload, 8);
  view.setUint32(8 + payload.length, checksum, false);
  return chunk;
}

function encodePng(samples, width, height, bitDepth) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1) {
    throw new RangeError("PNG dimensions must be positive integers");
  }
  if (samples.length !== width * height) {
    throw new RangeError("PNG sample count does not match dimensions");
  }

  const bytesPerSample = bitDepth === 16 ? 2 : 1;
  const scanlines = new Uint8Array(height * (1 + width * bytesPerSample));
  const view = new DataView(scanlines.buffer);
  let offset = 0;
  for (let y = 0; y < height; y += 1) {
    scanlines[offset] = 0;
    offset += 1;
    for (let x = 0; x < width; x += 1) {
      const sample = Math.max(0, Math.min(bitDepth === 16 ? 65535 : 255, Math.round(samples[y * width + x])));
      if (bitDepth === 16) {
        view.setUint16(offset, sample, false);
        offset += 2;
      } else {
        scanlines[offset] = sample;
        offset += 1;
      }
    }
  }

  const header = new Uint8Array(13);
  const headerView = new DataView(header.buffer);
  headerView.setUint32(0, width, false);
  headerView.setUint32(4, height, false);
  header[8] = bitDepth;
  header[9] = 0; // grayscale
  header[10] = 0;
  header[11] = 0;
  header[12] = 0;
  return concatBytes([
    new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk("IHDR", header),
    pngChunk("IDAT", zlibStore(scanlines)),
    pngChunk("IEND", new Uint8Array()),
  ]);
}

export function encodePngGray16(samples, width, height) {
  return encodePng(samples, width, height, 16);
}

export function encodePngGray8(samples, width, height) {
  return encodePng(samples, width, height, 8);
}

export function utf8Bytes(value) {
  return encoder.encode(value);
}

function dosDateTime() {
  const now = new Date();
  const date = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
  const time = (now.getHours() << 11) | (now.getMinutes() << 5) | Math.floor(now.getSeconds() / 2);
  return { date, time };
}

export function buildZip(entries) {
  const localParts = [];
  const centralParts = [];
  const { date, time } = dosDateTime();
  let offset = 0;

  for (const entry of entries) {
    const name = encoder.encode(entry.name);
    const data = asBytes(entry.data);
    const checksum = crc32(data);
    const local = new Uint8Array(30 + name.length);
    const localView = new DataView(local.buffer);
    localView.setUint32(0, 0x04034b50, true);
    localView.setUint16(4, 20, true);
    localView.setUint16(6, 0x0800, true); // UTF-8 names
    localView.setUint16(8, 0, true); // stored, no compression
    localView.setUint16(10, time, true);
    localView.setUint16(12, date, true);
    localView.setUint32(14, checksum, true);
    localView.setUint32(18, data.length, true);
    localView.setUint32(22, data.length, true);
    localView.setUint16(26, name.length, true);
    localView.setUint16(28, 0, true);
    local.set(name, 30);
    localParts.push(local, data);

    const central = new Uint8Array(46 + name.length);
    const centralView = new DataView(central.buffer);
    centralView.setUint32(0, 0x02014b50, true);
    centralView.setUint16(4, 20, true);
    centralView.setUint16(6, 20, true);
    centralView.setUint16(8, 0x0800, true);
    centralView.setUint16(10, 0, true);
    centralView.setUint16(12, time, true);
    centralView.setUint16(14, date, true);
    centralView.setUint32(16, checksum, true);
    centralView.setUint32(20, data.length, true);
    centralView.setUint32(24, data.length, true);
    centralView.setUint16(28, name.length, true);
    centralView.setUint16(30, 0, true);
    centralView.setUint16(32, 0, true);
    centralView.setUint16(34, 0, true);
    centralView.setUint16(36, 0, true);
    centralView.setUint32(38, 0, true);
    centralView.setUint32(42, offset, true);
    central.set(name, 46);
    centralParts.push(central);
    offset += local.length + data.length;
  }

  const centralDirectory = concatBytes(centralParts);
  const end = new Uint8Array(22);
  const endView = new DataView(end.buffer);
  endView.setUint32(0, 0x06054b50, true);
  endView.setUint16(8, entries.length, true);
  endView.setUint16(10, entries.length, true);
  endView.setUint32(12, centralDirectory.length, true);
  endView.setUint32(16, offset, true);
  endView.setUint16(20, 0, true);
  return new Blob([concatBytes([...localParts, centralDirectory, end])], { type: "application/zip" });
}

export async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", asBytes(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
